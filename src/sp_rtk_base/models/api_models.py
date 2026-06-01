"""Pydantic models for REST API request/response serialization.

These models define the JSON schema for all API endpoints.
They are separate from config_models.py which handles YAML persistence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Relay status response models
# ---------------------------------------------------------------------------


class InputStatusResponse(BaseModel):
    """Input source status snapshot."""

    connected: bool = False
    source_type: str = ""
    bytes_received: int = 0
    messages_received: int = 0
    seconds_since_last_data: float = 0.0
    reconnect_attempts: int = 0
    reconnect_successes: int = 0


class DestinationStatusResponse(BaseModel):
    """Individual destination status snapshot."""

    name: str
    type: str = ""
    connected: bool = False
    enabled: bool = True
    bytes_sent: int = 0
    messages_sent: int = 0
    error: str | None = None


class AutoStartStatusModel(BaseModel):
    """Auto-start lifecycle snapshot for API consumers.

    Mirrors :class:`sp_rtk_base.services.AutoStartStatus`.  Surfaced
    on :class:`RelayStatusResponse` so the Dashboard and external
    monitors can render a banner when auto-start is retrying or has
    failed.
    """

    state: Literal[
        "idle",
        "skipped_no_input",
        "in_progress",
        "succeeded",
        "succeeded_user",
        "failed_config",
        "failed_after_retries",
    ]
    attempts: int = 0
    last_error: str | None = None
    last_updated: datetime | None = None


class RelayStatusResponse(BaseModel):
    """Complete relay engine status snapshot."""

    running: bool
    uptime_seconds: float | None = None
    input: InputStatusResponse = Field(default_factory=InputStatusResponse)
    destinations: list[DestinationStatusResponse] = Field(
        default_factory=lambda: list[DestinationStatusResponse]()
    )
    active_destination_count: int = 0
    total_destination_count: int = 0
    bytes_received: int = 0
    chunks_distributed: int = 0
    frames_parsed: int = 0
    auto_start: AutoStartStatusModel | None = None


# ---------------------------------------------------------------------------
# Relay control request models
# ---------------------------------------------------------------------------


class RelayStartRequest(BaseModel):
    """Request body for starting the relay engine.

    If not provided, the relay starts with the persisted config.
    """

    use_saved_config: bool = True


class RelayActionResponse(BaseModel):
    """Generic response for relay control actions."""

    status: str
    message: str


# ---------------------------------------------------------------------------
# Destination request/response models
# ---------------------------------------------------------------------------


class DestinationCreateRequest(BaseModel):
    """Request body for creating a new destination."""

    name: str
    type: str
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    filter: dict[str, Any] = Field(default_factory=lambda: {"mode": "pass_all"})


class DestinationUpdateRequest(BaseModel):
    """Request body for updating a destination."""

    enabled: bool | None = None
    config: dict[str, Any] | None = None
    filter: dict[str, Any] | None = None


class DestinationResponse(BaseModel):
    """Response body for a destination."""

    name: str
    type: str
    enabled: bool
    config: dict[str, Any]
    filter: dict[str, Any]


class DestinationListResponse(BaseModel):
    """Response body for listing destinations."""

    destinations: list[DestinationResponse]
    count: int


# ---------------------------------------------------------------------------
# Input config request/response models
# ---------------------------------------------------------------------------


class InputConfigRequest(BaseModel):
    """Request body for setting input source configuration."""

    source: str
    config: dict[str, Any] = Field(default_factory=dict)


class InputConfigResponse(BaseModel):
    """Response body for input source configuration."""

    source: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    configured: bool = False


# ---------------------------------------------------------------------------
# Settings request/response models
# ---------------------------------------------------------------------------


class AppSettingsRequest(BaseModel):
    """Request body for updating application settings."""

    auto_start: bool | None = None
    status_poll_interval: float | None = None


class AppSettingsResponse(BaseModel):
    """Response body for application settings."""

    auto_start: bool
    status_poll_interval: float


# ---------------------------------------------------------------------------
# Event models
# ---------------------------------------------------------------------------


class EventResponse(BaseModel):
    """A single relay event."""

    event_type: str
    message: str
    timestamp: float
    payload: dict[str, Any] = Field(default_factory=dict)


class EventListResponse(BaseModel):
    """Response body for listing recent events."""

    events: list[EventResponse]
    count: int


# ---------------------------------------------------------------------------
# Device request/response models
# ---------------------------------------------------------------------------


class DeviceConnectRequest(BaseModel):
    """Request body for connecting to a GPS device."""

    vendor: str = Field(default="ublox", description="Driver vendor key")
    port: str = Field(description="Serial port path (e.g. /dev/ttyUSB0)")
    baud_rate: int = Field(
        default=115200, ge=4800, le=921600, description="Serial baud rate"
    )


class DeviceActionResponse(BaseModel):
    """Generic response for device control actions."""

    status: str
    message: str
