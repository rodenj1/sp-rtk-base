"""In-memory fake GPS receiver driver for E2E + dev-mode testing.

This driver is **only registered when** ``SP_RTK_BASE_FAKE_GPS=1`` is
present in the environment (see ``services/drivers/__init__.py``).
Production builds never expose it.

Purpose
-------
The Playwright e2e suite needs to drive UI paths that are gated on
``device_service.is_connected`` — the Survey-In page, GPS Config page,
Dashboard GPS card, etc.  Without a real serial port + ZED-F9P those
paths are unreachable.  ``FakeGpsDriver`` provides a stand-in:

- ``connect("FAKE", ...)`` always succeeds and returns a deterministic
  ``DeviceInfo`` for the ``FAKE-F9P`` "model".
- All configuration writes (survey-in, fixed-base, RTCM, GNSS) are
  accepted and round-trip on subsequent reads.
- ``get_position()`` returns a fixed RTK-fixed solution at the values
  from the May-26 2026 Save-Position bug report so that test names
  stay traceable to the original regression.
- ``get_survey_in_status()`` simulates a survey-in that auto-completes
  in ~3 seconds with a deterministic accuracy-convergence curve.

Design constraints
------------------
- Implements **all** abstract methods of ``GpsReceiverDriver``.
- No I/O — pure Python state.  Safe to instantiate anywhere, anytime.
- Deterministic — every call yields the same answer given the same
  prior state.  Survey-in uses ``time.monotonic()`` for elapsed-time
  computation; callers control timing by ``configure_survey_in()``.
- Hidden from production — never registered unless the env var is
  set, so unrelated tests and production deployments don't see it.

The 2026-05-26 Save-Position-dialog bug values
-----------------------------------------------
These are intentionally hard-coded so the saved-position dialog test
asserts on the *exact* numbers from the original report:

- latitude  = 32.7329015°
- longitude = -117.2362788°
- altitude  = 27.940 m
- accuracy  = 47308 mm
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from sp_rtk_base.models.device_models import (
    BaseMode,
    CurrentBaseConfig,
    DeviceCapability,
    DeviceInfo,
    FixedBaseConfig,
    GnssConfig,
    GnssConstellation,
    GnssSystemConfig,
    GpsFixType,
    GpsPosition,
    RtcmMessageConfig,
    RtcmPortConfig,
    SerialPortInfo,
    SurveyInConfig,
    SurveyInProgress,
)
from sp_rtk_base.services.drivers.base import GpsReceiverDriver

# ---------------------------------------------------------------------------
# Hard-coded fixtures (see module docstring for rationale)
# ---------------------------------------------------------------------------

# Position the fake driver always reports.  Chosen to match the
# 2026-05-26 Save-Position bug report so the regression test
# assertions stay traceable.
_FAKE_LAT: float = 32.7329015
_FAKE_LON: float = -117.2362788
_FAKE_ALT_M: float = 27.940
_FAKE_ACC_MM: int = 47308

# Survey-in target accuracy convergence: start at 5000 mm and decay
# linearly so a 3-second survey crosses any plausible accuracy_limit.
_SURVEY_START_ACCURACY_MM: float = 5000.0
_SURVEY_DECAY_PER_SECOND: float = 1500.0  # mm/s

# Fake survey-in duration cap — auto-complete after this many seconds
# regardless of the configured threshold so e2e tests stay snappy.
_SURVEY_FAST_COMPLETE_SECONDS: float = 3.0

# Identifier the UI uses to find the fake "serial port".
FAKE_PORT_LABEL: str = "FAKE"


class FakeGpsDriver(GpsReceiverDriver):
    """In-memory GPS receiver driver for E2E + dev-mode testing.

    State is per-instance.  The driver registry creates one instance
    per ``create_driver("fake")`` call, so each ``DeviceService``
    invocation gets a fresh starting state.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the fake driver in a disconnected state."""
        self._connected: bool = False
        self._port: str | None = None
        self._baud_rate: int | None = None

        # Identity returned by ``connect()`` / ``get_device_info()``.
        self._device_info: DeviceInfo = DeviceInfo(
            vendor="Fake",
            model="FAKE-F9P",
            firmware_version="FAKE 1.0",
            protocol_version="27.99",
            hardware_version="FAKE-HW",
            serial_number="FAKE-0001",
        )

        # Base config — starts disabled.  Switches to SURVEY_IN /
        # FIXED on the corresponding configure calls.
        self._base_config: CurrentBaseConfig = CurrentBaseConfig(
            mode=BaseMode.DISABLED,
            latitude=0.0,
            longitude=0.0,
            altitude_m=0.0,
            accuracy_mm=0,
        )

        # Survey-in clock.  Starts None — survey is not running.
        # Populated by ``configure_survey_in()``.
        self._survey_started_at: float | None = None
        self._survey_threshold_mm: int = 50000
        self._survey_min_duration_s: int = 60

        # RTCM configuration — initialised with sensible defaults.
        self._rtcm_msgs: RtcmMessageConfig = RtcmMessageConfig()
        self._rtcm_ports: RtcmPortConfig = RtcmPortConfig(
            messages={
                1005: {"USB": 1, "UART1": 1, "UART2": 0, "I2C": 0, "SPI": 0},
                1077: {"USB": 1, "UART1": 1, "UART2": 0, "I2C": 0, "SPI": 0},
                1087: {"USB": 1, "UART1": 1, "UART2": 0, "I2C": 0, "SPI": 0},
                1097: {"USB": 1, "UART1": 1, "UART2": 0, "I2C": 0, "SPI": 0},
                1127: {"USB": 1, "UART1": 1, "UART2": 0, "I2C": 0, "SPI": 0},
                1230: {"USB": 1, "UART1": 1, "UART2": 0, "I2C": 0, "SPI": 0},
            }
        )

        # GNSS — all six constellations enabled by default.
        self._gnss: GnssConfig = GnssConfig(
            systems=[
                GnssSystemConfig(
                    constellation=GnssConstellation.GPS,
                    enabled=True,
                    min_channels=8,
                    max_channels=16,
                    sig_cfg_mask=0x01,
                ),
                GnssSystemConfig(
                    constellation=GnssConstellation.GLONASS,
                    enabled=True,
                    min_channels=8,
                    max_channels=14,
                    sig_cfg_mask=0x01,
                ),
                GnssSystemConfig(
                    constellation=GnssConstellation.GALILEO,
                    enabled=True,
                    min_channels=4,
                    max_channels=12,
                    sig_cfg_mask=0x21,
                ),
                GnssSystemConfig(
                    constellation=GnssConstellation.BEIDOU,
                    enabled=True,
                    min_channels=8,
                    max_channels=16,
                    sig_cfg_mask=0x11,
                ),
                GnssSystemConfig(
                    constellation=GnssConstellation.SBAS,
                    enabled=False,
                    min_channels=1,
                    max_channels=3,
                    sig_cfg_mask=0x01,
                ),
                GnssSystemConfig(
                    constellation=GnssConstellation.QZSS,
                    enabled=False,
                    min_channels=0,
                    max_channels=3,
                    sig_cfg_mask=0x01,
                ),
            ]
        )

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def vendor_name(self) -> str:
        """Return the human-readable vendor name."""
        return "Fake"

    def get_capabilities(self) -> set[DeviceCapability]:
        """Fake driver claims to support every capability in the model.

        This keeps every UI path reachable during e2e — none of the
        capability-gated sections will be hidden.
        """
        return {
            DeviceCapability.SURVEY_IN,
            DeviceCapability.FIXED_BASE,
            DeviceCapability.RTCM_MESSAGE_SELECT,
            DeviceCapability.SAVE_TO_FLASH,
            DeviceCapability.POSITION_STREAM,
            DeviceCapability.SATELLITE_INFO,
            DeviceCapability.GNSS_SELECT,
        }

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self, port: str, baud_rate: int = 115200) -> DeviceInfo:
        """Accept the connection request unconditionally.

        Args:
            port: Ignored — fake driver does no I/O.  Stored on the
                instance so ``DeviceService`` status responses look
                realistic.
            baud_rate: Ignored, stored for status display.

        Returns:
            Deterministic :class:`DeviceInfo` for the fake receiver.
        """
        self._connected = True
        self._port = port
        self._baud_rate = baud_rate
        return self._device_info

    def disconnect(self) -> None:
        """Mark the driver disconnected.  Safe when already disconnected."""
        self._connected = False
        self._port = None
        self._baud_rate = None
        # Survey-in is implicitly aborted by disconnect.
        self._survey_started_at = None

    @property
    def is_connected(self) -> bool:
        """Return whether ``connect()`` has been called without a matching ``disconnect()``."""
        return self._connected

    # ------------------------------------------------------------------
    # Base station configuration
    # ------------------------------------------------------------------

    def configure_survey_in(self, config: SurveyInConfig) -> None:
        """Start a fake survey-in.

        Captures the threshold + minimum duration and starts the
        monotonic clock.  ``get_survey_in_status()`` reads this state
        to synthesise an accuracy-convergence curve.
        """
        self._ensure_connected()
        self._survey_threshold_mm = config.accuracy_limit_mm
        self._survey_min_duration_s = config.min_duration_seconds
        self._survey_started_at = time.monotonic()
        self._base_config = CurrentBaseConfig(
            mode=BaseMode.SURVEY_IN,
            latitude=0.0,
            longitude=0.0,
            altitude_m=0.0,
            accuracy_mm=0,
        )

    def configure_fixed_base(self, config: FixedBaseConfig) -> None:
        """Switch the fake receiver into fixed-base mode."""
        self._ensure_connected()
        self._survey_started_at = None
        self._base_config = CurrentBaseConfig(
            mode=BaseMode.FIXED,
            pos_type="llh",
            latitude=config.latitude,
            longitude=config.longitude,
            altitude_m=config.altitude_m,
            accuracy_mm=config.accuracy_mm,
        )

    def configure_rtcm_messages(self, config: RtcmMessageConfig) -> None:
        """Store the simple RTCM message config in memory."""
        self._ensure_connected()
        self._rtcm_msgs = config

    def get_rtcm_config(self) -> RtcmMessageConfig:
        """Return the most recently stored simple RTCM config."""
        self._ensure_connected()
        return self._rtcm_msgs

    def get_rtcm_port_config(self) -> RtcmPortConfig:
        """Return the most recently stored per-port RTCM config."""
        self._ensure_connected()
        return self._rtcm_ports

    def configure_rtcm_ports(self, config: RtcmPortConfig) -> None:
        """Store the per-port RTCM config in memory."""
        self._ensure_connected()
        self._rtcm_ports = config

    def save_to_flash(self) -> None:
        """No-op — fake driver has no flash memory.

        Successful save is implied by the lack of an exception.
        Real drivers might raise on NAK; the fake never does.
        """
        self._ensure_connected()

    # ------------------------------------------------------------------
    # GNSS constellation configuration
    # ------------------------------------------------------------------

    def get_gnss_config(self) -> GnssConfig:
        """Return the most recently stored GNSS configuration."""
        self._ensure_connected()
        return self._gnss

    def configure_gnss(self, config: GnssConfig) -> None:
        """Store the GNSS configuration in memory."""
        self._ensure_connected()
        self._gnss = config

    # ------------------------------------------------------------------
    # Status polling
    # ------------------------------------------------------------------

    def get_position(self) -> GpsPosition:
        """Return a deterministic RTK-fixed solution.

        The lat/lon/alt match the May-26 2026 Save-Position bug values
        so e2e regression tests on the dialog stay traceable.
        """
        self._ensure_connected()
        return GpsPosition(
            fix_type=GpsFixType.FIX_3D,
            rtk_status="fixed",
            latitude=_FAKE_LAT,
            longitude=_FAKE_LON,
            altitude_m=_FAKE_ALT_M,
            altitude_msl_m=_FAKE_ALT_M - 33.0,
            horizontal_accuracy_m=0.014,
            vertical_accuracy_m=0.021,
            num_satellites=24,
            speed_m_s=0.0,
            heading_deg=0.0,
            pdop=0.8,
            timestamp=datetime.now(timezone.utc),
        )

    def get_survey_in_status(self) -> SurveyInProgress:
        """Synthesise a survey-in progress snapshot.

        - If no survey has been started: ``active=False, valid=False``.
        - If a survey was started: linearly decreasing accuracy from
          5000 mm towards 0 at 1500 mm/s, observation count = elapsed
          seconds × 4.  The survey completes when *both* the elapsed
          duration exceeds ``min_duration_seconds`` AND the modelled
          accuracy drops below ``accuracy_limit_mm`` — OR when
          ``_SURVEY_FAST_COMPLETE_SECONDS`` is reached, whichever
          comes first.  Fast-complete keeps e2e tests bounded.
        """
        self._ensure_connected()
        if self._survey_started_at is None:
            return SurveyInProgress(active=False, valid=False)

        elapsed = time.monotonic() - self._survey_started_at
        elapsed_s = max(0, int(elapsed))
        modelled_mm = max(
            0.0, _SURVEY_START_ACCURACY_MM - _SURVEY_DECAY_PER_SECOND * elapsed
        )
        observations = elapsed_s * 4

        complete_by_threshold = (
            elapsed >= self._survey_min_duration_s
            and modelled_mm <= self._survey_threshold_mm
        )
        complete_by_fast_path = elapsed >= _SURVEY_FAST_COMPLETE_SECONDS

        if complete_by_threshold or complete_by_fast_path:
            # Auto-promote: survey done, populate the position from the
            # hard-coded fixtures so the UI's "promote to fixed base"
            # path has real numbers to work with.
            self._base_config = CurrentBaseConfig(
                mode=BaseMode.FIXED,
                pos_type="llh",
                latitude=_FAKE_LAT,
                longitude=_FAKE_LON,
                altitude_m=_FAKE_ALT_M,
                accuracy_mm=_FAKE_ACC_MM,
            )
            return SurveyInProgress(
                active=False,
                valid=True,
                duration_seconds=elapsed_s,
                mean_accuracy_mm=float(_FAKE_ACC_MM),
                observations=observations,
                latitude=_FAKE_LAT,
                longitude=_FAKE_LON,
                altitude_m=_FAKE_ALT_M,
            )

        return SurveyInProgress(
            active=True,
            valid=False,
            duration_seconds=elapsed_s,
            mean_accuracy_mm=modelled_mm,
            observations=observations,
            latitude=None,
            longitude=None,
            altitude_m=None,
        )

    def get_device_info(self) -> DeviceInfo:
        """Return the same identity ``connect()`` returned."""
        self._ensure_connected()
        return self._device_info

    def get_base_config(self) -> CurrentBaseConfig:
        """Return the most recently stored base configuration."""
        self._ensure_connected()
        return self._base_config

    # ------------------------------------------------------------------
    # Port discovery
    # ------------------------------------------------------------------

    @staticmethod
    def list_serial_ports() -> list[SerialPortInfo]:
        """Expose a single synthetic "FAKE" port for the UI dropdown.

        This is **only** called when the fake driver is registered
        (i.e. when ``SP_RTK_BASE_FAKE_GPS=1``) — so the FAKE entry is
        never present in production.  Returning just the one entry
        keeps the dropdown simple in e2e tests; tests that want to
        exercise mixed serial-port discovery can mock
        ``serial.tools.list_ports`` instead.
        """
        return [
            SerialPortInfo(
                port=FAKE_PORT_LABEL,
                description="Fake GPS Receiver (e2e)",
                manufacturer="sp-rtk-base",
                vid=None,
                pid=None,
                serial_number="FAKE-0001",
                is_gps=True,
            )
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        """Raise ``ConnectionError`` if the driver is not connected.

        Mirrors the behaviour of :class:`UbloxDriver`, which raises
        the same error for any operation issued before ``connect()``.
        """
        if not self._connected:
            raise ConnectionError(
                "FakeGpsDriver is not connected — call connect() first"
            )


__all__ = ["FAKE_PORT_LABEL", "FakeGpsDriver"]
