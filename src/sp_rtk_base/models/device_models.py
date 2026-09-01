"""Device models — vendor-neutral GPS receiver data structures.

These models are used by the DeviceService and GPS receiver drivers
to represent device state, configuration, and capabilities without
coupling to any specific vendor (u-blox, Septentrio, etc.).
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field

from sp_rtk_base.models.hardware_identity import (
    HARDWARE_UNKNOWN,
    HardwareConfidence,
    HardwareTarget,
)

# ---------------------------------------------------------------------------
# Connection state
# ---------------------------------------------------------------------------


class DeviceConnectionState(str, enum.Enum):
    """GPS receiver connection states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CONFIGURING = "configuring"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Serial port discovery
# ---------------------------------------------------------------------------


class SerialPortInfo(BaseModel):
    """Information about an available serial port.

    Populated by ``serial.tools.list_ports`` for port discovery.
    """

    port: str = Field(description="Device path (e.g. /dev/ttyUSB0)")
    description: str = Field(default="", description="Human-readable description")
    manufacturer: str = Field(default="", description="Device manufacturer")
    vid: int | None = Field(default=None, description="USB vendor ID")
    pid: int | None = Field(default=None, description="USB product ID")
    serial_number: str = Field(default="", description="Device serial number")
    is_gps: bool = Field(
        default=False,
        description="Heuristic: likely a GPS receiver (known vendor ID)",
    )


# ---------------------------------------------------------------------------
# Device identity
# ---------------------------------------------------------------------------


class DeviceInfo(BaseModel):
    """Identity information read from a connected GPS receiver.

    Populated by the driver after a successful connect (e.g. MON-VER
    on u-blox, or equivalent on other vendors).
    """

    vendor: str = Field(description="Vendor/manufacturer name")
    model: str = Field(description="Receiver model (e.g. ZED-F9P)")
    firmware_version: str = Field(default="", description="Firmware version string")
    protocol_version: str = Field(default="", description="Protocol version string")
    hardware_version: str = Field(default="", description="Hardware version string")
    serial_number: str = Field(
        default="", description="Device serial number if available"
    )
    hardware_target: HardwareTarget = Field(
        default=HARDWARE_UNKNOWN,
        description="Resolved identity target: a specific model, a family "
        "token, or 'unknown' (see models.hardware_identity)",
    )
    hardware_confidence: HardwareConfidence = Field(
        default=HardwareConfidence.UNKNOWN,
        description="How hardware_target was obtained",
    )


# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------


class DeviceCapability(str, enum.Enum):
    """Capabilities that a GPS receiver driver may support.

    The UI uses these to show/hide features dynamically.
    """

    SURVEY_IN = "survey_in"
    FIXED_BASE = "fixed_base"
    RTCM_MESSAGE_SELECT = "rtcm_message_select"
    SAVE_TO_FLASH = "save_to_flash"
    BACKUP_RESTORE = "backup_restore"
    POSITION_STREAM = "position_stream"
    SATELLITE_INFO = "satellite_info"
    GNSS_SELECT = "gnss_select"


# ---------------------------------------------------------------------------
# Base station configuration
# ---------------------------------------------------------------------------


class SurveyInConfig(BaseModel):
    """Configuration for survey-in base station mode."""

    min_duration_seconds: int = Field(
        default=120,
        ge=60,
        le=86400,
        description="Minimum survey duration in seconds",
    )
    accuracy_limit_mm: int = Field(
        default=50000,
        ge=1000,
        le=500000,
        description="Required accuracy in mm (e.g. 50000 = 5.0m)",
    )


class FixedBaseConfig(BaseModel):
    """Configuration for fixed-position base station mode."""

    latitude: float = Field(
        description="WGS84 latitude in degrees",
        ge=-90.0,
        le=90.0,
    )
    longitude: float = Field(
        description="WGS84 longitude in degrees",
        ge=-180.0,
        le=180.0,
    )
    altitude_m: float = Field(
        description="Height above ellipsoid in meters",
        ge=-1000.0,
        le=100000.0,
    )
    accuracy_mm: int = Field(
        default=1000,
        ge=1,
        description="Position accuracy in mm",
    )


class BaseMode(str, enum.Enum):
    """Current base station operating mode."""

    DISABLED = "disabled"
    SURVEY_IN = "survey_in"
    FIXED = "fixed"


class DynModel(str, enum.Enum):
    """``CFG_NAVSPG_DYNMODEL`` — the receiver's dynamics platform model."""

    STATIONARY = "stationary"
    PORTABLE = "portable"
    PEDESTRIAN = "pedestrian"
    AUTOMOTIVE = "automotive"
    SEA = "sea"
    AIRBORNE_1G = "airborne_1g"
    AIRBORNE_2G = "airborne_2g"
    AIRBORNE_4G = "airborne_4g"


