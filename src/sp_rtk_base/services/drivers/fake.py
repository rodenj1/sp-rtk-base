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
    ALL_RTCM_MESSAGE_IDS as _ALL_RTCM_IDS,
)
from sp_rtk_base.models.device_models import (
    BaseMode,
    CurrentBaseConfig,
    DeviceCapability,
    DeviceInfo,
    DynModel,
    FixedBaseConfig,
    GnssConfig,
    GnssConstellation,
    GnssSystemConfig,
    GpsFixType,
    GpsPosition,
    PortId,
    PortProtocolConfig,
    RtcmPortConfig,
    RtcmRowId,
    SerialPortInfo,
    SurveyInConfig,
    SurveyInProgress,
    UbxProtocol,
)
from sp_rtk_base.models.hardware_identity import HARDWARE_UNKNOWN, HardwareConfidence
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

# Sentinel ``port`` value the e2e suite can pass to ``connect()`` to make
# the fake driver report an unresolved hardware identity instead of its
# normal confirmed ZED-F9P — the only way to reach the GPS page's
# "unconfirmed hardware" picker banner without real hardware.
FAKE_UNKNOWN_HW_PORT: str = "FAKE-UNKNOWN-HW"


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
        # hardware_target/confidence default to a *confirmed* ZED-F9P —
        # this driver already mirrors that receiver's reference RTCM/port
        # profile (see class docstring), and the GPS page's profile
        # picker needs a confirmed identity to exercise its "suggested
        # default" path. ``connect(FAKE_UNKNOWN_HW_PORT, ...)`` overrides
        # this to unknown/unknown for the picker's other e2e path.
        self._device_info: DeviceInfo = DeviceInfo(
            vendor="Fake",
            model="FAKE-F9P",
            firmware_version="FAKE 1.0",
            protocol_version="27.99",
            hardware_version="FAKE-HW",
            serial_number="FAKE-0001",
            hardware_target="ZED-F9P",
            hardware_confidence=HardwareConfidence.CONFIRMED,
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
        self._rtcm_ports: RtcmPortConfig = RtcmPortConfig(
            messages={
                msg_id: {"USB": 1, "UART1": 1, "UART2": 0, "I2C": 0, "SPI": 0}
                for msg_id in (
                    RtcmRowId.RTCM_1005,
                    RtcmRowId.RTCM_1077,
                    RtcmRowId.RTCM_1087,
                    RtcmRowId.RTCM_1097,
                    RtcmRowId.RTCM_1127,
                    RtcmRowId.RTCM_1230,
                )
            }
        )

        # Port protocols — mirrors the reference base's RTCM-only
        # UART1/UART2 output profile (docs/zed-f9p-base-station-config-
        # reference.md); USB is left at factory defaults (all three
        # protocols both ways) since it's the local diagnostics port.
        self._port_protocols: PortProtocolConfig = PortProtocolConfig(
            in_protocols={
                PortId.UART1: [
                    UbxProtocol.UBX,
                    UbxProtocol.NMEA,
                    UbxProtocol.RTCM3X,
                ],
                PortId.UART2: [UbxProtocol.UBX, UbxProtocol.RTCM3X],
                PortId.USB: [UbxProtocol.UBX, UbxProtocol.NMEA, UbxProtocol.RTCM3X],
            },
            out_protocols={
                PortId.UART1: [UbxProtocol.RTCM3X],
                PortId.UART2: [UbxProtocol.RTCM3X],
                PortId.USB: [UbxProtocol.UBX, UbxProtocol.NMEA, UbxProtocol.RTCM3X],
            },
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

        # Apply-config primitives (issue #61) — plain in-memory state,
        # defaults chosen to match the shipped built-in profile
        # (docs/zed-f9p-base-station-config-reference.md).
        self._dyn_model: DynModel = DynModel.PORTABLE
        self._elevation_mask_deg: int = 15
        self._bds_b2_enabled: bool = False
        self._spi_enabled: bool = True
        self._meas_period_ms: int = 1000
        self._uart_baud_rates: dict[PortId, int] = {
            PortId.UART1: 57600,
            PortId.UART2: 115200,
        }

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
        if port == FAKE_UNKNOWN_HW_PORT:
            self._device_info = self._device_info.model_copy(
                update={
                    "hardware_target": HARDWARE_UNKNOWN,
                    "hardware_confidence": HardwareConfidence.UNKNOWN,
                }
            )
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

    def disable_base_mode(self) -> None:
        """Disable base mode (e.g. cancel an in-progress survey-in).

        Resets state to ``BaseMode.DISABLED`` and clears the
        survey-in clock so subsequent ``get_survey_in_status()`` calls
        report ``active=False, valid=False``.
        """
        self._ensure_connected()
        self._survey_started_at = None
        self._base_config = CurrentBaseConfig(
            mode=BaseMode.DISABLED,
            latitude=0.0,
            longitude=0.0,
            altitude_m=0.0,
            accuracy_mm=0,
        )

    def get_rtcm_port_config(self) -> RtcmPortConfig:
        """Return the most recently stored per-port RTCM config."""
        self._ensure_connected()
        return self._rtcm_ports

    def configure_rtcm_ports(self, config: RtcmPortConfig) -> None:
        """Store the per-port RTCM config in memory."""
        self._ensure_connected()
        self._rtcm_ports = config

    def get_port_protocols(self) -> PortProtocolConfig:
        """Return the fixed in-memory port protocol state."""
        self._ensure_connected()
        return self._port_protocols

    def save_to_flash(self) -> None:
        """No-op — fake driver has no flash memory.

        Successful save is implied by the lack of an exception.
        Real drivers might raise on NAK; the fake never does.
        """
        self._ensure_connected()

    # ------------------------------------------------------------------
    # Apply-config primitives (issue #61)
    # ------------------------------------------------------------------

    def configure_port_protocols(
        self,
        in_protocols: dict[PortId, list[UbxProtocol]],
        out_protocols: dict[PortId, list[UbxProtocol]],
    ) -> None:
        """Store the per-port protocol write in memory.

        Only ports present in either mapping are touched, mirroring
        ``UbloxDriver``'s assertive-per-touched-port semantics.
        """
        self._ensure_connected()
        new_in = dict(self._port_protocols.in_protocols)
        new_out = dict(self._port_protocols.out_protocols)
        for port, protocols in in_protocols.items():
            new_in[port] = list(protocols)
        for port, protocols in out_protocols.items():
            new_out[port] = list(protocols)
        self._port_protocols = PortProtocolConfig(
            in_protocols=new_in, out_protocols=new_out
        )

    def configure_measurement_rate(self, period_ms: int) -> None:
        """Store the measurement period in memory."""
        self._ensure_connected()
        self._meas_period_ms = period_ms

    def configure_dyn_model(self, model: DynModel) -> None:
        """Store the dynamics model in memory."""
        self._ensure_connected()
        self._dyn_model = model

    def get_dyn_model(self) -> DynModel:
        """Return the most recently stored dynamics model."""
        self._ensure_connected()
        return self._dyn_model

    def configure_tmode_mode(self, mode: BaseMode) -> None:
        """Store the TMODE mode, leaving any existing position untouched."""
        self._ensure_connected()
        self._base_config = self._base_config.model_copy(update={"mode": mode})

    def configure_optimisations(
        self,
        elevation_mask_deg: int | None,
        bds_b2_enabled: bool | None,
        spi_enabled: bool | None,
    ) -> None:
        """Store only the optimisation fields provided."""
        self._ensure_connected()
        if elevation_mask_deg is not None:
            self._elevation_mask_deg = elevation_mask_deg
        if bds_b2_enabled is not None:
            self._bds_b2_enabled = bds_b2_enabled
        if spi_enabled is not None:
            self._spi_enabled = spi_enabled

    def apply_rtcm_matrix(self, matrix: dict[RtcmRowId, dict[PortId, bool]]) -> None:
        """Store the assertive 12x3 RTCM matrix write in memory.

        I2C/SPI columns (which the matrix doesn't claim) are preserved
        from whatever the fake driver already held for them.
        """
        self._ensure_connected()
        updated: dict[RtcmRowId, dict[str, int]] = {}
        for row in _ALL_RTCM_IDS:
            cell = dict(self._rtcm_ports.messages.get(row, {}))
            for port in (PortId.UART1, PortId.UART2, PortId.USB):
                cell[port.value] = int(matrix.get(row, {}).get(port, False))
            cell.setdefault("I2C", 0)
            cell.setdefault("SPI", 0)
            updated[row] = cell
        self._rtcm_ports = RtcmPortConfig(messages=updated)

    def get_uart_baud_rates(self) -> dict[PortId, int]:
        """Return the fixed in-memory UART baud rates."""
        self._ensure_connected()
        return dict(self._uart_baud_rates)

    def configure_baud(self, uart1: int | None, uart2: int | None) -> None:
        """Store only the UART baud fields provided (issue #62)."""
        self._ensure_connected()
        if uart1 is not None:
            self._uart_baud_rates[PortId.UART1] = uart1
        if uart2 is not None:
            self._uart_baud_rates[PortId.UART2] = uart2

    def reconnect_at_baud(self, baud_rate: int) -> DeviceInfo:
        """Simulate reopening at a new baud — no I/O, always succeeds."""
        self._ensure_connected()
        self._baud_rate = baud_rate
        return self._device_info

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
