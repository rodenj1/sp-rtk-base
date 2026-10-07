"""SP-Base business logic services.

Provides singleton service instances and initialization for the
application's service layer:

- ``ConfigService`` — YAML configuration persistence
- ``RelayService`` — async wrapper around sp-rtk-base-relay RelayEngine,
  including its live event streams
- ``MetricsService`` — Prometheus metrics from RelayStatus
- ``DeviceService`` — GPS receiver connection & configuration (optional)
- ``ProfileStore`` — GPS receiver profile persistence (built-in + custom)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from pydantic import ValidationError
from sp_rtk_base_relay.config import DestinationConfig, InputConfig
from sp_rtk_base_relay.exceptions import ConfigurationError

from sp_rtk_base.services.bluetooth_service import BluetoothVerificationService
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.correction_verification import (
    CorrectionSourceVerificationService,
)
from sp_rtk_base.services.device_service import BluetoothOpenerFactory, DeviceService
from sp_rtk_base.services.drivers.bluetooth_link import BluetoothLinkOpener
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.network_service import NetworkService
from sp_rtk_base.services.profile_store import ProfileStore
from sp_rtk_base.services.relay_service import RelayService
from sp_rtk_base.services.signal_quality.service import SignalQualityService
from sp_rtk_base.services.survey_service import (
    FIXED_SETTLE_S,
    STALL_ABORT_S,
    STALL_WARNING_S,
    SurveyService,
)

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
metrics_service: MetricsService = MetricsService()


def _bluetooth_opener() -> BluetoothOpenerFactory:
    """The Bluetooth Console link's opener: the real one, or the fake GPS's.

    The fake GPS (e2e) reaches a Bluetooth module with no BlueZ behind it.
    """
    if os.environ.get("SP_RTK_BASE_FAKE_GPS") == "1":
        from sp_rtk_base.services.drivers.fake import FakeBluetoothLinkOpener

        return FakeBluetoothLinkOpener
    return BluetoothLinkOpener


# A Bluetooth Console link uses the saved Input profile's device; read
# through the module global so a reloaded ConfigService is honoured.
device_service: DeviceService = DeviceService(
    input_profile=lambda: config_service.get_input_config(),
    bluetooth_opener=_bluetooth_opener(),
)
network_service: NetworkService = NetworkService()
profile_store: ProfileStore = ProfileStore()
signal_quality_service: SignalQualityService = SignalQualityService(device_service)


def _fake_gps_timing(env: str, default: float) -> float:
    """A survey timing (stall, settling); only the fake GPS (e2e) may shorten it."""
    value = os.environ.get(env)
    if os.environ.get("SP_RTK_BASE_FAKE_GPS") != "1" or not value:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("Ignoring %s=%r: not a number of seconds", env, value)
        return default


survey_service: SurveyService = SurveyService(
    device_service,
    stall_warning_s=_fake_gps_timing(
        "SP_RTK_BASE_FAKE_STALL_WARNING_S", STALL_WARNING_S
    ),
    stall_abort_s=_fake_gps_timing("SP_RTK_BASE_FAKE_STALL_ABORT_S", STALL_ABORT_S),
    fixed_settle_s=_fake_gps_timing("SP_RTK_BASE_FAKE_FIXED_SETTLE_S", FIXED_SETTLE_S),
)
correction_verification_service: CorrectionSourceVerificationService = (
    CorrectionSourceVerificationService()
)


def wire_corrected_survey(
    survey: SurveyService,
    config: ConfigService,
    verifier: CorrectionSourceVerificationService,
    signal_quality: SignalQualityService,
) -> None:
    """While a Corrected survey-in runs, its Correction source can't be
    renamed or deleted, a Verification is refused (issue #196), and Signal
    Quality stops polling the receiver, leaving the link to the survey and
    its corrections (#197)."""
    config.set_correction_source_in_use_check(survey.correction_source_in_use)
    verifier.set_survey_running_check(survey.corrected_survey_running)
    signal_quality.set_pause_check(survey.corrected_survey_running)


wire_corrected_survey(
    survey_service,
    config_service,
    correction_verification_service,
    signal_quality_service,
)
# Signal Quality reads the Relay's MSM while it runs (every start path).
relay_service.set_frame_subscriber(signal_quality_service)
bluetooth_verification_service: BluetoothVerificationService = (
    BluetoothVerificationService(
        relay_service=relay_service,
        config_service=config_service,
    )
)


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


def get_correction_verification_service() -> CorrectionSourceVerificationService:
    """Get the singleton Correction source Verification service.

    A singleton so that "one Verification at a time" holds process-wide.
    """
    return correction_verification_service


def get_survey_service() -> SurveyService:
    """Get the singleton SurveyService instance.

    A singleton because a survey outlives the page that started it: the
    station's own averaging commits the fixed base with no page open.
    """
    return survey_service


def get_signal_quality_service() -> SignalQualityService:
    """Get the singleton SignalQualityService instance.

    Returns:
        The application's SignalQualityService instance.
    """
    return signal_quality_service


def get_network_service() -> NetworkService:
    """Get the singleton NetworkService instance.

    Returns:
        The application's NetworkService instance.
    """
    return network_service


def get_bluetooth_verification_service() -> BluetoothVerificationService:
    """Get the singleton BluetoothVerificationService instance.

    A singleton because the Proven-PIN memo and the one-at-a-time slot
    are process-wide facts: a per-request service would forget what it
    had proven and would let two Verifications race BlueZ's default
    agent.

    Returns:
        The application's BluetoothVerificationService instance.
    """
    return bluetooth_verification_service


def get_profile_store() -> ProfileStore:
    """Get the singleton ProfileStore instance.

    Returns:
        The application's ProfileStore instance.
    """
    return profile_store


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def wire_console_relay_exclusion(device: DeviceService, relay: RelayService) -> None:
    """Make the console and the Relay exclude each other in both directions.

    Connect refuses while the Relay runs; Start refuses while the console
    is connected.
    """
    device.set_relay_check(lambda: relay.is_running)
    relay.set_console_check(lambda: device.is_connected)


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
            await relay_service.start_relay(
                input_config,
                dest_configs,
                trigger=f"auto-start (attempt {attempt})",
                refuse_while_console_connected=False,
            )
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

        # Success.
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

    The stale Bluetooth handle a previous unclean shutdown may have
    left behind is *not* released here any more: that now happens in
    :meth:`RelayService.start_relay`, so every path into the relay gets
    it rather than auto-start alone.
    """
    global relay_service, config_service, device_service
    global auto_start_task

    # Load config from disk (creates default if missing)
    config = config_service.load_config()
    logger.info("Services initialized — config loaded")

    wire_console_relay_exclusion(device_service, relay_service)

    settings = config.settings
    if not settings.auto_start:
        return

    if config.input is None:
        _set_auto_start_status("skipped_no_input", 0)
        logger.info(
            "Auto-start enabled but no input source configured — skipping",
        )
        return

    # A saved config the Relay can't run (e.g. an NTRIP v2 output without
    # a username, issue #198) is reported, not raised: startup carries on
    # and the Dashboard shows why the relay didn't start.
    try:
        dest_configs = [d.to_relay_config() for d in config.destinations if d.enabled]
        input_config = config.input.to_relay_config()
    except (ConfigurationError, ValidationError, ValueError) as exc:
        _set_auto_start_status("failed_config", 0, str(exc))
        logger.error("Auto-start skipped: the saved configuration can't run: %s", exc)
        return

    # Schedule the retry loop as a background task.  Hold the
    # reference at module scope so it isn't GC'd and so tests can
    # ``await`` it to synchronise on completion.
    auto_start_task = asyncio.create_task(
        _auto_start_with_retry(input_config, dest_configs),
        name="sp_rtk_base.auto_start",
    )