class CurrentBaseConfig(BaseModel):
    """Current base station configuration read from the receiver.

    Represents the live TMODE state — which mode is active and,
    if fixed, the configured coordinates.  Lat/lon/alt are always
    WGS84 regardless of the underlying storage format (ECEF or LLH).
    """

    mode: BaseMode = Field(description="Active base station mode")
    pos_type: str = Field(default="llh", description="Storage format: 'ecef' or 'llh'")
    latitude: float = Field(default=0.0, description="WGS84 latitude in degrees")
    longitude: float = Field(default=0.0, description="WGS84 longitude in degrees")
    altitude_m: float = Field(
        default=0.0, description="Height above ellipsoid in metres"
    )
    accuracy_mm: int = Field(
        default=0, description="Configured position accuracy in mm"
    )


class RtcmRowId(str, enum.Enum):
    """Row identity for an RTCM message output slot.

    The 12 rows this app can address, keyed by their RTCM/u-blox
    identity string rather than a bare message-type int. This exists
    because 4072 (u-blox reference-station PVT) has two independently
    controllable sub-messages, 4072.0 and 4072.1, which an ``int``
    cannot represent as distinct rows.
    """

    RTCM_1005 = "1005"
    RTCM_4072_0 = "4072.0"
    RTCM_4072_1 = "4072.1"
    RTCM_1074 = "1074"
    RTCM_1077 = "1077"
    RTCM_1084 = "1084"
    RTCM_1087 = "1087"
    RTCM_1094 = "1094"
    RTCM_1097 = "1097"
    RTCM_1124 = "1124"
    RTCM_1127 = "1127"
    RTCM_1230 = "1230"


class BaseInvariantsCheck(BaseModel):
    """Result of the pre-flight base-invariants check (issue #63).

    Non-blocking advisories only — ``warnings`` never prevents a
    survey-in from starting. Empty means the live receiver config
    already matches the built-in base profile's dynamics-model and
    RTCM-on-data-link-port invariants.
    """

    warnings: list[str] = Field(default_factory=lambda: list[str]())


class RtcmOutputPort(str, enum.Enum):
    """u-blox output ports for RTCM messages."""

    USB = "USB"
    UART1 = "UART1"
    UART2 = "UART2"
    I2C = "I2C"
    SPI = "SPI"


# RTCM row groups for UI display
RTCM_MESSAGE_GROUPS: list[tuple[str, list[tuple[RtcmRowId, str]]]] = [
    (
        "Reference",
        [
            (RtcmRowId.RTCM_1005, "Station ARP"),
            (RtcmRowId.RTCM_4072_0, "Ref station PVT (4072.0)"),
            (RtcmRowId.RTCM_4072_1, "Ref station PVT (4072.1)"),
        ],
    ),
    ("GPS", [(RtcmRowId.RTCM_1074, "MSM4"), (RtcmRowId.RTCM_1077, "MSM7")]),
    ("GLONASS", [(RtcmRowId.RTCM_1084, "MSM4"), (RtcmRowId.RTCM_1087, "MSM7")]),
    ("Galileo", [(RtcmRowId.RTCM_1094, "MSM4"), (RtcmRowId.RTCM_1097, "MSM7")]),
    ("BeiDou", [(RtcmRowId.RTCM_1124, "MSM4"), (RtcmRowId.RTCM_1127, "MSM7")]),
    ("GLONASS Bias", [(RtcmRowId.RTCM_1230, "Code-phase biases")]),
]

# All known RTCM row IDs (flat list)
ALL_RTCM_MESSAGE_IDS: list[RtcmRowId] = [
    msg_id for _, msgs in RTCM_MESSAGE_GROUPS for msg_id, _ in msgs
]


