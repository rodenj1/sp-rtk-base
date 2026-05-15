"""Pydantic models for application configuration persistence.

Defines the data structures that are serialized to/from YAML
for persisting input source, destination profiles, and app settings.

These models bridge the gap between the YAML config file and
sp-rtk-base-relay's dataclass-based configuration objects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from sp_rtk_base_relay.config import (
    DestinationConfig,
    DestinationFilterConfig,
    InputConfig,
    NtripDestinationConfig,
    SurePathDestinationConfig,
    TcpServerDestinationConfig,
)


# ---------------------------------------------------------------------------
# Filter profile
# ---------------------------------------------------------------------------


class FilterProfile(BaseModel):
    """Message filter configuration for a destination."""

    mode: Literal["pass_all", "allowlist", "blocklist"] = "pass_all"
    message_ids: list[int] = Field(default_factory=lambda: list[int]())

    def to_relay_config(self) -> DestinationFilterConfig:
        """Convert to sp-rtk-base-relay DestinationFilterConfig dataclass.

        Returns:
            DestinationFilterConfig instance.
        """
        return DestinationFilterConfig(
            mode=self.mode,
            message_ids=list(self.message_ids),
        )

    @classmethod
    def from_relay_config(cls, config: DestinationFilterConfig) -> FilterProfile:
        """Create from sp-rtk-base-relay DestinationFilterConfig.

        Args:
            config: Relay engine filter config dataclass.

        Returns:
            FilterProfile instance.
        """
        return cls(mode=config.mode, message_ids=list(config.message_ids))


# ---------------------------------------------------------------------------
# Destination profiles
# ---------------------------------------------------------------------------


class SurePathProfile(BaseModel):
    """SurePath destination-specific configuration."""

    host: str
    port: int = 50010
    username: str
    password: str
    connection_timeout: int = 10
    read_timeout: int = 30
    heartbeat_timeout: int = 30
    retry_initial_delay: int = 15
    retry_max_delay: int = 60
    retry_multiplier: float = 2.0

    def to_relay_config(self) -> SurePathDestinationConfig:
        """Convert to sp-rtk-base-relay SurePathDestinationConfig.

        Returns:
            SurePathDestinationConfig instance.
        """
        return SurePathDestinationConfig(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            connection_timeout=self.connection_timeout,
            read_timeout=self.read_timeout,
            heartbeat_timeout=self.heartbeat_timeout,
            retry_initial_delay=self.retry_initial_delay,
            retry_max_delay=self.retry_max_delay,
            retry_multiplier=self.retry_multiplier,
        )


class NtripProfile(BaseModel):
    """NTRIP destination-specific configuration."""

    caster: str
    port: int = 2101
    mountpoint: str
    password: str
    username: str = ""
    version: str = "2.0"
    connection_timeout: int = 15
    retry_initial_delay: int = 10
    retry_max_delay: int = 120
    retry_multiplier: float = 2.0

    def to_relay_config(self) -> NtripDestinationConfig:
        """Convert to sp-rtk-base-relay NtripDestinationConfig.

        Returns:
            NtripDestinationConfig instance.
        """
        return NtripDestinationConfig(
            caster=self.caster,
            port=self.port,
            mountpoint=self.mountpoint,
            password=self.password,
            username=self.username,
            version=self.version,
            connection_timeout=self.connection_timeout,
            retry_initial_delay=self.retry_initial_delay,
            retry_max_delay=self.retry_max_delay,
            retry_multiplier=self.retry_multiplier,
        )


class TcpServerProfile(BaseModel):
    """TCP server destination-specific configuration."""

    host: str = "0.0.0.0"
    port: int = 5016
    max_clients: int = 10

    def to_relay_config(self) -> TcpServerDestinationConfig:
        """Convert to sp-rtk-base-relay TcpServerDestinationConfig.

        Returns:
            TcpServerDestinationConfig instance.
        """
        return TcpServerDestinationConfig(
            host=self.host,
            port=self.port,
            max_clients=self.max_clients,
        )


class DestinationProfile(BaseModel):
    """A named destination profile for persistence.

    The ``config`` field holds the destination-type-specific settings
    as a plain dictionary. Use ``to_relay_config()`` to convert to
    the sp-rtk-base-relay ``DestinationConfig`` dataclass.
    """

    name: str
    type: Literal["surepath", "ntrip", "tcp_server"]
    enabled: bool = True
    filter: FilterProfile = Field(default_factory=FilterProfile)
    config: dict[str, Any] = Field(default_factory=dict)

    def to_relay_config(self) -> DestinationConfig:
        """Convert to sp-rtk-base-relay DestinationConfig dataclass.

        Returns:
            DestinationConfig with the appropriate type-specific config.

        Raises:
            ValueError: If the destination type is not recognized.
        """
        filter_config = self.filter.to_relay_config()

        specific_config: (
            SurePathDestinationConfig
            | NtripDestinationConfig
            | TcpServerDestinationConfig
        )
        if self.type == "surepath":
            specific_config = SurePathProfile(**self.config).to_relay_config()
        elif self.type == "ntrip":
            specific_config = NtripProfile(**self.config).to_relay_config()
        elif self.type == "tcp_server":
            specific_config = TcpServerProfile(**self.config).to_relay_config()
        else:
            msg = f"Unknown destination type: {self.type}"
            raise ValueError(msg)

        return DestinationConfig(
            name=self.name,
            type=self.type,
            enabled=self.enabled,
            filter=filter_config,
            config=specific_config,
        )


# ---------------------------------------------------------------------------
# Input source profile
# ---------------------------------------------------------------------------


class InputProfile(BaseModel):
    """Input source configuration for persistence."""

    source: Literal["serial", "usb_serial", "tcp", "bluetooth"]
    config: dict[str, Any] = Field(default_factory=dict)

    def to_relay_config(self) -> InputConfig:
        """Convert to sp-rtk-base-relay InputConfig dataclass.

        Returns:
            InputConfig instance.
        """
        return InputConfig(source=self.source, config=dict(self.config))


# ---------------------------------------------------------------------------
# Application settings
# ---------------------------------------------------------------------------


class AppSettings(BaseModel):
    """Application-level settings."""

    auto_start: bool = False
    status_poll_interval: float = 2.0
    metrics_enabled: bool = True


# ---------------------------------------------------------------------------
# Top-level application config
# ---------------------------------------------------------------------------


class DeviceProfile(BaseModel):
    """Remembered GPS device connection settings.

    Persisted so the Device page can pre-fill the last-used port,
    baud rate, and driver vendor across restarts.
    """

    vendor: str = "ublox"
    port: str = ""
    baud_rate: int = 115200


# ---------------------------------------------------------------------------
# Base station position profiles
# ---------------------------------------------------------------------------


class BaseStationPosition(BaseModel):
    """A named base station position profile.

    Created after a successful survey-in (or entered manually).
    Used to restore a receiver to fixed-base mode with known
    coordinates — avoiding a re-survey after power loss or site
    revisit.
    """

    name: str = Field(description="User-chosen name (e.g. 'Office Roof')")
    latitude: float = Field(description="WGS84 latitude (°)", ge=-90.0, le=90.0)
    longitude: float = Field(description="WGS84 longitude (°)", ge=-180.0, le=180.0)
    altitude_m: float = Field(description="Height above ellipsoid (m)")
    accuracy_mm: float = Field(
        default=0.0, ge=0.0, description="Survey accuracy at time of save (mm)"
    )
    surveyed_at: datetime | None = Field(
        default=None, description="When the survey was performed"
    )
    source: Literal["survey_in", "manual", "device"] = Field(
        default="survey_in", description="How the position was obtained"
    )


class AppConfig(BaseModel):
    """Complete application configuration for YAML persistence.

    This is the root model that gets serialized to / deserialized from
    ``~/.config/sp-base/config.yaml`` (or ``SP_BASE_CONFIG`` override).
    """

    input: InputProfile | None = None
    destinations: list[DestinationProfile] = Field(
        default_factory=lambda: list[DestinationProfile]()
    )
    settings: AppSettings = Field(default_factory=AppSettings)
    device: DeviceProfile | None = None
    base_positions: list[BaseStationPosition] = Field(
        default_factory=lambda: list[BaseStationPosition](),
        description="Saved base station position profiles",
    )
