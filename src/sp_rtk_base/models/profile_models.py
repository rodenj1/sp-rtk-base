"""GPS receiver profile schema — ``ReceiverConfig`` and ``Profile``.

Two nested types:

- ``ReceiverConfig`` — everything writable to a receiver (no ``name``).
  This is what Apply takes.
- ``Profile`` — ``ReceiverConfig`` plus identity metadata (``name``,
  ``version``, ``hardware``, ``forked_from``, ``display_name``). This
  is what gets saved, listed, exported and imported.

``name`` is the profile's immutable slug — also its filename and the
key fork references (``forked_from``) point at. ``display_name`` is
an optional human-readable label; when absent, every UI surface falls
back to rendering ``name``. Renaming a profile only ever changes
``display_name`` — the slug is frozen at creation so fork references
never dangle and no files ever move.

Only *context-free* validation lives here — rules that need no live
device state. The UBX-in liveness guard and the ``tmode_mode: fixed``
coordinate guard are service-level and belong to the apply ticket.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sp_rtk_base.models.device_models import (
    BaseMode as TmodeMode,
)
from sp_rtk_base.models.device_models import (
    DynModel,
    GnssConstellation,
    PortId,
    RtcmRowId,
    UbxProtocol,
)
from sp_rtk_base.models.hardware_identity import (
    HARDWARE_ANY,
    KNOWN_FAMILY_TOKENS,
    KNOWN_MODEL_TOKENS,
)

# ---------------------------------------------------------------------------
# Hardware target tokens
#
# The identity-resolution catalog lives in ``hardware_identity`` — the
# hardware-detection ticket (#60) that owns it — and is re-exported here so
# a ``Profile`` can target any model or family that resolver can actually
# produce, plus ``HARDWARE_ANY``.
# ---------------------------------------------------------------------------

#: Verbatim MON-VER MOD= model ids a profile may target directly.
KNOWN_HARDWARE_MODELS: frozenset[str] = KNOWN_MODEL_TOKENS

#: Generation-level fallback tokens (a profile compatible with a whole
#: hardware family rather than one specific model).
KNOWN_HARDWARE_FAMILIES: frozenset[str] = KNOWN_FAMILY_TOKENS

#: Schema major versions this running app understands.
KNOWN_PROFILE_VERSIONS: frozenset[int] = frozenset({1})

#: Ports that can carry the app's own management link and therefore
#: qualify as a rover data-link source. USB is excluded per spec.
_DATA_LINK_CANDIDATE_PORTS: frozenset[PortId] = frozenset({PortId.UART1, PortId.UART2})

_SANE_BAUD_MIN = 9600
_SANE_BAUD_MAX = 921600


# ---------------------------------------------------------------------------
# Receiver role fields
#
# ``tmode_mode`` reuses ``device_models.BaseMode`` (imported here as
# ``TmodeMode``) — same three values (``CFG_TMODE_MODE``), whether read live
# or declared by a profile. Port protocols reuse ``device_models.UbxProtocol``
# for the same reason.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Nested value objects
# ---------------------------------------------------------------------------


class BaudConfig(BaseModel):
    """Per-UART baud rate. USB is excluded — USB CDC has no baud rate."""

    model_config = ConfigDict(extra="forbid")

    uart1: int | None = Field(default=None, ge=_SANE_BAUD_MIN, le=_SANE_BAUD_MAX)
    uart2: int | None = Field(default=None, ge=_SANE_BAUD_MIN, le=_SANE_BAUD_MAX)


class PortProtocolSet(BaseModel):
    """Full in/out protocol set for one port (assertive, not a delta)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    in_: list[UbxProtocol] = Field(
        default_factory=lambda: list[UbxProtocol](), alias="in"
    )
    out: list[UbxProtocol] = Field(default_factory=lambda: list[UbxProtocol]())


class RtcmStreamConfig(BaseModel):
    """The boolean RTCM message x port matrix.

    Absent cell = off. Rows are restricted to the standard 12-member
    ``RtcmRowId`` set; columns to ``PortId`` (UART1/UART2/USB) — the
    matrix deliberately does not claim I2C or SPI.
    """

    model_config = ConfigDict(extra="forbid")

    matrix: dict[RtcmRowId, dict[PortId, bool]] = Field(
        default_factory=lambda: dict[RtcmRowId, dict[PortId, bool]]()
    )


# ---------------------------------------------------------------------------
# ReceiverConfig — what Apply takes
# ---------------------------------------------------------------------------


class ReceiverConfig(BaseModel):
    """Everything writable to a receiver. No ``name`` — see ``Profile``."""

    model_config = ConfigDict(extra="forbid")

    baud: BaudConfig | None = Field(default=None)
    meas_period_ms: int = Field(default=1000, ge=100, le=60000)
    constellations: list[GnssConstellation] | None = Field(default=None)
    ports: dict[PortId, PortProtocolSet] | None = Field(default=None)
    data_link_port: list[PortId] = Field(min_length=1)
    dyn_model: DynModel | None = Field(default=None)
    tmode_mode: TmodeMode | None = Field(default=None)
    elevation_mask_deg: int | None = Field(default=None, ge=0, le=90)
    bds_b2_enabled: bool | None = Field(default=None)
    spi_enabled: bool | None = Field(default=None)
    rtcm_stream: RtcmStreamConfig = Field(default_factory=RtcmStreamConfig)

    @model_validator(mode="after")
    def _validate_data_link_ports_are_uart(self) -> ReceiverConfig:
        invalid_ports = [
            p for p in self.data_link_port if p not in _DATA_LINK_CANDIDATE_PORTS
        ]
        if invalid_ports:
            raise ValueError(
                f"data_link_port: {[p.value for p in invalid_ports]} cannot be a "
                "data-link port — only UART1/UART2 are valid"
            )
        return self

    @model_validator(mode="after")
    def _validate_1005_on_a_data_link_port(self) -> ReceiverConfig:
        rows_on_1005 = self.rtcm_stream.matrix.get(RtcmRowId.RTCM_1005, {})
        if not any(rows_on_1005.get(port, False) for port in self.data_link_port):
            raise ValueError(
                "1005 (Station ARP) must be enabled on at least one data_link_port"
            )
        return self

    @model_validator(mode="after")
    def _validate_every_data_link_port_has_a_row_on(self) -> ReceiverConfig:
        matrix = self.rtcm_stream.matrix
        for port in self.data_link_port:
            has_any_row_on = any(
                ports_for_row.get(port, False) for ports_for_row in matrix.values()
            )
            if not has_any_row_on:
                raise ValueError(
                    f"data_link_port {port.value}: has zero RTCM rows enabled"
                )
        return self


# ---------------------------------------------------------------------------
# Profile — ReceiverConfig + identity metadata
# ---------------------------------------------------------------------------


class Profile(ReceiverConfig):
    """A saved, named, hardware-tagged receiver configuration."""

    name: str = Field(min_length=1)
    version: int
    hardware: str
    forked_from: str | None = Field(default=None)
    display_name: str | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_identity(self) -> Profile:
        if self.version not in KNOWN_PROFILE_VERSIONS:
            raise ValueError(
                f"version {self.version} is not a schema version this app knows "
                f"(known: {sorted(KNOWN_PROFILE_VERSIONS)})"
            )

        known_hardware = (
            KNOWN_HARDWARE_MODELS | KNOWN_HARDWARE_FAMILIES | {HARDWARE_ANY}
        )
        if self.hardware not in known_hardware:
            raise ValueError(
                f"hardware {self.hardware!r} is not a known model, family, or "
                f"{HARDWARE_ANY!r} token"
            )

        return self