class RtcmPortConfig(BaseModel):
    """Multi-port RTCM output configuration.

    Stores per-row, per-port output rates.  A rate of 0 means
    the row is disabled on that port; rate > 0 means enabled
    at that many messages per navigation epoch.

    Example::

        config.messages = {
            RtcmRowId.RTCM_1005: {"USB": 1, "UART1": 0, "UART2": 1, "I2C": 0, "SPI": 0},
            RtcmRowId.RTCM_1077: {"USB": 1, "UART1": 1, "UART2": 0, "I2C": 0, "SPI": 0},
        }
    """

    messages: dict[RtcmRowId, dict[str, int]] = Field(
        default_factory=lambda: dict[RtcmRowId, dict[str, int]](),
        description="row id → {port: rate} mapping",
    )

    def enabled_on_port(self, port: RtcmOutputPort) -> list[RtcmRowId]:
        """Return row IDs enabled (rate > 0) on a given port."""
        return [
            msg_id
            for msg_id, ports in self.messages.items()
            if ports.get(port.value, 0) > 0
        ]

    def is_enabled(self, msg_id: RtcmRowId, port: RtcmOutputPort) -> bool:
        """Check if a specific row is enabled on a specific port."""
        return self.messages.get(msg_id, {}).get(port.value, 0) > 0

    def rate(self, msg_id: RtcmRowId, port: RtcmOutputPort) -> int:
        """Get the rate for a specific row on a specific port."""
        return self.messages.get(msg_id, {}).get(port.value, 0)


# ---------------------------------------------------------------------------
# Port protocol configuration
# ---------------------------------------------------------------------------


class PortId(str, enum.Enum):
    """Communication ports covered by live port-protocol reads."""

    UART1 = "UART1"
    UART2 = "UART2"
    USB = "USB"


class UbxProtocol(str, enum.Enum):
    """I/O protocol a u-blox port can be configured to speak."""

    UBX = "UBX"
    NMEA = "NMEA"
    RTCM3X = "RTCM3X"


# ---------------------------------------------------------------------------
# Apply-config's read-back-verify diff/result types now live on
# ``models.profile_models`` (``ApplyDiffEntry`` / ``ApplyConfigResult`` /
# ``diff_receiver_assertions`` — issue #98), since they compare
# ``ReceiverAssertion`` values and this module can't import that without
# a circular dependency. The old matrix-only ``ApplyConfigCellDiff`` /
# ``ApplyConfigResult`` pair that lived here is gone — no parallel type.
# ---------------------------------------------------------------------------


class PortProtocolConfig(BaseModel):
    """Live in/out protocol state for UART1, UART2 and USB.

    Covers the ``CFG_{PORT}{IN,OUT}PROT_{UBX,NMEA,RTCM3X}`` key family —
    the full picture the assertive ports section, the UBX-in liveness
    guard, and the profile form all need (issue #57).
    """

    in_protocols: dict[PortId, list[UbxProtocol]] = Field(
        default_factory=lambda: dict[PortId, list[UbxProtocol]](),
        description="port -> enabled input protocols",
    )
    out_protocols: dict[PortId, list[UbxProtocol]] = Field(
        default_factory=lambda: dict[PortId, list[UbxProtocol]](),
        description="port -> enabled output protocols",
    )

    def enabled_in(self, port: PortId) -> list[UbxProtocol]:
        """Return input protocols enabled on a given port."""
        return self.in_protocols.get(port, [])

    def enabled_out(self, port: PortId) -> list[UbxProtocol]:
        """Return output protocols enabled on a given port."""
        return self.out_protocols.get(port, [])


# ---------------------------------------------------------------------------
# GNSS constellation configuration
# ---------------------------------------------------------------------------


class GnssConstellation(str, enum.Enum):
    """GNSS satellite constellations."""

    GPS = "gps"
    GLONASS = "glonass"
    GALILEO = "galileo"
    BEIDOU = "beidou"
    SBAS = "sbas"
    QZSS = "qzss"


class GnssSystemConfig(BaseModel):
    """Configuration for a single GNSS constellation."""

    constellation: GnssConstellation = Field(description="GNSS system identifier")
    enabled: bool = Field(default=True, description="Whether this system is enabled")
    min_channels: int = Field(
        default=0, ge=0, le=255, description="Minimum tracking channels"
    )
    max_channels: int = Field(
        default=0, ge=0, le=255, description="Maximum tracking channels"
    )
    sig_cfg_mask: int = Field(
        default=0,
        ge=0,
        description="Signal configuration bitmask (vendor-specific)",
    )


class GnssConfig(BaseModel):
    """Full GNSS constellation configuration for the receiver."""

    systems: list[GnssSystemConfig] = Field(
        default_factory=lambda: list[GnssSystemConfig](),
        description="Configuration for each GNSS system",
    )

    def enabled_constellations(self) -> list[GnssConstellation]:
        """Return list of enabled constellation identifiers."""
        return [s.constellation for s in self.systems if s.enabled]


