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

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sp_rtk_base.models.device_models import (
    ALL_RTCM_MESSAGE_IDS,
    DynModel,
    GnssConstellation,
    PortId,
    RtcmRowId,
    UbxProtocol,
)
from sp_rtk_base.models.device_models import (
    BaseMode as TmodeMode,
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

#: Ports the RTCM matrix covers, in ``ReceiverAssertion``/``ReceiverConfig``
#: alike — deliberately excludes I2C/SPI, which the schema doesn't claim.
#: Public: shared with ``device_service.build_receiver_assertion`` and
#: ``ui.pages.gps_config`` so the port set is defined in exactly one place.
MATRIX_PORTS: tuple[PortId, ...] = (PortId.UART1, PortId.UART2, PortId.USB)


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
    """Everything writable to a receiver. No ``name`` — see ``Profile``.

    ``data_link_port`` deliberately isn't a field here (issue #98): it
    has no CFG key and is re-inferred from the matrix on every load,
    so there is no receiver-side counterpart a ``ReceiverConfig``
    could ever be out of sync with. It lives on ``Profile`` (a saved
    profile does want to remember which ports were the data-link
    ports) and on ``ReceiverApplyRequest`` (Apply's envelope, where
    it's purely an input to the cross-field validators below).
    """

    model_config = ConfigDict(extra="forbid")

    baud: BaudConfig | None = Field(default=None)
    meas_period_ms: int = Field(default=1000, ge=100, le=60000)
    constellations: list[GnssConstellation] | None = Field(default=None)
    ports: dict[PortId, PortProtocolSet] | None = Field(default=None)
    dyn_model: DynModel | None = Field(default=None)
    tmode_mode: TmodeMode | None = Field(default=None)
    elevation_mask_deg: int | None = Field(default=None, ge=0, le=90)
    bds_b2_enabled: bool | None = Field(default=None)
    spi_enabled: bool | None = Field(default=None)
    rtcm_stream: RtcmStreamConfig = Field(default_factory=RtcmStreamConfig)


# ---------------------------------------------------------------------------
# Shared data-link-port cross-field rules
#
# Three context-free rules that need both a ``data_link_port`` selection
# and an RTCM matrix to check. Neither ``ReceiverConfig`` nor
# ``ReceiverAssertion`` carries ``data_link_port`` (issue #98 — it has no
# CFG key of its own), so these rules apply wherever the two are bundled
# together: ``Profile`` (its own ``data_link_port`` field, alongside the
# ``rtcm_stream`` it inherits from ``ReceiverConfig``) and
# ``ReceiverApplyRequest`` (Apply's envelope). Extracted as free functions
# so both call sites validate identically without one inheriting the
# other's fields.
# ---------------------------------------------------------------------------


def _check_data_link_ports_are_uart(data_link_port: list[PortId]) -> None:
    invalid_ports = [p for p in data_link_port if p not in _DATA_LINK_CANDIDATE_PORTS]
    if invalid_ports:
        raise ValueError(
            f"data_link_port: {[p.value for p in invalid_ports]} cannot be a "
            "data-link port — only UART1/UART2 are valid"
        )


def _check_1005_on_a_data_link_port(
    data_link_port: list[PortId], matrix: dict[RtcmRowId, dict[PortId, bool]]
) -> None:
    rows_on_1005 = matrix.get(RtcmRowId.RTCM_1005, {})
    if not any(rows_on_1005.get(port, False) for port in data_link_port):
        raise ValueError(
            "1005 (Station ARP) must be enabled on at least one data_link_port"
        )


def _check_every_data_link_port_has_a_row_on(
    data_link_port: list[PortId], matrix: dict[RtcmRowId, dict[PortId, bool]]
) -> None:
    for port in data_link_port:
        has_any_row_on = any(
            ports_for_row.get(port, False) for ports_for_row in matrix.values()
        )
        if not has_any_row_on:
            raise ValueError(f"data_link_port {port.value}: has zero RTCM rows enabled")


# ---------------------------------------------------------------------------
# Profile — ReceiverConfig + identity metadata
# ---------------------------------------------------------------------------


class Profile(ReceiverConfig):
    """A saved, named, hardware-tagged receiver configuration.

    Unlike ``ReceiverAssertion``, a saved profile *does* want to
    remember which ports were the data-link ports — that's part of
    what makes it worth saving — so ``data_link_port`` lives here as
    ``Profile``'s own field rather than on ``ReceiverConfig`` (issue
    #98).
    """

    data_link_port: list[PortId] = Field(min_length=1)
    name: str = Field(min_length=1)
    version: int
    hardware: str
    forked_from: str | None = Field(default=None)
    display_name: str | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_data_link_ports_are_uart(self) -> Profile:
        _check_data_link_ports_are_uart(self.data_link_port)
        return self

    @model_validator(mode="after")
    def _validate_1005_on_a_data_link_port(self) -> Profile:
        _check_1005_on_a_data_link_port(self.data_link_port, self.rtcm_stream.matrix)
        return self

    @model_validator(mode="after")
    def _validate_every_data_link_port_has_a_row_on(self) -> Profile:
        _check_every_data_link_port_has_a_row_on(
            self.data_link_port, self.rtcm_stream.matrix
        )
        return self

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


# ---------------------------------------------------------------------------
# ReceiverAssertion — the shape a full receiver read returns (issue #97)
# ---------------------------------------------------------------------------


class BaudAssertion(BaseModel):
    """Per-UART baud rate, both required — a live read always has both."""

    model_config = ConfigDict(extra="forbid")

    uart1: int = Field(ge=_SANE_BAUD_MIN, le=_SANE_BAUD_MAX)
    uart2: int = Field(ge=_SANE_BAUD_MIN, le=_SANE_BAUD_MAX)


class ReceiverAssertion(BaseModel):
    """Every receiver field required — no optional values.

    Deliberately symmetric with ``ReceiverConfig``: the same field list
    Apply will eventually send is the field list a full read returns
    (``data_link_port`` excepted — that stays page state, inferred from
    the matrix rather than asserted). Unlike ``ReceiverConfig``, there
    is no "omitted means leave it alone" here — every field always
    carries the receiver's actual value, which is exactly what makes a
    ``ReceiverAssertion`` usable as both ``form`` and ``live`` state: on
    seed they're equal by construction, and every field the page
    displays reflects the receiver rather than a schema default.
    """

    model_config = ConfigDict(extra="forbid")

    baud: BaudAssertion
    meas_period_ms: int = Field(ge=100, le=60000)
    constellations: list[GnssConstellation]
    ports: dict[PortId, PortProtocolSet]
    dyn_model: DynModel
    tmode_mode: TmodeMode
    elevation_mask_deg: int = Field(ge=0, le=90)
    bds_b2_enabled: bool
    spi_enabled: bool
    rtcm_stream: RtcmStreamConfig


# ---------------------------------------------------------------------------
# ReceiverApplyRequest — Apply's envelope (issue #98)
# ---------------------------------------------------------------------------


class ReceiverApplyRequest(BaseModel):
    """The envelope Apply sends: the whole asserted receiver state plus
    the data-link port selection.

    ``data_link_port`` cannot live on ``ReceiverAssertion`` itself — it
    has no CFG key and is re-inferred from the matrix on every load, so
    there is no receiver-side counterpart it could ever be out of sync
    with. Here it is purely an input to the three cross-field
    validators below; it is never counted or marked in a diff (see
    :func:`diff_receiver_assertions`, which never looks at it) — an
    Apply where only the data-link port selection changed compares
    equal on ``assertion`` and so correctly reports nothing to do.
    """

    model_config = ConfigDict(extra="forbid")

    assertion: ReceiverAssertion
    data_link_port: list[PortId] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_data_link_ports_are_uart(self) -> ReceiverApplyRequest:
        _check_data_link_ports_are_uart(self.data_link_port)
        return self

    @model_validator(mode="after")
    def _validate_1005_on_a_data_link_port(self) -> ReceiverApplyRequest:
        _check_1005_on_a_data_link_port(
            self.data_link_port, self.assertion.rtcm_stream.matrix
        )
        return self

    @model_validator(mode="after")
    def _validate_every_data_link_port_has_a_row_on(self) -> ReceiverApplyRequest:
        _check_every_data_link_port_has_a_row_on(
            self.data_link_port, self.assertion.rtcm_stream.matrix
        )
        return self


# ---------------------------------------------------------------------------
# Per-leaf, path-keyed diff — verify and sync collapse to one call (issue #98)
# ---------------------------------------------------------------------------


class ApplyDiffEntry(BaseModel):
    """One leaf-level mismatch between two ``ReceiverAssertion`` values.

    *path* identifies the exact widget a future inline marker would
    highlight — a single matrix cell (``rtcm.1005.UART1``), one port's
    one protocol direction (``ports.UART1.out.RTCM3X``), one
    constellation (``constellations.gps``), or a plain scalar field
    (``meas_period_ms``). Every leaf is a scalar, so *expected*/*actual*
    stay simply typed rather than needing a union of composite shapes.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    expected: bool | int | str
    actual: bool | int | str


def diff_receiver_assertions(
    expected: ReceiverAssertion, actual: ReceiverAssertion
) -> list[ApplyDiffEntry]:
    """Per-leaf, path-keyed differences between two ``ReceiverAssertion``s.

    One entry per scalar leaf — matrix cells, per-port per-protocol
    in/out membership, per-constellation membership, baud, and every
    other scalar field — never a whole-field comparison: the per-cell
    resolution is load-bearing for the UI (a single toggled cell must
    render as exactly one diff line, not "the matrix differs"), every
    leaf being a scalar keeps *expected*/*actual* simply typed, and
    *path* is the join key a future inline marker will need to
    highlight the mismatched widget.

    This one expression is the comparison this app uses everywhere two
    receiver states need diffing: Apply's post-write read-back verify
    (sent vs. read-back) and the Advanced GPS page's "receiver out of
    sync" indicator (form vs. live) both call this — verify and sync
    collapse to the same call on the same type.

    Deliberately never mentions ``data_link_port`` — neither operand is
    a ``ReceiverApplyRequest`` — so the data-link port selection can
    never appear as a difference.
    """
    diffs: list[ApplyDiffEntry] = []

    if expected.baud.uart1 != actual.baud.uart1:
        diffs.append(
            ApplyDiffEntry(
                path="baud.uart1",
                expected=expected.baud.uart1,
                actual=actual.baud.uart1,
            )
        )
    if expected.baud.uart2 != actual.baud.uart2:
        diffs.append(
            ApplyDiffEntry(
                path="baud.uart2",
                expected=expected.baud.uart2,
                actual=actual.baud.uart2,
            )
        )

    if expected.meas_period_ms != actual.meas_period_ms:
        diffs.append(
            ApplyDiffEntry(
                path="meas_period_ms",
                expected=expected.meas_period_ms,
                actual=actual.meas_period_ms,
            )
        )

    expected_constellations = set(expected.constellations)
    actual_constellations = set(actual.constellations)
    for constellation in GnssConstellation:
        exp_on = constellation in expected_constellations
        act_on = constellation in actual_constellations
        if exp_on != act_on:
            diffs.append(
                ApplyDiffEntry(
                    path=f"constellations.{constellation.value}",
                    expected=exp_on,
                    actual=act_on,
                )
            )

    empty_ports = PortProtocolSet()
    for port in PortId:
        expected_port = expected.ports.get(port, empty_ports)
        actual_port = actual.ports.get(port, empty_ports)
        expected_in = set(expected_port.in_)
        actual_in = set(actual_port.in_)
        expected_out = set(expected_port.out)
        actual_out = set(actual_port.out)
        for protocol in UbxProtocol:
            exp_in_on = protocol in expected_in
            act_in_on = protocol in actual_in
            if exp_in_on != act_in_on:
                diffs.append(
                    ApplyDiffEntry(
                        path=f"ports.{port.value}.in.{protocol.value}",
                        expected=exp_in_on,
                        actual=act_in_on,
                    )
                )
            exp_out_on = protocol in expected_out
            act_out_on = protocol in actual_out
            if exp_out_on != act_out_on:
                diffs.append(
                    ApplyDiffEntry(
                        path=f"ports.{port.value}.out.{protocol.value}",
                        expected=exp_out_on,
                        actual=act_out_on,
                    )
                )

    if expected.dyn_model != actual.dyn_model:
        diffs.append(
            ApplyDiffEntry(
                path="dyn_model",
                expected=expected.dyn_model.value,
                actual=actual.dyn_model.value,
            )
        )

    if expected.tmode_mode != actual.tmode_mode:
        diffs.append(
            ApplyDiffEntry(
                path="tmode_mode",
                expected=expected.tmode_mode.value,
                actual=actual.tmode_mode.value,
            )
        )

    if expected.elevation_mask_deg != actual.elevation_mask_deg:
        diffs.append(
            ApplyDiffEntry(
                path="elevation_mask_deg",
                expected=expected.elevation_mask_deg,
                actual=actual.elevation_mask_deg,
            )
        )

    if expected.bds_b2_enabled != actual.bds_b2_enabled:
        diffs.append(
            ApplyDiffEntry(
                path="bds_b2_enabled",
                expected=expected.bds_b2_enabled,
                actual=actual.bds_b2_enabled,
            )
        )

    if expected.spi_enabled != actual.spi_enabled:
        diffs.append(
            ApplyDiffEntry(
                path="spi_enabled",
                expected=expected.spi_enabled,
                actual=actual.spi_enabled,
            )
        )

    for row_id in ALL_RTCM_MESSAGE_IDS:
        expected_row = expected.rtcm_stream.matrix.get(row_id, {})
        actual_row = actual.rtcm_stream.matrix.get(row_id, {})
        for port in MATRIX_PORTS:
            exp_on = expected_row.get(port, False)
            act_on = actual_row.get(port, False)
            if exp_on != act_on:
                diffs.append(
                    ApplyDiffEntry(
                        path=f"rtcm.{row_id.value}.{port.value}",
                        expected=exp_on,
                        actual=act_on,
                    )
                )

    return diffs


#: The write steps ``apply_receiver_config`` performs, in execution
#: order (issue #99). Baud is last — see
#: ``DeviceService.apply_receiver_config`` for why.
APPLY_STEPS: tuple[str, ...] = (
    "meas_period_ms",
    "ports",
    "constellations",
    "optimisations",
    "dyn_model",
    "tmode_mode",
    "rtcm_matrix",
    "baud",
)


def step_for_diff_path(path: str) -> str:
    """Map one :class:`ApplyDiffEntry` ``path`` onto the write step that
    owns it — the grouping :func:`~sp_rtk_base.services.device_service.
    DeviceService.apply_receiver_config` uses to decide, per step,
    whether the pre-write read-back already matches what's being sent
    (issue #99). Every path :func:`diff_receiver_assertions` can ever
    produce resolves to exactly one of :data:`APPLY_STEPS`.
    """
    if path == "meas_period_ms":
        return "meas_period_ms"
    if path.startswith("ports."):
        return "ports"
    if path.startswith("constellations."):
        return "constellations"
    if path in ("elevation_mask_deg", "bds_b2_enabled", "spi_enabled"):
        return "optimisations"
    if path == "dyn_model":
        return "dyn_model"
    if path == "tmode_mode":
        return "tmode_mode"
    if path.startswith("rtcm."):
        return "rtcm_matrix"
    if path.startswith("baud."):
        return "baud"
    raise ValueError(f"unrecognised apply-diff path: {path!r}")  # pragma: no cover


class ApplyStepResult(BaseModel):
    """One write step's outcome, in the order ``apply_receiver_config`` ran it.

    ``"skipped"`` covers two distinct reasons alike (issue #99): the
    step's fields already matched the pre-write read-back (the
    service-decided no-op), or an earlier step failed and stopped the
    rest of the sequence from running at all. Either way, nothing was
    written for this step.
    """

    model_config = ConfigDict(extra="forbid")

    step: str
    status: Literal["ok", "failed", "skipped"]


class ApplyStepWarning(BaseModel):
    """One advisory drained from the driver's warning channel after a step
    ran (issue #99).

    Means exactly one thing, deliberately with no severity field: the
    write appears to have succeeded, but something the sync check is
    structurally unable to see is wrong (a write that landed in RAM
    but not flash; a constellation the firmware acknowledged but does
    not appear to act on). ``step`` is the step whose write produced
    it — a step that warned on one Apply is excluded from the skip on
    the next, so pressing Apply again actually retries it.
    """

    model_config = ConfigDict(extra="forbid")

    step: str
    message: str


class ApplyConfigResult(BaseModel):
    """Response for ``POST /api/device/apply-config``.

    ``status="failed"`` means the post-apply read-back didn't match
    what was sent — the writes are left in flash (nothing is rolled
    back); ``diff`` lists every mismatched leaf, path-keyed (see
    :func:`diff_receiver_assertions`). ``read_back`` is the full
    post-apply assertion read — callers (the Advanced GPS page) sync
    their live state from it whether ``status`` is ``"ok"`` or
    ``"failed"``, since the writes land either way (or didn't run at
    all — a failed or skipped step still leaves ``read_back`` as the
    truth of what the receiver holds). ``steps`` reports every write
    step's outcome in execution order (issue #99); ``step_warnings``
    carries the driver's step-tagged warning channel — the one thing
    left in this result's warning channel: a post-write observation
    the sync check can't express (issue #102 moved the other warning
    this used to carry, the estimated-throughput advisory, to a live,
    form-derived caption on the Advanced GPS page instead, since there
    was no consumer of it here — see :class:`ApplyStepWarning`).
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "failed"]
    read_back: ReceiverAssertion
    diff: list[ApplyDiffEntry] = Field(default_factory=lambda: list[ApplyDiffEntry]())
    steps: list[ApplyStepResult] = Field(
        default_factory=lambda: list[ApplyStepResult]()
    )
    step_warnings: list[ApplyStepWarning] = Field(
        default_factory=lambda: list[ApplyStepWarning]()
    )


def _dense_matrix(
    matrix: dict[RtcmRowId, dict[PortId, bool]],
) -> dict[RtcmRowId, dict[PortId, bool]]:
    """Normalize a possibly-sparse row x port matrix to the full catalog
    x :data:`MATRIX_PORTS` grid (absent cell = off).

    A profile's matrix is sparse by design (``RtcmStreamConfig``: absent
    cell = off); a live read-back is already dense. Routing both through
    this same normalization is what makes a merged/live/form matrix
    comparable by plain equality.
    """
    return {
        row_id: {port: matrix.get(row_id, {}).get(port, False) for port in MATRIX_PORTS}
        for row_id in ALL_RTCM_MESSAGE_IDS
    }


def merge_profile_into_assertion(
    profile: Profile, live: ReceiverAssertion
) -> ReceiverAssertion:
    """Resolve a profile pick's optional omissions against a live-seeded
    assertion, producing a fully-populated ``form``.

    Wherever *profile* leaves a field unset, *live*'s value stays in
    place instead of falling back to a schema default — e.g. the
    built-in profile omits USB port protocols and ``tmode_mode`` on
    purpose, and both stay whatever the receiver already reports.
    ``ports`` is merged per-port (a profile may set some ports and
    leave others alone); every other field is whole-field.

    ``rtcm_stream`` is the one exception to the omission rule: a
    profile's matrix is always assertive (absent cell = off, never
    "leave alone" — see ``RtcmStreamConfig``), so it's taken from
    *profile* outright, normalized to the same dense grid *live*'s
    matrix already uses.

    ``meas_period_ms`` is a second, narrower exception, inherited from
    ``ReceiverConfig`` rather than introduced here: that field is a
    plain ``int`` defaulting to ``1000`` (see ``ReceiverConfig``), not
    ``int | None`` like every other optimisation/role field, so a
    profile that never mentions it is indistinguishable from one that
    explicitly wants ``1000`` — there is no wire-level "omitted" state
    to fall back to *live* from. ``profile.meas_period_ms`` is always
    used as-is.

    A pure, module-level function (issue #97) — no device I/O, no page
    state — so the omission rule is an explicit, testable merge step
    rather than implicit driver or page-closure behaviour.
    """
    merged_ports = dict(live.ports)
    if profile.ports is not None:
        merged_ports.update(profile.ports)

    profile_baud = profile.baud
    baud = BaudAssertion(
        uart1=(
            profile_baud.uart1
            if profile_baud is not None and profile_baud.uart1 is not None
            else live.baud.uart1
        ),
        uart2=(
            profile_baud.uart2
            if profile_baud is not None and profile_baud.uart2 is not None
            else live.baud.uart2
        ),
    )

    return ReceiverAssertion(
        baud=baud,
        meas_period_ms=profile.meas_period_ms,
        constellations=(
            profile.constellations
            if profile.constellations is not None
            else live.constellations
        ),
        ports=merged_ports,
        dyn_model=profile.dyn_model
        if profile.dyn_model is not None
        else live.dyn_model,
        tmode_mode=(
            profile.tmode_mode if profile.tmode_mode is not None else live.tmode_mode
        ),
        elevation_mask_deg=(
            profile.elevation_mask_deg
            if profile.elevation_mask_deg is not None
            else live.elevation_mask_deg
        ),
        bds_b2_enabled=(
            profile.bds_b2_enabled
            if profile.bds_b2_enabled is not None
            else live.bds_b2_enabled
        ),
        spi_enabled=(
            profile.spi_enabled if profile.spi_enabled is not None else live.spi_enabled
        ),
        rtcm_stream=RtcmStreamConfig(matrix=_dense_matrix(profile.rtcm_stream.matrix)),
    )
