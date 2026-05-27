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

import logging

from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.event_bridge import EventBridge
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.relay_service import RelayService

logger = logging.getLogger(__name__)

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


async def init_services() -> None:
    """Initialize all services and optionally auto-start the relay.

    Loads the configuration from disk. If ``auto_start`` is enabled
    and both input config and destinations are configured, starts
    the relay engine and event bridge automatically.

    Before auto-starting, this also releases any stale handles a
    previous unclean shutdown may have left behind (currently:
    Bluetooth — see :func:`_release_stale_bluetooth_handle`).
    """
    global relay_service, config_service, event_bridge, device_service

    # Load config from disk (creates default if missing)
    config = config_service.load_config()
    logger.info("Services initialized — config loaded")

    # Wire up device service ↔ relay mutual exclusion
    device_service.set_relay_check(lambda: relay_service.is_running)

    # Auto-start relay if configured
    settings = config.settings
    if settings.auto_start and config.input is not None:
        # Bug D — best-effort: if a previous instance exited uncleanly
        # we may need to ask BlueZ to drop a stale handle before the
        # relay engine tries to claim the same RFCOMM channel.
        if config.input.source == "bluetooth":
            mac = config.input.config.get("mac_address") or config.input.config.get(
                "address"
            )
            if isinstance(mac, str) and mac:
                await _release_stale_bluetooth_handle(mac)

        destinations_configs = [
            d.to_relay_config() for d in config.destinations if d.enabled
        ]
        input_config = config.input.to_relay_config()

        try:
            await relay_service.start_relay(input_config, destinations_configs)
            logger.info("Auto-started relay engine")

            # Start event bridge after relay is running
            event_bridge.start(relay_service)
            logger.info("Auto-started event bridge")
        except Exception:
            logger.exception("Failed to auto-start relay engine")