class ReceiverScalarConfig(BaseModel):
    """Every scalar (non-matrix, non-per-port) CFG value the Advanced GPS
    page's full receiver read needs — baud, measurement rate, dynamics
    model, TMODE mode, elevation mask, BeiDou B2 and SPI enablement.

    Read from the receiver in a single CFG-VALGET poll (issue #97),
    replacing what would otherwise be up to seven separate getters —
    three of which (dyn model, UART baud, TMODE mode) already existed,
    the other four (measurement rate, elevation mask, BeiDou B2, SPI)
    had no standalone getter at all before this.
    """

    uart1_baud: int = Field(description="UART1 baud rate")
    uart2_baud: int = Field(description="UART2 baud rate")
    meas_period_ms: int = Field(description="Measurement period in milliseconds")
    dyn_model: DynModel = Field(description="Dynamics platform model")
    tmode_mode: BaseMode = Field(description="Active base station mode")
    elevation_mask_deg: int = Field(description="Minimum satellite elevation, degrees")
    bds_b2_enabled: bool = Field(description="Whether the BeiDou B2 signal is enabled")
    spi_enabled: bool = Field(description="Whether the SPI interface is enabled")


# ---------------------------------------------------------------------------
# Survey-in progress
# ---------------------------------------------------------------------------


class GpsFixType(str, enum.Enum):
    """GPS fix type / quality indicator."""

    NO_FIX = "no_fix"
    DEAD_RECKONING = "dead_reckoning"
    FIX_2D = "2d"
    FIX_3D = "3d"
    GNSS_DR = "gnss_dr"  # GNSS + dead reckoning
    TIME_ONLY = "time_only"


class GpsPosition(BaseModel):
    """Live position data from the GPS receiver (NAV-PVT equivalent).

    Vendor-neutral snapshot of the receiver's current position solution.
    Updated by polling or streaming from the driver.
    """

    fix_type: GpsFixType = Field(
        default=GpsFixType.NO_FIX, description="Position fix type"
    )
    rtk_status: str = Field(
        default="none",
        description="RTK status: 'none', 'float', or 'fixed'",
    )
    latitude: float = Field(default=0.0, description="WGS84 latitude (°)")
    longitude: float = Field(default=0.0, description="WGS84 longitude (°)")
    altitude_m: float = Field(default=0.0, description="Height above ellipsoid (m)")
    altitude_msl_m: float = Field(
        default=0.0, description="Height above mean sea level (m)"
    )
    horizontal_accuracy_m: float = Field(
        default=0.0, ge=0.0, description="Horizontal accuracy estimate (m)"
    )
    vertical_accuracy_m: float = Field(
        default=0.0, ge=0.0, description="Vertical accuracy estimate (m)"
    )
    num_satellites: int = Field(
        default=0, ge=0, description="Number of satellites used in fix"
    )
    speed_m_s: float = Field(default=0.0, ge=0.0, description="Ground speed (m/s)")
    heading_deg: float = Field(default=0.0, description="Heading of motion (°)")
    pdop: float = Field(
        default=99.9, ge=0.0, description="Position dilution of precision"
    )
    timestamp: datetime | None = Field(default=None, description="UTC time of the fix")


class SurveyInProgress(BaseModel):
    """Live survey-in status from the receiver."""

    active: bool = Field(default=False, description="Survey-in is currently running")
    valid: bool = Field(default=False, description="Survey-in result is valid")
    duration_seconds: int = Field(
        default=0, ge=0, description="Elapsed duration in seconds"
    )
    mean_accuracy_mm: float = Field(
        default=0.0, ge=0.0, description="Current mean accuracy in mm"
    )
    observations: int = Field(
        default=0, ge=0, description="Number of observations collected"
    )
    # Position from the survey result (populated when valid=True)
    latitude: float | None = Field(default=None, description="WGS84 latitude (°)")
    longitude: float | None = Field(default=None, description="WGS84 longitude (°)")
    altitude_m: float | None = Field(
        default=None, description="Height above ellipsoid (m)"
    )


# ---------------------------------------------------------------------------
# Device status snapshot (for API responses)
# ---------------------------------------------------------------------------


class DeviceStatus(BaseModel):
    """Full device status snapshot returned by the API."""

    state: DeviceConnectionState = DeviceConnectionState.DISCONNECTED
    port: str | None = Field(default=None, description="Connected serial port path")
    baud_rate: int | None = Field(default=None, description="Serial baud rate")
    info: DeviceInfo | None = Field(
        default=None, description="Device identity (when connected)"
    )
    capabilities: list[DeviceCapability] = Field(
        default_factory=lambda: list[DeviceCapability](),
        description="Supported capabilities of the connected driver",
    )
    survey_in: SurveyInProgress | None = Field(
        default=None,
        description="Survey-in progress (when active)",
    )
    last_error: str | None = Field(default=None, description="Last error message")
    connected_at: datetime | None = Field(
        default=None, description="Connection timestamp"
    )
