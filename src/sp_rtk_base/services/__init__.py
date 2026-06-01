"""SP-Base business logic services.

Provides singleton service instances and initialization for the
application's service layer:

- ``ConfigService`` — YAML configuration persistence
- ``RelayService`` — async wrapper around sp-rtk-base-relay RelayEngine
- ``EventBridge`` — thread-to-async event forwarding
- ``MetricsService`` — Prometheus metrics from RelayStatus
- ``DeviceService`` — GPS receiver connection & configuration (optional)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from pydantic import ValidationError
from sp_rtk_base_relay.config import DestinationConfig, InputConfig
from sp_rtk_base_relay.exceptions import ConfigurationError

from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.event_bridge import EventBridge
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.relay_service import RelayService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auto-start state
# ---------------------------------------------------------------------------

AutoStartState = Literal[
    "idle",
    "skipped_no_input",
    "in_progress",
    "succeeded",
    "succeeded_user",
    "failed_config",
    "failed_after_retries",
]


@dataclass(frozen=True)
class AutoStartStatus:
    """Snapshot of the auto-start lifecycle.

    Mutated as a single atomic replacement of the module attribute so
    readers in HTTP handlers and the Dashboard always see a consistent
    view.  Fields are intentionally simple: a state machine label, the
    attempt counter, the most recent error message (if any), and a
    timestamp for the most recent transition.
    """

    state: AutoStartState = "idle"
    attempts: int = 0
    last_error: str | None = None
    last_updated: datetime | None = None


# ``replace()`` is used to update fields immutably; consumers always
# read the current value of this module attribute.  Tests can reset
# it to a fresh idle status between cases.
auto_start_status: AutoStartStatus = AutoStartStatus()

# Background task reference — held at module scope so it isn't
# garbage-collected mid-flight and so tests can ``await`` it to
# synchronise on completion.
auto_start_task: asyncio.Task[None] | None = None

# Retry schedule: 0 (immediate), then exponential backoff capped at
# 80 s.  Six attempts total over ~155 s — long enough to outlast a
# post-power-cycle Bluetooth peer / USB-serial enumeration delay,
# short enough that the operator isn't kept waiting forever.
AUTO_START_BACKOFF_SECONDS: tuple[int, ...] = (0, 5, 10, 20, 40, 80)


def _set_auto_start_status(
    state: AutoStartState,
    attempts: int,
    last_error: str | None = None,
) -> None:
    """Atomically replace the module-level auto-start status."""
    global auto_start_status
    auto_start_status = replace(
        auto_start_status,
        state=state,
        attempts=attempts,
        last_error=last_error,
        last_updated=datetime.now(),
    )


# ---------------------------------------------------------------------------
# Module-level singleton instances
# ---------------------------------------------------------------------------

relay_service: RelayService = RelayService()
config_service: ConfigService = ConfigService()
event_bridge: EventBridge = EventBridge()
metrics_service: MetricsService = MetricsService()
device_service: DeviceService = DeviceService()


# ---------------------------------------------------------------------------
# FastAPI dependency injection helpers
# ---------------------------------------------------------------------------


def get_relay_service() -> RelayService:
    """Get the singleton RelayService instance.

    Returns:
        The application's RelayService instance.
    """
    return relay_service


def get_config_service() -> ConfigService:
    """Get the singleton ConfigService instance.

    Returns:
        The application's ConfigService instance.
    """
    return config_service


def get_event_bridge() -> EventBridge:
    """Get the singleton EventBridge instance.

    Returns:
        The application's EventBridge instance.
    """
    return event_bridge


def get_metrics_service() -> MetricsService:
    """Get the singleton MetricsService instance.

    Returns:
        The application's MetricsService instance.
    """
    return metrics_service


def get_device_service() -> DeviceService:
    """Get the singleton DeviceService instance.

    Returns:
        The application's DeviceService instance.
    """
    return device_service


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


async def _release_stale_bluetooth_handle(mac_address: str) -> None:
    """Best-effort: disconnect a stale BlueZ-side connection for *mac_address*.

    Called from :func:`init_services` before the relay engine is asked to
    open the same Bluetooth source.  If a previous instance exited
    uncleanly (``SIGKILL``, OOM, power-loss during shutdown) BlueZ can
    still believe the GPS receiver is connected — opening RFCOMM will
    then fail with ``Address already in use`` or hang waiting for an
    already-leased channel.

    This helper *only* asks BlueZ to drop the connection on its side;
    it does **not** un-pair, un-trust, or remove the device.  Errors
    are swallowed and logged — startup must not fail just because
    we couldn't pre-clean a handle that may not even have been stuck.

    Args:
        mac_address: The Bluetooth MAC of the configured GPS.
    """
    try:
        # Imported lazily so test environments that don't have dbus-fast
        # installed (CI, macOS dev boxes) don't pay the import cost or
        # fail at module load.
        from sp_rtk_base_relay.core.bluetooth_manager import BluetoothManager
    except ImportError:
        logger.debug(
            "BluetoothManager unavailable; skipping stale-handle release for %s",
            mac_address,
        )
        return

    mgr: BluetoothManager | None = None
    try:
        mgr = BluetoothManager()
        # disconnect_device is sync-but-blocks-on-D-Bus; push it off-loop
        # so a wedged BlueZ can't stall startup.  A short budget is fine:
        # if BlueZ doesn't ack quickly, the handle wasn't really held.
        import asyncio

        await asyncio.wait_for(
            asyncio.to_thread(mgr.disconnect_device, mac_address),
            timeout=5.0,
        )
        logger.info(
            "Pre-disconnected stale Bluetooth handle for %s on startup",
            mac_address,
        )
    except TimeoutError:
        logger.warning(
            "Timed out releasing stale Bluetooth handle for %s; "
            "continuing startup anyway",
            mac_address,
        )
    except Exception as exc:
        # Most common cause: device wasn't connected — entirely fine.
        logger.debug(
            "Stale-handle release for %s skipped (%s); continuing startup",
            mac_address,
            exc,
        )
    finally:
        if mgr is not None:
            try:
                mgr.close()
            except Exception:
                logger.debug("BluetoothManager.close() raised; ignoring", exc_info=True)


async def _auto_start_with_retry(
    input_config: InputConfig,
    dest_configs: list[DestinationConfig],
) -> None:
    """Retry-with-backoff loop for the auto-start path.

    Runs as a background task scheduled by :func:`init_services`.  At
    each iteration:

    1. Sleeps for the scheduled delay (0 on the first pass).
    2. Bails out if the operator manually started the relay during
       the wait — they win the race.
    3. Tries :meth:`RelayService.start_relay`.  Permanent config-shape
       errors (``ValidationError`` / ``ConfigurationError``) fail fast
       — no amount of retrying will fix bad YAML.  All other errors
       are treated as transient (typical case: Bluetooth peer not yet
       reachable after a power cycle, USB-serial device not yet
       enumerated, NTRIP caster TCP timeout) and retried.

    The module-level :data:`auto_start_status` is updated at each
    state transition so the Dashboard banner can render the current
    attempt and last error.
    """
    last_error: str | None = None
    total_attempts = len(AUTO_START_BACKOFF_SECONDS)
    for attempt, delay in enumerate(AUTO_START_BACKOFF_SECONDS, start=1):
        if delay:
            await asyncio.sleep(delay)

        # Operator clicked Start during the backoff window — they win.
        if relay_service.is_running:
            _set_auto_start_status("succeeded_user", attempt - 1)
            logger.info(
                "Auto-start aborted: relay was started manually during backoff",
            )
            return

        _set_auto_start_status("in_progress", attempt, last_error)
        try:
            await relay_service.start_relay(input_config, dest_configs)
        except (ValidationError, ConfigurationError) as exc:
            # Permanent — config is malformed; retrying won't help.
            last_error = str(exc)
            _set_auto_start_status("failed_config", attempt, last_error)
            logger.error(
                "Auto-start blocked by config error (no retry): %s", last_error
            )
            return
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                "Auto-start attempt %d/%d failed: %s",
                attempt,
                total_attempts,
                last_error,
            )
            continue

        # Success — bring up the event bridge and we're done.
        event_bridge.start(relay_service)
        _set_auto_start_status("succeeded", attempt)
        logger.info(
            "Auto-started relay engine on attempt %d/%d", attempt, total_attempts
        )
        return

    _set_auto_start_status("failed_after_retries", total_attempts, last_error)
    logger.error(
        "Auto-start failed after %d attempts; last error: %s",
        total_attempts,
        last_error,
    )


async def init_services() -> None:
    """Initialize all services and optionally schedule auto-start.

    Loads the configuration from disk and wires up the device-service ↔
    relay mutual-exclusion check.  If ``auto_start`` is enabled and an
    input source is configured, schedules :func:`_auto_start_with_retry`
    as a background task — the function itself returns promptly so the
    rest of application startup (FastAPI routes, NiceGUI pages) is not
    blocked while the relay engine tries to come up.

    Before scheduling auto-start, also releases any stale Bluetooth
    handle a previous unclean shutdown may have left behind (see
    :func:`_release_stale_bluetooth_handle`).
    """
    global relay_service, config_service, event_bridge, device_service
    global auto_start_task

    # Load config from disk (creates default if missing)
    config = config_service.load_config()
    logger.info("Services initialized — config loaded")

    # Wire up device service ↔ relay mutual exclusion
    device_service.set_relay_check(lambda: relay_service.is_running)

    settings = config.settings
    if not settings.auto_start:
        return

    if config.input is None:
        _set_auto_start_status("skipped_no_input", 0)
        logger.info(
            "Auto-start enabled but no input source configured — skipping",
        )
        return

    # Bug D — best-effort: if a previous instance exited uncleanly
    # we may need to ask BlueZ to drop a stale handle before the
    # relay engine tries to claim the same RFCOMM channel.
    if config.input.source == "bluetooth":
        mac = config.input.config.get("mac_address") or config.input.config.get(
            "address"
        )
        if isinstance(mac, str) and mac:
            await _release_stale_bluetooth_handle(mac)

    dest_configs = [d.to_relay_config() for d in config.destinations if d.enabled]
    input_config = config.input.to_relay_config()

    # Schedule the retry loop as a background task.  Hold the
    # reference at module scope so it isn't GC'd and so tests can
    # ``await`` it to synchronise on completion.
    auto_start_task = asyncio.create_task(
        _auto_start_with_retry(input_config, dest_configs),
        name="sp_rtk_base.auto_start",
    )
