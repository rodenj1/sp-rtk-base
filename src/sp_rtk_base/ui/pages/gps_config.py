"""Advanced GPS Configuration page — profile picker, live-seeded form, Apply.

Provides the profile-based GPS receiver setup flow (issue #54): a
profile picker tagged with hardware compatibility, a receiver-
configuration view seeded from the live device (RTCM matrix, port
protocols, GNSS constellations), and save-to-flash.

Issue #67 dropped Handoff-to-relay from this page (device-session
concerns live on the Dashboard/Outputs pages, not here). Cancel-
survey-in and Reset GPS were already Survey-page-only, alongside the
rest of the positioning workflow, and stay untouched by that issue —
neither is a profile concern. Save-to-flash stays too: see the
comment on ``flash_card`` below for why it's still load-bearing.

Issue #64 shipped the read-only shell; issue #65 made the RTCM matrix
and data-link port(s) editable, wired to
``POST /api/device/apply-config`` via ``DeviceService.apply_receiver_config``,
plus the "receiver out of sync" indicator (form vs. live receiver,
cleared only by a successful apply).

Issue #66 makes picking a profile actually write into the form — the
whole ``ReceiverAssertion`` shape (ports, GNSS, baud/measurement-rate,
role fields, optimisations, plus the matrix and data-link ports), not
just the matrix — and adds the second, independent "modified from X"
indicator (form vs. *selected profile*, gating Save-as) alongside the
existing "out of sync" one (form vs. *live receiver*, gating Apply).
Save-as, rename, delete and export round out the custom-profile
lifecycle. The ports/GNSS/baud/role section remains a read-only
*display* of form state rather than a click-to-edit grid.

Issue #98 (this revision) makes Apply push the *whole* form, not just
the matrix and data-link ports — it now sends a ``ReceiverApplyRequest``
(the whole ``form`` :class:`~sp_rtk_base.models.profile_models.ReceiverAssertion`
plus ``form_data_link_ports``), and both "out of sync" and the post-apply
verify go through the one shared
:func:`~sp_rtk_base.models.profile_models.diff_receiver_assertions`
per-leaf, path-keyed comparison. This also fixes a standing bug by
construction: ``form`` is always live-seeded, so measurement rate can
no longer silently default to 1 Hz on every Apply press.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# pyright: reportOptionalMemberAccess=false
# pyright: reportOptionalIterable=false
# pyright: reportPrivateUsage=false
# NiceGUI elements have partially unknown types. reportPrivateUsage is
# disabled because the profile-picker dropdown (issue #105) writes
# directly to a raw ``q-select`` element's ``_props['options']`` — the
# public ``ui.select``/``ChoiceElement`` options API rebuilds that prop
# from a plain ``{value: label}`` mapping on every ``update()``, with
# no room for the per-option disable/tooltip/highlight fields its
# ``option`` scoped slot needs (see the comment beside ``picker_select``).

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

from nicegui import ui
from pydantic import ValidationError

from sp_rtk_base.models.config_models import DeviceProfile
from sp_rtk_base.models.device_models import (
    ALL_RTCM_MESSAGE_IDS,
    RTCM_MESSAGE_GROUPS,
    CurrentBaseConfig,
    DeviceCapability,
    DeviceConnectionState,
    DynModel,
    GnssConstellation,
    PortId,
    RtcmOutputPort,
    RtcmPortConfig,
    RtcmRowId,
    SurveyInProgress,
    UbxProtocol,
)
from sp_rtk_base.models.device_models import (
    BaseMode as TmodeMode,
)
from sp_rtk_base.models.hardware_identity import (
    HARDWARE_ANY,
    HARDWARE_UNKNOWN,
    KNOWN_FAMILY_TOKENS,
    HardwareConfidence,
    HardwareIdentity,
    default_selection,
    identity_from_target,
    incompatible_reason,
    is_compatible,
)
from sp_rtk_base.models.profile_models import (
    MATRIX_PORTS,
    ApplyDiffEntry,
    ApplyStepResult,
    ApplyStepWarning,
    BaudAssertion,
    PortProtocolSet,
    Profile,
    ReceiverApplyRequest,
    ReceiverAssertion,
    RtcmStreamConfig,
    diff_receiver_assertions,
    merge_profile_into_assertion,
)
from sp_rtk_base.services import (
    get_config_service,
    get_device_service,
    get_profile_store,
)
from sp_rtk_base.services.device_service import (
    ApplyConfigLinkLostError,
    ApplyConfigRefusedError,
)
from sp_rtk_base.services.drivers import create_driver, list_drivers
from sp_rtk_base.services.drivers.base import GpsReceiverDriver
from sp_rtk_base.services.profile_store import ProfileStore, ProfileStoreError
from sp_rtk_base.ui.layout import page_layout

logger = logging.getLogger(__name__)

BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
DEFAULT_BAUD = 115200

# GNSS constellations shown in the read-only form, in display order.
_GNSS_DISPLAY: list[tuple[str, str]] = [
    ("gps", "GPS"),
    ("glonass", "GLONASS"),
    ("galileo", "Galileo"),
    ("beidou", "BeiDou"),
    ("sbas", "SBAS"),
    ("qzss", "QZSS"),
]

#: Data-link-capable ports — highlighted matrix columns. Mirrors
#: ``profile_models._DATA_LINK_CANDIDATE_PORTS`` (USB excluded).
DATA_LINK_PORTS: frozenset[PortId] = frozenset({PortId.UART1, PortId.UART2})

#: Ports the matrix doesn't manage. A nonzero cell here in the live
#: read-back means the receiver has RTCM enabled somewhere the
#: profile form can't see or control.
_ADVISORY_PORTS: tuple[RtcmOutputPort, ...] = (RtcmOutputPort.I2C, RtcmOutputPort.SPI)

#: The one row every base profile must carry on a data-link port.
REQUIRED_RTCM_ROW: RtcmRowId = RtcmRowId.RTCM_1005


# ---------------------------------------------------------------------------
# Pure helpers — extracted for unit testing (see test_gps_config_helpers.py).
# The page-rendering closure itself drives NiceGUI elements and can't be
# meaningfully unit-tested without a full browser harness (tests/e2e).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfilePickerEntry:
    """One row in the profile picker, tagged with everything it needs to render."""

    profile: Profile
    is_builtin: bool
    compatible: bool
    incompatible_reason: str | None
    is_default: bool


def resolve_identity(
    hardware_target: str | None, hardware_confidence: HardwareConfidence | None
) -> HardwareIdentity:
    """Build a :class:`HardwareIdentity` from device info, or the unknown sentinel."""
    if hardware_target is None or hardware_confidence is None:
        return identity_from_target(HARDWARE_UNKNOWN, HardwareConfidence.UNKNOWN)
    return identity_from_target(hardware_target, hardware_confidence)


def display_label(profile: Profile) -> str:
    """The human-facing label for *profile* — its display name, falling
    back to the slug (``name``) when none is set.

    Every operator-visible rendering of a profile's identity (picker
    rows, provenance labels like "Forked from"/"Modified from", the
    rename dialog's prefill) goes through this one helper so the
    fallback rule lives in exactly one place.
    """
    return profile.display_name or profile.name


def build_picker_entries(
    profiles: list[Profile],
    store: ProfileStore,
    identity: HardwareIdentity,
) -> list[ProfilePickerEntry]:
    """Tag every profile with builtin/compatibility/default state for the picker.

    Mirrors ``api/profiles.py:list_profiles`` — the UI reads the same
    ``ProfileStore`` + ``hardware_identity`` primitives directly rather
    than round-tripping through its own REST API. *profiles* is assumed
    already ordered (built-ins before customs, alphabetical within
    each group — ``ProfileStore.list_profiles`` guarantees this).
    """
    default_name = default_selection(identity, [(p.name, p.hardware) for p in profiles])
    return [
        ProfilePickerEntry(
            profile=p,
            is_builtin=store.is_builtin(p.name),
            compatible=is_compatible(identity, p.hardware),
            incompatible_reason=incompatible_reason(identity, p.hardware),
            is_default=p.name == default_name,
        )
        for p in profiles
    ]


def matrix_cell_on(rtcm: RtcmPortConfig, row: RtcmRowId, port: PortId) -> bool:
    """Whether *row* is enabled on *port* in the live read-back."""
    return rtcm.is_enabled(row, RtcmOutputPort(port.value))


def i2c_spi_advisory_rows(rtcm: RtcmPortConfig) -> list[RtcmRowId]:
    """Row IDs with nonzero RTCM output on a port the matrix doesn't manage."""
    return [
        row_id
        for row_id in ALL_RTCM_MESSAGE_IDS
        if any(rtcm.is_enabled(row_id, p) for p in _ADVISORY_PORTS)
    ]


def rtcm_config_to_matrix(
    rtcm: RtcmPortConfig,
) -> dict[RtcmRowId, dict[PortId, bool]]:
    """Seed the editable boolean matrix from a live ``RtcmPortConfig`` read-back.

    Every catalog row x :data:`MATRIX_PORTS` cell is present (default
    off) so the result compares by plain equality against another
    matrix built the same way — that equality is how the "receiver out
    of sync" indicator works.
    """
    return {
        row_id: {port: matrix_cell_on(rtcm, row_id, port) for port in MATRIX_PORTS}
        for row_id in ALL_RTCM_MESSAGE_IDS
    }


def infer_data_link_ports(matrix: dict[RtcmRowId, dict[PortId, bool]]) -> list[PortId]:
    """UART ports already carrying at least one enabled RTCM row.

    ``data_link_port`` has no CFG key of its own — it can't be read
    back from the receiver — so it's inferred from which UART already
    emits RTCM rather than guessed (issue #54's usage-driven
    revision). USB is never a candidate. An empty result means the
    inference is empty (a factory receiver with no RTCM anywhere) and
    the operator must pick explicitly.
    """
    return [
        port
        for port in (PortId.UART1, PortId.UART2)
        if any(row.get(port, False) for row in matrix.values())
    ]


def apply_blocked_reason(data_link_port: list[PortId]) -> str | None:
    """Why Apply is disabled right now, or ``None`` if it isn't."""
    if not data_link_port:
        return (
            "No data-link port could be inferred — select at least one "
            "UART port below before applying."
        )
    return None


def build_apply_request(
    form: ReceiverAssertion, data_link_port: list[PortId]
) -> ReceiverApplyRequest:
    """Build the whole-form Apply envelope (issue #98).

    Apply now sends the entire form — the matrix-only default this
    used to have (issue #65/#66) is gone; every caller (the actual
    Apply call, Save-as, and the "modified from X" comparison) passes
    the current ``form`` :class:`ReceiverAssertion` through.

    BeiDou B2 is coerced off here when BeiDou itself is off (issue
    #100), regardless of what the form's raw ``bds_b2_enabled`` holds —
    this is the one choke point every caller (Apply, Save-as, the
    "modified from X" comparison) goes through, so the outgoing assert
    always agrees with what the firmware would renormalise on its own,
    pre-empting a permanently unclearable difference.

    Raises:
        pydantic.ValidationError: If the resulting request fails a
            context-free rule (e.g. 1005 missing from every chosen
            data-link port) — a client-side pre-write refusal, nothing
            is sent to the receiver.
    """
    assertion = form
    if bds_b2_control_disabled(form.constellations) and form.bds_b2_enabled:
        assertion = form.model_copy(update={"bds_b2_enabled": False})
    return ReceiverApplyRequest(assertion=assertion, data_link_port=data_link_port)


def receiver_config_from_profile(
    profile: Profile, live: ReceiverAssertion
) -> ReceiverApplyRequest:
    """The ``ReceiverApplyRequest`` picking *profile* against *live* would
    produce.

    Used to compare "the form" against "the selected profile" as the
    same type. Deliberately built through the same
    :func:`merge_profile_into_assertion` resolution
    :func:`_select_profile` (in the page closure) uses to seed the form
    from this same profile against the current *live* state — a stored
    profile's matrix is *sparse* (absent cell = off) and its optional
    fields mean "leave the live value alone", while the form is always
    fully populated. Comparing a freshly-picked, untouched form against
    a bare ``profile.model_dump()`` would almost always report
    "modified" unless both sides go through the identical resolution.
    """
    merged = merge_profile_into_assertion(profile, live)
    return build_apply_request(merged, list(profile.data_link_port))


def is_modified_from_profile(
    form_request: ReceiverApplyRequest, profile: Profile | None, live: ReceiverAssertion
) -> bool:
    """Whether *form_request* diverges from *profile* — the second,
    independent indicator (form vs. selected profile), distinct from "out
    of sync" (form vs. live receiver). ``False`` when no profile is
    selected — there is nothing to have diverged from."""
    if profile is None:
        return False
    return form_request != receiver_config_from_profile(profile, live)


def save_as_enabled(
    form_request: ReceiverApplyRequest | None,
    profile: Profile | None,
    live: ReceiverAssertion,
) -> bool:
    """Save-as is available whenever the form is valid, suppressed only when
    a *selected* profile still exactly equals the form."""
    if form_request is None:
        return False
    return profile is None or is_modified_from_profile(form_request, profile, live)


def suggest_profile_name(profile: Profile | None, hardware_target: str) -> str:
    """Auto-suggested Save-as name.

    ``"<source> (copy)"`` when forked from a profile, a
    hardware-derived name when capturing a bare live config — both
    sanitized to the store's filesystem-safe slug charset
    (``^[A-Za-z0-9_-]+$``, see ``profile_store._SAFE_NAME_RE``), since
    a name containing a space or parentheses would fail validation
    the instant an operator hits Save without editing it.
    """
    if profile is not None:
        return f"{profile.name}-copy"
    return f"{hardware_target.lower()}-captured"


def resolve_save_hardware(profile: Profile | None, identity: HardwareIdentity) -> str:
    """The ``hardware`` tag a newly-saved profile should carry.

    A fork keeps its source profile's tag unchanged. A bare capture
    (no profile selected) tags itself with the connected receiver's
    resolved identity when that's a valid profile-hardware token
    (a confirmed specific model, or any family token — ``is_specific_model``
    isn't checked here, since a family token is just as valid a target as a
    specific model) — falling back to :data:`HARDWARE_ANY` only when identity
    hasn't resolved to a usable token at all (``unknown``, or an ``inferred``
    guess of a specific model that a family token can't stand in for).
    """
    if profile is not None:
        return profile.hardware
    if identity.confidence == HardwareConfidence.CONFIRMED:
        return identity.target
    if identity.target in KNOWN_FAMILY_TOKENS:
        return identity.target
    return HARDWARE_ANY


def build_saved_profile(
    name: str,
    form_request: ReceiverApplyRequest,
    hardware: str,
    forked_from: str | None,
) -> Profile:
    """Construct the ``Profile`` document Save-as persists.

    Flattens *form_request*'s envelope — ``assertion``'s fields plus
    ``data_link_port`` — onto ``Profile``'s own field layout (issue
    #98: ``data_link_port`` moved off ``ReceiverConfig``, so it's no
    longer part of ``assertion.model_dump()`` and must be passed
    separately).
    """
    return Profile(
        **form_request.assertion.model_dump(),
        data_link_port=form_request.data_link_port,
        name=name,
        version=1,
        hardware=hardware,
        forked_from=forked_from,
    )


def resolve_ports_display(
    ports: dict[PortId, PortProtocolSet],
) -> dict[PortId, tuple[list[str], list[str]]]:
    """Per-port (in, out) protocol name lists the "Port Protocols" display
    renders, straight from the form's always-populated ``ports`` map."""
    empty = PortProtocolSet()
    return {
        port: (
            [p.value for p in ports.get(port, empty).in_],
            [p.value for p in ports.get(port, empty).out],
        )
        for port in (PortId.UART1, PortId.UART2, PortId.USB)
    }


def resolve_gnss_display(constellations: list[GnssConstellation]) -> dict[str, bool]:
    """Constellation -> enabled map the "GNSS Constellations" display
    renders, straight from the form's always-populated ``constellations``
    list."""
    wanted = set(constellations)
    return {c.value: c in wanted for c in GnssConstellation}


#: Base mode states the hardware-section control offers directly.
#: ``survey_in`` is seedable (a live receiver can be found running one)
#: but never itself selectable — starting a survey stays the Survey
#: page's transition, since it's edge-triggered and needs survey
#: parameters (issue #100).
SELECTABLE_TMODE_MODES: tuple[TmodeMode, ...] = (TmodeMode.DISABLED, TmodeMode.FIXED)


def tmode_mode_locked(mode: TmodeMode) -> bool:
    """Whether the base-mode control is showing *mode* as a locked,
    non-selectable current value rather than a normal picker.

    True only for ``survey_in`` — the one state the control must be
    able to represent (so a receiver mid-survey shows no phantom
    unapplied change) without offering it as something an operator can
    pick here.
    """
    return mode == TmodeMode.SURVEY_IN


def bds_b2_control_disabled(constellations: list[GnssConstellation]) -> bool:
    """Whether the BeiDou B2 control should be greyed out.

    True whenever BeiDou itself is off — B2 has nothing to modulate
    without it. Doesn't touch the underlying value; that's
    :func:`build_apply_request`'s job (see its docstring) so the read
    can still report the receiver's truth even in the rare case where
    the two are already inconsistent.
    """
    return GnssConstellation.BEIDOU not in constellations


def placeholder_assertion() -> ReceiverAssertion:
    """An empty ``ReceiverAssertion`` for the page's pre-connect state,
    before any live receiver read has happened."""
    return ReceiverAssertion(
        baud=BaudAssertion(uart1=DEFAULT_BAUD, uart2=DEFAULT_BAUD),
        meas_period_ms=1000,
        constellations=[],
        ports={},
        dyn_model=DynModel.PORTABLE,
        tmode_mode=TmodeMode.DISABLED,
        elevation_mask_deg=0,
        bds_b2_enabled=False,
        spi_enabled=False,
        rtcm_stream=RtcmStreamConfig(matrix=rtcm_config_to_matrix(RtcmPortConfig())),
    )


def _format_diff_value(value: bool | int | str) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def format_leaf_diff(diff: ApplyDiffEntry) -> str:
    """Render one post-apply read-back mismatch as a human-readable line.

    *diff.path* already names the exact leaf (a matrix cell, a port's
    protocol, a constellation, or a plain scalar field) — see
    :func:`sp_rtk_base.models.profile_models.diff_receiver_assertions`.
    """
    return (
        f"{diff.path}: expected {_format_diff_value(diff.expected)}, "
        f"got {_format_diff_value(diff.actual)}"
    )


def row_slug(row_id: RtcmRowId) -> str:
    """A CSS-class-safe token for *row_id* (``"4072.0"`` -> ``"4072_0"``) — a
    stable hook the e2e suite uses to target a specific matrix row/cell."""
    return row_id.value.replace(".", "_")


# ---------------------------------------------------------------------------
# Result surfaces (issue #101) — the three-state badge, inline-marker
# provenance split, headline verdict, step log and warning strip that
# turn Apply's already-rich ``ApplyConfigResult`` (issue #99) into
# something the operator can read at a glance.
# ---------------------------------------------------------------------------


def rtcm_diff_path(msg_id: RtcmRowId, port: PortId) -> str:
    """The :func:`~sp_rtk_base.models.profile_models.diff_receiver_assertions`
    path for one matrix cell — the single place that format is built, so
    a mutation handler clearing provenance and the render function
    marking it can never drift apart."""
    return f"rtcm.{msg_id.value}.{port.value}"


def port_protocol_diff_path(
    port: PortId, direction: Literal["in", "out"], protocol: UbxProtocol
) -> str:
    """The diff path for one port's one protocol, one direction."""
    return f"ports.{port.value}.{direction}.{protocol.value}"


def constellation_diff_path(constellation: GnssConstellation) -> str:
    """The diff path for one GNSS constellation."""
    return f"constellations.{constellation.value}"


def partition_diff_by_provenance(
    diff: list[ApplyDiffEntry], failed_paths: set[str]
) -> tuple[list[ApplyDiffEntry], list[ApplyDiffEntry]]:
    """Split *diff* (form vs. live) into ``(failed, pending)`` by provenance.

    A leaf is *failed* when it appears in the last apply's diff and
    hasn't been edited since — *failed_paths* is exactly that set, and
    every mutation handler on the page drops a path from it the moment
    the operator edits that field. Anything else that currently
    differs is a *pending* edit — the resting state of an editable
    form, never a fault.
    """
    failed = [d for d in diff if d.path in failed_paths]
    pending = [d for d in diff if d.path not in failed_paths]
    return failed, pending


@dataclass(frozen=True)
class ResultBadgeState:
    """One badge, three states — see :func:`result_badge_state`."""

    kind: Literal["in_sync", "pending", "failed"]
    count: int


def result_badge_state(
    failed: list[ApplyDiffEntry], pending: list[ApplyDiffEntry]
) -> ResultBadgeState:
    """The one-badge, three-state verdict.

    Verification failure takes priority over pending edits — the badge
    never shows both — and either count covers exactly the leaves
    :func:`partition_diff_by_provenance` produced, which is exactly
    the set Apply itself asserts: no carve-outs.
    """
    if failed:
        return ResultBadgeState(kind="failed", count=len(failed))
    if pending:
        return ResultBadgeState(kind="pending", count=len(pending))
    return ResultBadgeState(kind="in_sync", count=0)


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"


def badge_label(state: ResultBadgeState) -> str:
    """The badge's text for *state*."""
    if state.kind == "in_sync":
        return "In sync"
    if state.kind == "failed":
        return f"{state.count} {_plural(state.count, 'field')} failed verification"
    return f"{state.count} unapplied {_plural(state.count, 'change')}"


#: Quasar colour per badge state. ``pending`` stays a neutral grey —
#: the resting state of an editable form shouldn't train the operator
#: to read amber as trouble; only a genuine verification failure does.
_BADGE_COLOR: dict[str, str] = {
    "in_sync": "positive",
    "pending": "grey-7",
    "failed": "warning",
}


def badge_color(state: ResultBadgeState) -> str:
    """The Quasar colour name for *state*."""
    return _BADGE_COLOR[state.kind]


def apply_headline(status: Literal["ok", "failed"], warning_count: int) -> str:
    """The headline verdict line — the outcome without reading, plus a
    neutral warning count when warnings are present so a green verdict
    never sits directly above an amber strip the operator has no
    reason to read."""
    verdict = (
        "Applied and verified ✓"
        if status == "ok"
        else "Applied, but verification found mismatches — nothing was rolled back."
    )
    if not warning_count:
        return verdict
    return f"{verdict} ({warning_count} {_plural(warning_count, 'warning')})"


def apply_warning_lines(
    warnings: list[str], step_warnings: list[ApplyStepWarning]
) -> list[str]:
    """One line per warning for the strip — never joined into a single
    string. Step warnings are prefixed with the step that produced
    them; the non-blocking throughput warnings need no prefix."""
    return [*warnings, *(f"{w.step}: {w.message}" for w in step_warnings)]


#: Icon per :class:`~sp_rtk_base.models.profile_models.ApplyStepResult`
#: status, for the step log's append-only line.
_STEP_STATUS_ICON: dict[str, str] = {"ok": "✓", "failed": "✗", "skipped": "—"}


def format_step_log_entry(step: ApplyStepResult) -> str:
    """One step-log line. Order and partial application are the two
    things only this surface can express."""
    return f"{_STEP_STATUS_ICON[step.status]} {step.step}: {step.status}"


# ---------------------------------------------------------------------------
# Fixed Position three-step card (issue #96) — deliberately separate from
# the profile-form helpers above. This card's state is derived entirely
# from live receiver polls (``CurrentBaseConfig`` + ``SurveyInProgress``),
# never from the selected/applied profile — a profile has no position
# field, per the card's own "not part of the profile" copy.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixedPositionStepState:
    """Which step of Apply -> survey-in -> fixed-position the receiver is
    currently at, plus the per-step status text the card renders.

    ``current_step`` is 1 (apply profile), 2 (run survey-in) or 3 (fixed
    position set) — steps before it are "done", steps after are "pending".
    """

    current_step: int
    survey_state_text: str
    fixed_pos_text: str


def fixed_position_step_state(
    base_config: CurrentBaseConfig, survey: SurveyInProgress
) -> FixedPositionStepState:
    """Derive the three-step card's current step + status text.

    Step 3 (fixed position set) is current once the receiver reports
    ``BaseMode.FIXED`` — a promoted survey-in and a manually restored
    position both land here identically, and survey-in's own state is
    irrelevant once fixed mode is reached. Step 2 (run survey-in) is
    current while the receiver is in ``BaseMode.SURVEY_IN`` *or* the
    survey-in poll reports ``active`` (covers a receiver mid-survey
    whose TMODE read-back hasn't caught up yet). Anything else is step
    1 (apply profile) — the baseline a freshly-applied, not-yet-surveyed
    base sits in.
    """
    if base_config.mode == TmodeMode.FIXED:
        return FixedPositionStepState(
            current_step=3,
            survey_state_text="— complete" if survey.valid else "— done",
            fixed_pos_text=(
                f"{base_config.latitude:.7f}°, {base_config.longitude:.7f}° "
                f"± {base_config.accuracy_mm} mm"
            ),
        )
    if base_config.mode == TmodeMode.SURVEY_IN or survey.active:
        return FixedPositionStepState(
            current_step=2,
            survey_state_text=(
                f"— running ({survey.duration_seconds}s, "
                f"{survey.mean_accuracy_mm:.0f} mm, {survey.observations} obs)"
            ),
            fixed_pos_text="— none",
        )
    return FixedPositionStepState(
        current_step=1,
        survey_state_text="— not started",
        fixed_pos_text="— none",
    )


@ui.page("/gps-config")
def gps_config_page() -> None:
    """Render the advanced GPS configuration page."""
    svc = get_device_service()
    config_svc = get_config_service()
    profile_store = get_profile_store()

    with page_layout("Advanced GPS"):
        ui.label("Advanced GPS Configuration").classes("text-h4 text-white q-mb-md")

        # ================================================================
        # Section A: Connection
        # ================================================================
        with ui.card().classes("w-full q-pa-md"):
            ui.label("Connection").classes("text-h6 text-white")
            ui.separator()

            # State elements
            status_row = ui.row().classes("items-center gap-2 q-mt-sm")
            error_label = ui.label("").classes("text-negative q-mt-xs")
            error_label.set_visibility(False)

            # Port and baud selectors
            with ui.row().classes("w-full gap-4 q-mt-sm sp-metric-row"):
                port_select = ui.select(
                    options=[],
                    label="Serial Port",
                    with_input=True,
                ).classes("col-grow")

                baud_select = ui.select(
                    options={r: str(r) for r in BAUD_RATES},
                    label="Baud Rate",
                    value=DEFAULT_BAUD,
                ).classes("w-40")

                driver_select = ui.select(
                    options=list_drivers(),
                    label="Driver",
                    value="ublox",
                ).classes("w-40")

            # Action buttons
            with ui.row().classes("gap-2 q-mt-sm items-center"):
                connect_btn = ui.button("Connect", icon="link")
                disconnect_btn = ui.button("Disconnect", icon="link_off").props(
                    "color=grey"
                )
                cancel_btn = ui.button("Cancel", icon="cancel").props(
                    "color=negative outline"
                )
                cancel_btn.set_visibility(False)
                reload_device_btn = ui.button(
                    "Reload Device Config", icon="sync"
                ).props("color=info outline")
                reload_device_btn.set_visibility(False)
                refresh_btn = (
                    ui.button("", icon="refresh")
                    .props("flat round color=white")
                    .tooltip("Refresh serial port list")
                )

            # Device info card (hidden until connected)
            info_card = ui.card().classes("w-full q-pa-md q-mt-md")
            info_card.set_visibility(False)

        # ================================================================
        # Section B: Profile picker (hidden until connected)
        # ================================================================
        profile_card = ui.card().classes("w-full q-pa-md q-mt-md")
        profile_card.set_visibility(False)

        with profile_card:
            ui.label("Profile").classes("text-h6 text-white")
            ui.separator()
            identity_label = ui.label("").classes("text-caption text-grey-4 q-mt-xs")
            unconfirmed_banner = (
                ui.label(
                    "Unconfirmed hardware — only family- or any-tagged profiles are "
                    "enabled, and no profile is suggested."
                )
                .classes("text-warning text-caption q-mt-xs q-pa-sm")
                .style("border: 1px solid #5a4520; border-radius: 4px")
            )
            unconfirmed_banner.set_visibility(False)

            # The dropdown (issue #105 — replaces the #64/#65/#66 card
            # list). Built as a raw Quasar ``q-select`` via ``ui.element``
            # rather than NiceGUI's ``ui.select`` wrapper: the wrapper's
            # ``ChoiceElement`` maps options to plain ``{value, label}``
            # pairs and rebuilds that list itself on every ``update()``,
            # leaving nowhere to hang the extra per-option
            # disable/tooltip/highlight fields the ``option`` scoped slot
            # below needs. Working against the raw element keeps full
            # control of the ``options`` prop's shape.
            with ui.row().classes("items-center gap-2 q-mt-sm flex-wrap"):
                picker_select = (
                    ui.element("q-select")
                    .props(
                        "label='Select profile' emit-value map-options "
                        "option-value=value option-label=label "
                        "options-dense dense outlined behavior=menu"
                    )
                    .classes("profile-picker")
                    .style("min-width: 320px")
                )
                # Custom ``option`` slot: Quasar's own ``option-disable``
                # prop would set the native ``disable`` prop on the
                # rendered ``q-item``, which sets ``pointer-events:none``
                # and silently kills hover — exactly what the tooltip
                # explaining *why* a profile is disabled needs. So
                # selection is gated by hand (``props.opt.disable`` guards
                # the click) instead, keeping the item hoverable.
                #
                # NB: NiceGUI's own slot-template wrapper (see
                # ``renderRecursively`` in its bundled ``nicegui.js``)
                # exposes the Quasar scoped-slot object as a template
                # variable named ``props`` — *not* Quasar's own docs
                # convention ``scope`` — regardless of which slot this
                # is, so every reference below is ``props.*``.
                picker_select.add_slot(
                    "option",
                    r"""
                    <q-item
                      v-bind:clickable="!props.opt.disable"
                      v-bind:class="[
                        'profile-option',
                        'profile-option-' + props.opt.value,
                        props.opt.disable ? 'profile-option-disabled' : '',
                        props.opt.highlighted ? 'profile-option-suggested bg-primary-1' : '',
                      ]"
                      v-on:click="() => { if (!props.opt.disable) props.toggleOption(props.opt) }"
                    >
                      <q-item-section>
                        <q-item-label v-bind:class="props.opt.disable ? 'text-grey-6' : ''">
                          {{ props.opt.label }}
                          <q-badge
                            v-if="props.opt.highlighted"
                            color="primary"
                            class="profile-suggested-badge q-ml-sm"
                          >Suggested</q-badge>
                          <q-badge
                            outline
                            color="grey"
                            class="profile-builtin-badge q-ml-sm"
                          >{{ props.opt.builtin ? 'built-in' : 'custom' }}</q-badge>
                        </q-item-label>
                      </q-item-section>
                      <q-tooltip
                        v-if="props.opt.disable"
                        class="profile-option-tooltip"
                      >{{ props.opt.tooltip }}</q-tooltip>
                    </q-item>
                    """,
                )
                picker_select.on(
                    "update:model-value",
                    lambda e: _select_profile_by_name(e.args),
                )

                picker_rename_icon = (
                    ui.icon("edit")
                    .classes("profile-rename-icon text-grey-4 cursor-pointer")
                    .tooltip("Rename")
                )
                picker_delete_icon = (
                    ui.icon("delete")
                    .classes("profile-delete-icon text-grey-4 cursor-pointer")
                    .tooltip("Delete")
                )
                picker_export_icon = (
                    ui.icon("download")
                    .classes("profile-export-icon text-grey-4 cursor-pointer")
                    .tooltip("Export")
                )
                picker_rename_icon.on(
                    "click",
                    lambda: (
                        _open_rename_dialog(selected_profile)
                        if selected_profile is not None
                        else None
                    ),
                )
                picker_delete_icon.on(
                    "click",
                    lambda: (
                        _open_delete_dialog(selected_profile)
                        if selected_profile is not None
                        else None
                    ),
                )
                picker_export_icon.on(
                    "click",
                    lambda: (
                        _export_profile(selected_profile.name)
                        if selected_profile is not None
                        else None
                    ),
                )

                # The profile-relation indicator (issue #105 — moved out
                # of the action row, where it sat as a same-weight
                # sibling of receiver status and used warning colour for
                # "modified from X". It answers "should I save this?",
                # never "something's wrong", so it renders here next to
                # the control that resolves it, always in a neutral
                # colour — see ``_render_modified_indicator``.
                modified_badge = (
                    ui.badge("").classes("modified-badge").props("color=grey-7 outline")
                )
                modified_badge.set_visibility(False)

        # ================================================================
        # Section C: Receiver configuration — read-only, seeded from the
        # live receiver (hidden until connected)
        # ================================================================
        config_card = ui.card().classes("w-full q-pa-md q-mt-md")
        config_card.set_visibility(False)

        with config_card:
            ui.label("Receiver Configuration").classes("text-h6 text-white")
            ui.label(
                "Every field below is seeded from the live receiver, or a "
                "picked profile once one's selected, and every field is "
                "editable — model, firmware, protocol and hardware version "
                "on the Connection card above are the only genuine "
                "read-only status fields. Edit anything, then Apply to "
                "assert the whole form and verify the read-back."
            ).classes("text-grey-4 q-mt-xs text-caption")
            ui.separator()

            ui.label("Port Protocols").classes("text-subtitle2 text-white q-mt-sm")
            ports_view = ui.column().classes("q-mt-xs gap-1 w-full")

            ui.label("GNSS Constellations").classes("text-subtitle2 text-white q-mt-md")
            gnss_view = ui.row().classes("q-mt-xs gap-2 flex-wrap")

            ui.label("Hardware Section").classes("text-subtitle2 text-white q-mt-md")
            hw_extras_view = ui.row().classes("hw-extras-view q-mt-xs gap-4 flex-wrap")

            ui.label("RTCM Stream").classes("text-subtitle2 text-white q-mt-md")
            ui.label(
                "Boolean matrix — on/off per message x port. Click a cell to "
                "toggle it. Highlighted columns are data-link ports (where "
                "rovers read corrections); USB is local diagnostics only."
            ).classes("text-grey-4 q-mt-xs text-caption")
            matrix_view = ui.column().classes("q-mt-sm gap-0 w-full")

            advisory_label = (
                ui.label("")
                .classes("text-warning text-caption q-mt-sm q-pa-sm")
                .style("border: 1px solid #5a4520; border-radius: 4px")
            )
            advisory_label.set_visibility(False)

            ui.label("Data-Link Port(s)").classes("text-subtitle2 text-white q-mt-md")
            data_link_hint = ui.label("").classes(
                "data-link-hint text-grey-4 q-mt-xs text-caption"
            )
            data_link_blocked_label = (
                ui.label("")
                .classes("data-link-blocked text-warning text-caption q-mt-xs q-pa-sm")
                .style("border: 1px solid #5a4520; border-radius: 4px")
            )
            data_link_blocked_label.set_visibility(False)
            data_link_picker = ui.row().classes("data-link-picker q-mt-xs gap-4")

            with ui.row().classes("items-center gap-3 q-mt-md"):
                apply_btn = ui.button("Apply", icon="bolt").props("color=primary")
                sync_badge = ui.badge("").classes("sync-badge").props("color=positive")
                save_as_btn = (
                    ui.button("Save as…", icon="save")
                    .classes("save-as-btn")
                    .props("color=secondary outline")
                )

            apply_result_label = (
                ui.label("")
                .classes("apply-result text-caption q-mt-sm q-pa-sm")
                .style("border: 1px solid #333; border-radius: 4px")
            )
            apply_result_label.set_visibility(False)

            # Warning strip (issue #101) — the Survey page's amber-card
            # idiom for pre-flight advisories, reused here for Apply's
            # non-blocking step warnings. One ``⚠`` label per warning,
            # never a string-joined blob; replaced whole on every Apply
            # attempt and cleared on disconnect. No remedy button — the
            # remedy is the Apply button already in this action row.
            warning_strip_card = (
                ui.card()
                .classes("warning-strip w-full q-pa-sm q-mt-sm")
                .style("background-color: #3a2a10")
            )
            warning_strip_card.set_visibility(False)
            with warning_strip_card:
                warning_strip_view = ui.column().classes("gap-0")

            # Session-only, append-only step log (issue #101) — the only
            # surface that can express ordering and partial application.
            # Grows across every Apply this connection makes; cleared on
            # disconnect, because a surviving log would narrate a
            # possibly-detached receiver.
            ui.label("Apply Step Log").classes(
                "step-log-label text-subtitle2 text-white q-mt-md"
            )
            step_log_view = ui.column().classes("step-log q-mt-xs gap-0")

        # ---- Save-as dialog ----
        with ui.dialog() as save_as_dialog, ui.card().classes("q-pa-md"):
            ui.label("Save as new profile").classes("text-h6 text-white")
            ui.separator()
            save_as_name_input = ui.input("Name").classes("save-as-name w-full q-mt-sm")
            save_as_from_label = ui.label("").classes(
                "save-as-from text-caption text-grey-4 q-mt-xs"
            )
            save_as_error_label = ui.label("").classes(
                "save-as-error text-negative text-caption q-mt-xs"
            )
            save_as_error_label.set_visibility(False)
            with ui.row().classes("justify-end gap-2 q-mt-md"):
                ui.button("Cancel", on_click=save_as_dialog.close).props("flat")
                save_as_confirm_btn = (
                    ui.button("Create")
                    .classes("save-as-confirm-btn")
                    .props("color=primary")
                )

        # ---- Rename dialog (customs only) ----
        with ui.dialog() as rename_dialog, ui.card().classes("q-pa-md"):
            ui.label("Rename profile").classes("text-h6 text-white")
            ui.separator()
            rename_name_input = ui.input("Display name").classes(
                "rename-name w-full q-mt-sm"
            )
            rename_error_label = ui.label("").classes(
                "rename-error text-negative text-caption q-mt-xs"
            )
            rename_error_label.set_visibility(False)
            with ui.row().classes("justify-end gap-2 q-mt-md"):
                ui.button("Cancel", on_click=rename_dialog.close).props("flat")
                rename_confirm_btn = (
                    ui.button("Rename")
                    .classes("rename-confirm-btn")
                    .props("color=primary")
                )

        # ---- Delete confirm dialog (customs only) ----
        with ui.dialog() as delete_dialog, ui.card().classes("q-pa-md"):
            ui.label("Delete profile?").classes("text-h6 text-white")
            ui.separator()
            delete_confirm_label = ui.label("").classes("q-mt-sm")
            ui.label("This cannot be undone. The live receiver is untouched.").classes(
                "text-caption text-grey-4 q-mt-xs"
            )
            with ui.row().classes("justify-end gap-2 q-mt-md"):
                ui.button("Cancel", on_click=delete_dialog.close).props("flat")
                delete_confirm_btn = (
                    ui.button("Delete")
                    .classes("delete-confirm-btn")
                    .props("color=negative")
                )

        # ================================================================
        # Section D: Save to Flash (hidden until connected + capable)
        #
        # Issue #67 removed Handoff-to-relay from this page and looked at
        # dropping this control too, on the premise (stated in the issue)
        # that every profile write is already layer=5 (RAM+Flash). That
        # premise is false for one field: ``apply_receiver_config`` writes
        # ``ReceiverConfig.constellations`` via ``UbloxDriver.configure_gnss()``,
        # which sends the legacy UBX-CFG-GNSS SET message — a RAM-only
        # write with no layer concept, unlike every CFG-VALSET writer
        # elsewhere in that same apply sequence. (The standalone, UI-less
        # ``PUT /api/device/gnss`` endpoint shares the same RAM-only
        # method, so it has the identical gap.) So a constellation change
        # — made through Apply on this very page — is unpersisted across
        # a reset or reconnect without an explicit flash, and this
        # control stays load-bearing until GNSS constellation selection
        # is migrated to CFG-VALSET (e.g. per-constellation
        # ``CFG_SIGNAL_*_ENA`` keys).
        # ================================================================
        flash_card = ui.card().classes("w-full q-pa-md q-mt-md")
        flash_card.set_visibility(False)

        with flash_card:
            with ui.row().classes("items-center gap-4"):
                save_flash_btn = ui.button("Save to Flash", icon="save").props(
                    "color=warning"
                )
                ui.label(
                    "Persist current receiver configuration to non-volatile memory"
                ).classes("text-grey-4")

        # ================================================================
        # Section E: Fixed Position — three-step Apply -> survey-in ->
        # fixed-position card (issue #96). Hidden until connected, like
        # every other card but Connection. Deliberately NOT part of the
        # Profile section above — a profile has no position field, and
        # the card says so explicitly. Survey-in and position-setting
        # stay owned by the Survey page; this card only links there
        # rather than duplicating those controls (issue #96 acceptance
        # criteria).
        # ================================================================
        fixed_position_card = ui.card().classes(
            "fixed-position-card w-full q-pa-md q-mt-md"
        )
        fixed_position_card.set_visibility(False)

        with fixed_position_card:
            with ui.row().classes("items-center gap-2"):
                ui.label("Fixed Position").classes("text-h6 text-white")
                ui.badge("separate operator step — not part of the profile").classes(
                    "fixed-position-not-in-profile"
                ).props("outline color=grey")
            ui.separator()
            ui.label(
                "A profile has no position field. After Apply, the base is a "
                "correctly-configured fixed-mode receiver still without a "
                "fixed point — positioning is your deliberate step, "
                "unchanged from today's Survey-In page."
            ).classes("text-grey-4 q-mt-xs text-caption")

            with ui.row().classes(
                "fixed-position-steps items-center gap-2 q-mt-md flex-wrap"
            ):
                fp_step1_label = ui.label("").classes("fixed-position-step-1 q-pa-xs")
                ui.icon("arrow_forward").classes("text-grey-6")
                fp_step2_label = ui.label("").classes("fixed-position-step-2 q-pa-xs")
                ui.icon("arrow_forward").classes("text-grey-6")
                fp_step3_label = ui.label("").classes("fixed-position-step-3 q-pa-xs")

            with ui.row().classes("items-center gap-2 q-mt-md"):
                ui.button("Run survey-in", icon="open_in_new").classes(
                    "fixed-position-survey-link"
                ).props("outline color=primary").on(
                    "click", lambda: ui.navigate.to("/survey")
                )
                ui.button("Set fixed position", icon="open_in_new").classes(
                    "fixed-position-manual-link"
                ).props("outline color=primary").on(
                    "click", lambda: ui.navigate.to("/survey")
                )

            ui.label(
                "Survey-in is a live observation session (see Survey) — "
                "this step is shown here only to make the apply -> "
                "position sequence legible at a glance. It never blocks "
                "Apply, and the profile never carries it."
            ).classes("text-grey-5 q-mt-sm text-caption")

        # ================================================================
        # Event handlers
        # ================================================================

        def _refresh_ports() -> None:
            """Reload serial port list from the driver."""
            try:
                ports = GpsReceiverDriver.list_serial_ports()
                options: dict[str, str] = {}
                for p in ports:
                    star = " ⭐" if p.is_gps else ""
                    options[p.port] = f"{p.port} — {p.description}{star}"
                port_select.options = options  # type: ignore[assignment]
                port_select.update()
                if ports:
                    port_select.value = ports[0].port
            except Exception as exc:
                logger.warning("Failed to list ports: %s", exc)

        def _load_saved_device_settings() -> None:
            """Load saved port/baud/driver from config and pre-fill."""
            profile = config_svc.get_device_profile()
            if profile and profile.port:
                port_select.value = profile.port
            if profile and profile.baud_rate:
                baud_select.value = profile.baud_rate
            if profile and profile.vendor:
                driver_select.value = profile.vendor

        def _save_device_settings() -> None:
            """Persist current port/baud/driver to config."""
            try:
                config_svc.save_device_profile(
                    DeviceProfile(
                        port=str(port_select.value or ""),
                        baud_rate=int(baud_select.value or DEFAULT_BAUD),
                        vendor=str(driver_select.value or "ublox"),
                    )
                )
            except Exception:
                pass  # Non-critical

        def _render_picker() -> None:
            """Render the profile picker dropdown from the current device identity.

            Populates the raw ``q-select``'s ``options`` prop directly
            (each entry a dict carrying ``value``/``label`` plus the
            ``disable``/``tooltip``/``highlighted``/``builtin`` fields the
            ``option`` scoped slot renders) rather than going through
            NiceGUI's ``ui.select`` options API, which has no room for
            those extra fields — see the comment where ``picker_select``
            is built.
            """
            identity = _current_identity()
            identity_label.text = (
                f"Connected receiver hardware: {identity.target} "
                f"({identity.confidence.value})"
            )
            unconfirmed_banner.set_visibility(
                identity.confidence != HardwareConfidence.CONFIRMED
            )

            entries = build_picker_entries(
                profile_store.list_profiles(), profile_store, identity
            )

            picker_select._props["options"] = [
                {
                    "value": entry.profile.name,
                    "label": display_label(entry.profile),
                    "disable": not entry.compatible,
                    "tooltip": entry.incompatible_reason or "",
                    "highlighted": entry.is_default,
                    "builtin": entry.is_builtin,
                }
                for entry in entries
            ]
            picker_select._props["model-value"] = (
                selected_profile.name if selected_profile is not None else None
            )
            picker_select.update()

            # Rename/delete/export act on whichever profile is currently
            # selected (issue #105) — customs-only, same as before.
            is_custom_selected = selected_profile is not None and not (
                profile_store.is_builtin(selected_profile.name)
            )
            for icon in (
                picker_rename_icon,
                picker_delete_icon,
                picker_export_icon,
            ):
                icon.set_visibility(is_custom_selected)

        def _select_profile_by_name(name: str | None) -> None:
            """``update:model-value`` handler for the picker dropdown.

            Looks the picked value back up as a :class:`Profile` and
            delegates to :func:`_select_profile` — the dropdown emits the
            profile's slug (``option-value=value``/``emit-value``), not
            the object itself.
            """
            if not name:
                return
            profile = profile_store.get_profile(name)
            if profile is None:
                return
            _select_profile(profile)

        def _select_profile(profile: Profile) -> None:
            """Pick a profile — pre-fills the whole form (issue #66).

            This is expected to put the form out of sync with the
            receiver (per spec that's correct and legible, precisely
            what Apply is for) — ``_on_form_changed`` recomputes that
            indicator same as any other edit. The form remains the
            source of truth afterwards: further matrix/data-link edits
            mutate it same as before, independent of the profile pick.
            """
            nonlocal selected_profile, form, form_data_link_ports
            selected_profile = profile
            form = merge_profile_into_assertion(profile, live)
            form_data_link_ports = list(profile.data_link_port)
            # A profile pick is a bulk edit of every field it touches —
            # same provenance rule as any other edit (issue #101).
            failed_paths.clear()

            _render_matrix()
            _render_ports_view()
            _render_gnss_view()
            _render_hw_extras_view()
            _render_picker()
            _on_form_changed()

        def _current_identity() -> HardwareIdentity:
            info = svc.device_info
            return resolve_identity(
                info.hardware_target if info else None,
                info.hardware_confidence if info else None,
            )

        def _open_save_as_dialog() -> None:
            identity = _current_identity()
            save_as_name_input.value = suggest_profile_name(
                selected_profile, identity.target
            )
            save_as_from_label.text = (
                f"Forked from: {display_label(selected_profile)}"
                if selected_profile
                else ""
            )
            save_as_from_label.set_visibility(selected_profile is not None)
            save_as_error_label.set_visibility(False)
            save_as_dialog.open()

        def _confirm_save_as() -> None:
            nonlocal selected_profile
            name = (save_as_name_input.value or "").strip()
            if not name:
                save_as_error_label.text = "Name is required"
                save_as_error_label.set_visibility(True)
                return

            form_request = _current_form_request()
            if form_request is None:
                save_as_error_label.text = (
                    "The form doesn't currently validate — fix the RTCM "
                    "matrix / data-link ports before saving."
                )
                save_as_error_label.set_visibility(True)
                return

            hardware = resolve_save_hardware(selected_profile, _current_identity())
            forked_from = selected_profile.name if selected_profile else None

            try:
                profile = build_saved_profile(name, form_request, hardware, forked_from)
                created = profile_store.create_profile(profile)
            except (ValidationError, ProfileStoreError) as exc:
                save_as_error_label.text = str(exc)
                save_as_error_label.set_visibility(True)
                return

            selected_profile = created
            save_as_dialog.close()
            ui.notify(f"Saved profile '{created.name}'", type="positive")
            _render_picker()
            _on_form_changed()

        def _open_rename_dialog(profile: Profile) -> None:
            nonlocal rename_target
            rename_target = profile.name
            rename_name_input.value = display_label(profile)
            rename_error_label.set_visibility(False)
            rename_dialog.open()

        def _confirm_rename() -> None:
            nonlocal selected_profile
            if rename_target is None:
                return
            new_display_name = (rename_name_input.value or "").strip()
            try:
                renamed = profile_store.rename_profile(rename_target, new_display_name)
            except ProfileStoreError as exc:
                rename_error_label.text = str(exc)
                rename_error_label.set_visibility(True)
                return

            if selected_profile is not None and selected_profile.name == rename_target:
                selected_profile = renamed
            rename_dialog.close()
            ui.notify(f"Renamed to '{display_label(renamed)}'", type="positive")
            _render_picker()
            _on_form_changed()

        def _open_delete_dialog(profile: Profile) -> None:
            nonlocal delete_target, delete_target_label
            delete_target = profile.name
            delete_target_label = display_label(profile)
            delete_confirm_label.text = (
                f"Delete custom profile '{delete_target_label}'?"
            )
            delete_dialog.open()

        def _confirm_delete() -> None:
            nonlocal selected_profile
            if delete_target is None:
                return
            try:
                profile_store.delete_profile(delete_target)
            except ProfileStoreError as exc:
                ui.notify(str(exc), type="negative")
                delete_dialog.close()
                return

            if selected_profile is not None and selected_profile.name == delete_target:
                selected_profile = None
            delete_dialog.close()
            ui.notify(f"Deleted '{delete_target_label}'", type="positive")
            _render_picker()
            _on_form_changed()

        def _export_profile(name: str) -> None:
            try:
                profile = profile_store.export_profile(name)
            except ProfileStoreError as exc:
                ui.notify(str(exc), type="negative")
                return
            data = json.dumps(
                profile.model_dump(mode="json", exclude_none=True), indent=2
            )
            ui.download(data.encode("utf-8"), filename=f"{name}.json")

        def _update_ui_state() -> None:
            """Update UI visibility and button states based on device state."""
            state = svc.state
            connected = state == DeviceConnectionState.CONNECTED
            caps = svc.capabilities

            # Status indicator
            status_row.clear()
            with status_row:
                if state == DeviceConnectionState.CONNECTED:
                    ui.icon("check_circle").classes("text-positive text-h6")
                    ui.label("Connected").classes("text-positive")
                elif state == DeviceConnectionState.CONNECTING:
                    ui.spinner(size="sm")
                    ui.label("Connecting...").classes("text-warning")
                elif state == DeviceConnectionState.ERROR:
                    ui.icon("error").classes("text-negative text-h6")
                    ui.label("Error").classes("text-negative")
                else:
                    ui.icon("link_off").classes("text-grey text-h6")
                    ui.label("Disconnected").classes("text-grey")

            # Buttons
            connecting = state == DeviceConnectionState.CONNECTING
            connect_btn.set_visibility(not connected and not connecting)
            disconnect_btn.set_visibility(connected)
            cancel_btn.set_visibility(connecting)

            # Device info card
            info_card.set_visibility(connected)
            info_card.clear()
            if connected and svc.device_info:
                info = svc.device_info
                with info_card:
                    ui.label("Device Info").classes("text-subtitle1 text-white")
                    with ui.row().classes("w-full gap-4 q-mt-xs sp-metric-row"):
                        _info_item("Model", info.model)
                        _info_item("Firmware", info.firmware_version)
                        _info_item("Protocol", info.protocol_version)
                        _info_item("Hardware", info.hardware_version)

                    if caps:
                        with ui.row().classes("gap-1 q-mt-sm flex-wrap"):
                            for c in sorted(caps):
                                ui.badge(c.value).props("color=primary outline")

            # Section visibility
            profile_card.set_visibility(connected)
            config_card.set_visibility(connected)
            flash_card.set_visibility(
                connected and DeviceCapability.SAVE_TO_FLASH in caps
            )
            fixed_position_card.set_visibility(connected)
            reload_device_btn.set_visibility(connected)

            if connected:
                _render_picker()

            # Error display
            status = svc.get_status()
            if status.last_error:
                error_label.text = f"Error: {status.last_error}"
                error_label.set_visibility(True)
            else:
                error_label.set_visibility(False)

        # Editable form state (issue #97) — a ``form``/``live`` assertion
        # pair plus the data-link port selection, seeded from the live
        # receiver on every load/reload/reconnect. ``form`` holds operator
        # intent (mutated by matrix clicks, profile picks, and the
        # data-link checkboxes until the next Apply or reseed); ``live``
        # holds receiver truth. On seed they are equal by construction.
        form: ReceiverAssertion = placeholder_assertion()
        live: ReceiverAssertion = form.model_copy(deep=True)
        form_data_link_ports: list[PortId] = []

        # The profile a pick selected (None = no pick, the default/
        # transient state).
        selected_profile: Profile | None = None
        rename_target: str | None = None
        delete_target: str | None = None
        delete_target_label: str = ""

        # Provenance for the three-state badge and inline markers (issue
        # #101): paths that appeared in the *last* Apply's read-back diff
        # and haven't been edited since. Every form-mutating handler
        # below drops its own path(s) out of this set the moment the
        # operator edits that field — see ``_clear_failed``. Reset
        # wholesale by a fresh Apply, a profile pick, or a live reseed.
        failed_paths: set[str] = set()

        # Session-only, append-only step log (issue #101) — every Apply
        # this connection makes extends it; ``_disconnect``/reconnect
        # clear it (see ``_clear_session_state``).
        step_log: list[ApplyStepResult] = []

        def _partitioned_diff() -> tuple[list[ApplyDiffEntry], list[ApplyDiffEntry]]:
            """(failed, pending) leaves — the whole-form ``form`` vs.
            ``live`` comparison (issue #98's ``diff_receiver_assertions``
            call, same one Apply's own read-back verify uses), split by
            provenance (issue #101). Never mentions ``data_link_port``
            (it isn't part of either operand), so a data-link-port-only
            change never shows as out of sync."""
            diff = diff_receiver_assertions(form, live)
            return partition_diff_by_provenance(diff, failed_paths)

        def _failed_by_path() -> dict[str, ApplyDiffEntry]:
            """The currently-failed leaves, keyed by path — what the
            inline markers highlight."""
            failed, _pending = _partitioned_diff()
            return {d.path: d for d in failed}

        def _mark_mismatch(
            element: ui.element, path: str, failed_by_path: dict[str, ApplyDiffEntry]
        ) -> None:
            """Ring *element* amber and attach the mismatch detail as a
            tooltip when *path* is currently failed — the persistent
            mismatch truth an inline marker gives instead of prose the
            operator has to hunt a widget down to match (issue #101)."""
            entry = failed_by_path.get(path)
            if entry is None:
                return
            element.classes("field-failed")
            element.style(
                "outline: 2px solid #F2C037; outline-offset: 2px; border-radius: 4px"
            )
            element.tooltip(format_leaf_diff(entry))

        def _clear_failed(*paths: str) -> None:
            """Editing a field after a failed verification moves it from
            failed to pending (issue #101) — every mutation handler
            calls this with its own path(s) before re-rendering."""
            failed_paths.difference_update(paths)

        def _current_form_request() -> ReceiverApplyRequest | None:
            """The form as a ``ReceiverApplyRequest``, or ``None`` if it
            doesn't currently validate (e.g. no data-link port selected)."""
            try:
                return build_apply_request(form, form_data_link_ports)
            except ValidationError:
                return None

        def _toggle_port_protocol(
            port: PortId,
            direction: Literal["in", "out"],
            protocol: UbxProtocol,
            checked: bool,
        ) -> None:
            protocols = form.ports.setdefault(port, PortProtocolSet())
            target = protocols.in_ if direction == "in" else protocols.out
            if checked and protocol not in target:
                target.append(protocol)
            elif not checked and protocol in target:
                target.remove(protocol)
            _clear_failed(port_protocol_diff_path(port, direction, protocol))
            _render_ports_view()
            _on_form_changed()

        def _render_ports_view() -> None:
            ports_view.clear()
            failed_by_path = _failed_by_path()
            with ports_view:
                display = resolve_ports_display(form.ports)
                for port_id in (PortId.UART1, PortId.UART2, PortId.USB):
                    in_names, out_names = display[port_id]
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.label(port_id.value).classes("text-white").style(
                            "width: 60px; flex-shrink: 0"
                        )
                        ui.label("IN").classes("text-caption text-grey-5")
                        for protocol in UbxProtocol:
                            checkbox = ui.checkbox(
                                protocol.value,
                                value=protocol.value in in_names,
                                on_change=lambda e, p=port_id, pr=protocol: (
                                    _toggle_port_protocol(p, "in", pr, bool(e.value))
                                ),
                            ).classes(
                                f"port-protocol-{port_id.value}-in-{protocol.value}"
                            )
                            _mark_mismatch(
                                checkbox,
                                port_protocol_diff_path(port_id, "in", protocol),
                                failed_by_path,
                            )
                        ui.label("OUT").classes("text-caption text-grey-5 q-ml-md")
                        for protocol in UbxProtocol:
                            checkbox = ui.checkbox(
                                protocol.value,
                                value=protocol.value in out_names,
                                on_change=lambda e, p=port_id, pr=protocol: (
                                    _toggle_port_protocol(p, "out", pr, bool(e.value))
                                ),
                            ).classes(
                                f"port-protocol-{port_id.value}-out-{protocol.value}"
                            )
                            _mark_mismatch(
                                checkbox,
                                port_protocol_diff_path(port_id, "out", protocol),
                                failed_by_path,
                            )

        def _toggle_gnss(constellation: GnssConstellation, checked: bool) -> None:
            _clear_failed(constellation_diff_path(constellation))
            if checked and constellation not in form.constellations:
                form.constellations.append(constellation)
            elif not checked and constellation in form.constellations:
                form.constellations.remove(constellation)
                if constellation == GnssConstellation.BEIDOU:
                    # Keep the form's own value consistent with the
                    # now-greyed-out B2 control the moment BeiDou goes
                    # off, rather than leaving a stale "on" behind it
                    # (build_apply_request coerces this too, for the
                    # case where the two were already inconsistent on
                    # seed — see its docstring). Counts as an edit to
                    # bds_b2_enabled too (issue #101).
                    form.bds_b2_enabled = False
                    _clear_failed("bds_b2_enabled")
            _render_gnss_view()
            _render_hw_extras_view()
            _on_form_changed()

        def _render_gnss_view() -> None:
            gnss_view.clear()
            failed_by_path = _failed_by_path()
            with gnss_view:
                enabled_map = resolve_gnss_display(form.constellations)
                for c_val, c_name in _GNSS_DISPLAY:
                    constellation = GnssConstellation(c_val)
                    checkbox = ui.checkbox(
                        c_name,
                        value=enabled_map.get(c_val, False),
                        on_change=lambda e, c=constellation: _toggle_gnss(
                            c, bool(e.value)
                        ),
                    ).classes(f"gnss-checkbox-{c_val}")
                    _mark_mismatch(
                        checkbox, constellation_diff_path(constellation), failed_by_path
                    )

        def _set_meas_period_ms(period_ms: float | None) -> None:
            if period_ms is None:
                return
            form.meas_period_ms = int(period_ms)
            _clear_failed("meas_period_ms")
            _render_hw_extras_view()
            _on_form_changed()

        def _set_baud(uart: str, value: int | None) -> None:
            if value is None:
                return
            if uart == "uart1":
                form.baud.uart1 = int(value)
            else:
                form.baud.uart2 = int(value)
            _clear_failed(f"baud.{uart}")
            _render_hw_extras_view()
            _on_form_changed()

        def _set_dyn_model(value: str | None) -> None:
            if value is None:
                return
            form.dyn_model = DynModel(value)
            _clear_failed("dyn_model")
            _render_hw_extras_view()
            _on_form_changed()

        def _set_tmode_mode(value: str | None) -> None:
            if value is None:
                return
            form.tmode_mode = TmodeMode(value)
            _clear_failed("tmode_mode")
            _render_hw_extras_view()
            _on_form_changed()

        def _set_elevation_mask(value: float | None) -> None:
            if value is None:
                return
            form.elevation_mask_deg = int(value)
            _clear_failed("elevation_mask_deg")
            _render_hw_extras_view()
            _on_form_changed()

        def _toggle_bds_b2(checked: bool) -> None:
            form.bds_b2_enabled = checked
            _clear_failed("bds_b2_enabled")
            _render_hw_extras_view()
            _on_form_changed()

        def _toggle_spi(checked: bool) -> None:
            form.spi_enabled = checked
            _clear_failed("spi_enabled")
            _render_hw_extras_view()
            _on_form_changed()

        def _render_hw_extras_view() -> None:
            hw_extras_view.clear()
            tmode_options = {m.value: m.value for m in SELECTABLE_TMODE_MODES}
            failed_by_path = _failed_by_path()
            with hw_extras_view:
                with ui.column().classes("hw-field-meas-rate gap-0"):
                    ui.label("Measurement Rate").classes("text-caption text-grey-5")
                    meas_input = (
                        ui.number(
                            value=form.meas_period_ms,
                            min=100,
                            max=60000,
                            step=100,
                            on_change=lambda e: _set_meas_period_ms(e.value),
                        )
                        .classes("hw-field-meas-rate-input")
                        .props("dense suffix=ms")
                        .style("width: 140px")
                    )
                    _mark_mismatch(meas_input, "meas_period_ms", failed_by_path)
                    hz = 1000 / form.meas_period_ms
                    ui.label(f"= {hz:g} Hz").classes("text-caption text-grey-5")

                with ui.column().classes("hw-field-baud gap-0"):
                    ui.label("Baud").classes("text-caption text-grey-5")
                    with ui.row().classes("gap-2"):
                        uart1_select = (
                            ui.select(
                                options={r: str(r) for r in BAUD_RATES},
                                label="UART1",
                                value=form.baud.uart1,
                                on_change=lambda e: _set_baud("uart1", e.value),
                            )
                            .classes("hw-field-baud-uart1")
                            .style("width: 110px")
                        )
                        _mark_mismatch(uart1_select, "baud.uart1", failed_by_path)
                        uart2_select = (
                            ui.select(
                                options={r: str(r) for r in BAUD_RATES},
                                label="UART2",
                                value=form.baud.uart2,
                                on_change=lambda e: _set_baud("uart2", e.value),
                            )
                            .classes("hw-field-baud-uart2")
                            .style("width: 110px")
                        )
                        _mark_mismatch(uart2_select, "baud.uart2", failed_by_path)

                with ui.column().classes("hw-field-dyn-model gap-0"):
                    ui.label("Dynamics Model").classes("text-caption text-grey-5")
                    dyn_model_select = (
                        ui.select(
                            options={m.value: m.value for m in DynModel},
                            value=form.dyn_model.value,
                            on_change=lambda e: _set_dyn_model(e.value),
                        )
                        .classes("hw-field-dyn-model-select")
                        .style("width: 160px")
                    )
                    _mark_mismatch(dyn_model_select, "dyn_model", failed_by_path)

                with ui.column().classes("hw-field-tmode gap-0"):
                    ui.label("Time Mode").classes("text-caption text-grey-5")
                    if tmode_mode_locked(form.tmode_mode):
                        ui.label(f"{form.tmode_mode.value} (running)").classes(
                            "hw-field-tmode-note text-white"
                        )
                        ui.button("Go to Survey page", icon="open_in_new").classes(
                            "hw-field-tmode-survey-link"
                        ).props("outline color=primary dense").on(
                            "click", lambda: ui.navigate.to("/survey")
                        )
                        tmode_select = (
                            ui.select(
                                options=tmode_options,
                                label="Change to...",
                                on_change=lambda e: _set_tmode_mode(e.value),
                            )
                            .classes("hw-field-tmode-select")
                            .style("width: 140px")
                        )
                    else:
                        tmode_select = (
                            ui.select(
                                options=tmode_options,
                                value=form.tmode_mode.value,
                                on_change=lambda e: _set_tmode_mode(e.value),
                            )
                            .classes("hw-field-tmode-select")
                            .style("width: 140px")
                        )
                    _mark_mismatch(tmode_select, "tmode_mode", failed_by_path)

                with ui.column().classes("hw-field-elevation-mask gap-0"):
                    ui.label("Elevation Mask").classes("text-caption text-grey-5")
                    elevation_input = (
                        ui.number(
                            value=form.elevation_mask_deg,
                            min=0,
                            max=90,
                            step=1,
                            on_change=lambda e: _set_elevation_mask(e.value),
                        )
                        .classes("hw-field-elevation-mask-input")
                        .props("dense suffix=°")
                        .style("width: 100px")
                    )
                    _mark_mismatch(
                        elevation_input, "elevation_mask_deg", failed_by_path
                    )

                with ui.column().classes("hw-field-bds-b2 gap-0"):
                    ui.label("BeiDou B2").classes("text-caption text-grey-5")
                    bds_b2_checkbox = (
                        ui.checkbox(
                            value=form.bds_b2_enabled,
                            on_change=lambda e: _toggle_bds_b2(bool(e.value)),
                        )
                        .classes("hw-field-bds-b2-checkbox")
                        .set_enabled(not bds_b2_control_disabled(form.constellations))
                    )
                    _mark_mismatch(bds_b2_checkbox, "bds_b2_enabled", failed_by_path)

                with ui.column().classes("hw-field-spi gap-0"):
                    ui.label("SPI").classes("text-caption text-grey-5")
                    spi_checkbox = ui.checkbox(
                        value=form.spi_enabled,
                        on_change=lambda e: _toggle_spi(bool(e.value)),
                    ).classes("hw-field-spi-checkbox")
                    _mark_mismatch(spi_checkbox, "spi_enabled", failed_by_path)

        def _toggle_matrix_cell(msg_id: RtcmRowId, port: PortId) -> None:
            form.rtcm_stream.matrix[msg_id][port] = not form.rtcm_stream.matrix[msg_id][
                port
            ]
            _clear_failed(rtcm_diff_path(msg_id, port))
            _render_matrix()
            _on_form_changed()

        def _render_matrix() -> None:
            matrix_view.clear()
            failed_by_path = _failed_by_path()
            with matrix_view:
                # Header row
                with (
                    ui.row()
                    .classes("items-center w-full gap-0")
                    .style("border-bottom: 1px solid #333; padding-bottom: 4px")
                ):
                    ui.label("Message").classes("text-caption text-grey-5").style(
                        "width: 220px; flex-shrink: 0"
                    )
                    for port in MATRIX_PORTS:
                        header_classes = (
                            f"rtcm-col-header rtcm-col-header-{port.value} "
                            "text-caption text-center q-px-sm"
                            + (
                                " text-primary"
                                if port in DATA_LINK_PORTS
                                else " text-grey-5"
                            )
                        )
                        ui.label(port.value).classes(header_classes).style(
                            "width: 70px; flex-shrink: 0"
                        )

                for _group_name, messages in RTCM_MESSAGE_GROUPS:
                    for msg_id, msg_desc in messages:
                        slug = row_slug(msg_id)
                        with (
                            ui.row()
                            .classes(
                                f"rtcm-row rtcm-row-{slug} items-center w-full gap-0"
                            )
                            .style("border-bottom: 1px solid #222; padding: 2px 0")
                        ):
                            with (
                                ui.row()
                                .classes("items-center gap-1")
                                .style("width: 220px; flex-shrink: 0")
                            ):
                                ui.label(f"{msg_id.value} {msg_desc}").classes(
                                    "text-grey-3 text-caption"
                                )
                                if msg_id == REQUIRED_RTCM_ROW:
                                    ui.badge("Required").props("outline color=warning")

                            for port in MATRIX_PORTS:
                                on = form.rtcm_stream.matrix[msg_id][port]
                                cell_classes = (
                                    f"rtcm-cell rtcm-cell-{slug}-{port.value} "
                                    "text-center cursor-pointer"
                                    + (" text-positive" if on else " text-grey-7")
                                )
                                cell = (
                                    ui.label("✓" if on else "-")
                                    .classes(cell_classes)
                                    .style("width: 70px; flex-shrink: 0")
                                    .on(
                                        "click",
                                        lambda _, m=msg_id, p=port: _toggle_matrix_cell(
                                            m, p
                                        ),
                                    )
                                )
                                _mark_mismatch(
                                    cell,
                                    rtcm_diff_path(msg_id, port),
                                    failed_by_path,
                                )

        def _render_data_link_picker() -> None:
            inferred = infer_data_link_ports(form.rtcm_stream.matrix)
            data_link_hint.text = (
                f"Inferred from current RTCM state: "
                f"{', '.join(p.value for p in inferred)}"
                if inferred
                else "Nothing inferred yet — pick below."
            )

            data_link_picker.clear()
            with data_link_picker:
                for port in (PortId.UART1, PortId.UART2):
                    ui.checkbox(
                        port.value,
                        value=port in form_data_link_ports,
                        on_change=lambda e, p=port: _toggle_data_link_port(
                            p, bool(e.value)
                        ),
                    ).classes(f"data-link-checkbox-{port.value}")

        def _toggle_data_link_port(port: PortId, checked: bool) -> None:
            if checked and port not in form_data_link_ports:
                form_data_link_ports.append(port)
            elif not checked and port in form_data_link_ports:
                form_data_link_ports.remove(port)
            _on_form_changed()

        def _render_sync_indicator() -> None:
            """The one-badge, three-state result surface (issue #101):
            ``In sync`` / ``n unapplied change(s)`` (neutral — the resting
            state of an editable form) / ``n field(s) failed verification``
            (warning colour, priority over pending)."""
            failed, pending = _partitioned_diff()
            state = result_badge_state(failed, pending)
            sync_badge.text = badge_label(state)
            sync_badge.props(f"color={badge_color(state)}")

        def _render_apply_gate() -> None:
            reason = apply_blocked_reason(form_data_link_ports)
            apply_btn.set_enabled(reason is None)
            data_link_blocked_label.text = reason or ""
            data_link_blocked_label.set_visibility(reason is not None)

        def _render_modified_indicator() -> None:
            """The second, independent indicator — form vs. *selected
            profile*, distinct from "out of sync" (form vs. live receiver).
            Hidden when no profile is selected: nothing to have diverged
            from.

            Renders in a neutral colour either way (issue #105) — this
            answers "should I save this?", not "something's wrong", so
            neither state ever borrows the warning/positive colours that
            train an operator to read amber as trouble. "Modified from
            X" and "Matches X" are told apart by an outline vs. filled
            treatment of the same neutral grey, not by hue.
            """
            form_request = _current_form_request()
            if selected_profile is None or form_request is None:
                modified_badge.set_visibility(False)
                return
            modified_badge.set_visibility(True)
            if is_modified_from_profile(form_request, selected_profile, live):
                modified_badge.text = f"Modified from {display_label(selected_profile)}"
                modified_badge.props(remove="outline")
                modified_badge.props("color=grey-7")
            else:
                modified_badge.text = f"Matches {display_label(selected_profile)}"
                modified_badge.props("color=grey-7 outline")

        def _render_save_as_gate() -> None:
            save_as_btn.set_enabled(
                save_as_enabled(_current_form_request(), selected_profile, live)
            )

        def _on_form_changed() -> None:
            """Recompute every reactive bit of the form after an edit.

            Includes the data-link picker so its "inferred from current
            RTCM state" hint stays current after a matrix toggle, not
            just after a reseed.
            """
            _render_data_link_picker()
            _render_sync_indicator()
            _render_apply_gate()
            _render_modified_indicator()
            _render_save_as_gate()

        def _set_apply_result(text: str, *, ok: bool) -> None:
            apply_result_label.text = text
            apply_result_label.classes(
                remove="text-negative" if ok else "text-positive",
                add="text-positive" if ok else "text-negative",
            )
            apply_result_label.set_visibility(True)

        def _clear_apply_result() -> None:
            apply_result_label.set_visibility(False)

        def _clear_warning_strip() -> None:
            warning_strip_card.set_visibility(False)
            warning_strip_view.clear()

        def _render_warning_strip(lines: list[str]) -> None:
            """Replace the strip whole with *lines* — one ``⚠`` label per
            warning, never a string-joined blob (issue #101). No remedy
            button — the remedy is the Apply button already in the
            action row, named explicitly so the operator doesn't have
            to guess it."""
            warning_strip_view.clear()
            if not lines:
                warning_strip_card.set_visibility(False)
                return
            warning_strip_card.set_visibility(True)
            with warning_strip_view:
                for line in lines:
                    ui.label(f"⚠ {line}").classes(
                        "warning-strip-line text-warning text-caption"
                    )
                ui.label("Press Apply to retry.").classes(
                    "warning-strip-remedy text-warning text-caption"
                )

        def _render_step_log() -> None:
            """Re-render the whole append-only log from ``step_log``."""
            step_log_view.clear()
            with step_log_view:
                for step in step_log:
                    ui.label(format_step_log_entry(step)).classes(
                        f"step-log-entry step-log-entry-{step.status} "
                        "text-caption text-grey-4"
                    )

        def _clear_session_state() -> None:
            """Session-only surfaces cleared on disconnect (issue #101) —
            a surviving log or strip would narrate a possibly-detached
            receiver."""
            failed_paths.clear()
            step_log.clear()
            _render_step_log()
            _clear_warning_strip()

        async def _apply() -> None:
            """Push the whole current form to the receiver (issue #98).

            Apply now sends every receiver field, not just the RTCM
            matrix — ``build_apply_request`` wraps the full ``form``
            plus the data-link port selection into the envelope
            ``svc.apply_receiver_config`` takes. ``live`` is replaced
            wholesale from ``result.read_back`` — the fresh full
            read-back the service always returns, whichever ``status``
            — rather than patched field-by-field, so every display
            (matrix, ports, GNSS, hardware section) stays honest after
            both a clean apply and a partial-mismatch one.

            Issue #101: the warning strip is replaced (or cleared) on
            every Apply attempt, including a pre-write refusal — ``live``
            and ``failed_paths`` are untouched by a refusal since nothing
            was written. A completed Apply reseeds ``failed_paths`` from
            ``result.diff`` (empty on a clean apply) and extends the
            session's step log with ``result.steps``.
            """
            nonlocal live
            _clear_warning_strip()

            try:
                request = build_apply_request(form, form_data_link_ports)
            except ValidationError as exc:
                _set_apply_result(
                    f"Apply refused: {exc.errors()[0]['msg']} — nothing was written.",
                    ok=False,
                )
                return

            try:
                result = await svc.apply_receiver_config(request)
            except ApplyConfigRefusedError as exc:
                _set_apply_result(
                    f"Apply refused ({exc.rule}): {exc} — nothing was written.",
                    ok=False,
                )
                return
            except ApplyConfigLinkLostError as exc:
                _set_apply_result(str(exc), ok=False)
                return
            except Exception as exc:
                ui.notify(f"Apply failed: {exc}", type="negative")
                logger.exception("apply-config failed")
                return

            live = result.read_back
            failed_paths.clear()
            failed_paths.update(d.path for d in result.diff)
            step_log.extend(result.steps)
            _render_step_log()

            warning_lines = apply_warning_lines(result.warnings, result.step_warnings)
            _render_warning_strip(warning_lines)

            headline = apply_headline(result.status, len(warning_lines))
            _set_apply_result(headline, ok=(result.status == "ok"))
            ui.notify(
                headline,
                type="positive" if result.status == "ok" else "warning",
            )

            _render_matrix()
            _render_ports_view()
            _render_gnss_view()
            _render_hw_extras_view()
            _on_form_changed()

        def _render_advisory(rtcm: RtcmPortConfig) -> None:
            rows = i2c_spi_advisory_rows(rtcm)
            if rows:
                names = ", ".join(r.value for r in rows)
                advisory_label.text = (
                    f"RTCM enabled on I2C/SPI for {names} — this profile "
                    "doesn't manage those ports."
                )
                advisory_label.set_visibility(True)
            else:
                advisory_label.set_visibility(False)

        def _render_fixed_position_step(
            label: ui.label, step_index: int, current_step: int, text: str
        ) -> None:
            label.text = text
            if step_index == current_step:
                modifier = "is-current"
            elif step_index < current_step:
                modifier = "is-done"
            else:
                modifier = "is-pending"
            label.classes(
                remove="is-current is-done is-pending",
                add=modifier,
            )

        async def _render_fixed_position() -> None:
            """Refresh the Fixed Position card's three-step state (issue #96).

            Polls the live receiver directly — ``get_base_config`` /
            ``get_survey_in_status`` — never the profile-seeded form
            state above. Called alongside the rest of the live-seeding
            refresh flow in ``_load_receiver_config_form``, so it stays
            current on connect, reload/reconnect, and page (re)load
            while already connected.
            """
            try:
                base_config = await svc.get_base_config()
                survey = await svc.get_survey_in_status()
            except Exception:
                logger.debug("Failed to read fixed-position step state")
                return

            state = fixed_position_step_state(base_config, survey)
            _render_fixed_position_step(
                fp_step1_label, 1, state.current_step, "① Apply profile"
            )
            _render_fixed_position_step(
                fp_step2_label,
                2,
                state.current_step,
                f"② Run survey-in {state.survey_state_text}",
            )
            _render_fixed_position_step(
                fp_step3_label,
                3,
                state.current_step,
                f"③ Fixed position set {state.fixed_pos_text}",
            )

        async def _load_receiver_config_form() -> None:
            """Seed the form from the live receiver.

            The RTCM matrix and data-link ports are freshly inferred here
            every time — on connect, reload, and reconnect — so the form
            starts in sync by construction (issue #65), matching the rest
            of this page's existing live-seeding behaviour. This is also
            the reset point for the transient profile-pick buffer (issue
            #66): unsaved form state, including which profile (if any) is
            selected, is discarded on nav / reload / reconnect / restart,
            per spec — the form always re-seeds from the live receiver,
            never from the last-loaded profile.
            """
            nonlocal form, live, form_data_link_ports, selected_profile
            read = await svc.get_receiver_assertion()
            live = read.assertion
            form = live.model_copy(deep=True)
            form_data_link_ports = infer_data_link_ports(live.rtcm_stream.matrix)
            selected_profile = None
            # A full reseed makes form == live by construction, so no
            # path can still differ — reset provenance defensively too.
            failed_paths.clear()

            # The I2C/SPI advisory (rows enabled on a port the matrix
            # doesn't manage) needs the raw multi-port read-back, which
            # ``ReceiverAssertion`` doesn't carry — ``read.rtcm`` reuses
            # the one poll ``get_receiver_assertion`` already made rather
            # than re-polling RTCM a second time.
            rtcm = read.rtcm

            _render_ports_view()
            _render_gnss_view()
            _render_hw_extras_view()
            _render_matrix()
            _render_advisory(rtcm)
            _clear_apply_result()
            _on_form_changed()
            _render_picker()
            await _render_fixed_position()

        async def _connect() -> None:
            """Connect to the selected device."""
            port = port_select.value
            baud = int(baud_select.value or DEFAULT_BAUD)
            vendor = str(driver_select.value or "ublox")

            if not port:
                ui.notify("Select a serial port", type="warning")
                return

            try:
                if svc.is_connected:
                    await svc.disconnect()
                    _clear_session_state()

                driver = create_driver(vendor)
                svc.set_driver(driver)

                svc.set_connecting()
                _update_ui_state()

                await svc.connect(str(port), baud)
                ui.notify("Connected!", type="positive")

                # Save port/baud for next time
                _save_device_settings()

            except Exception as exc:
                ui.notify(f"Connection failed: {exc}", type="negative")
                logger.exception("Device connect failed")

            _update_ui_state()

            # Auto-load the receiver-config form after connect
            if svc.is_connected:
                try:
                    await _load_receiver_config_form()
                except Exception:
                    logger.warning("Failed to load receiver config on connect")

        def _cancel_connect() -> None:
            """Cancel an in-progress connect attempt."""
            svc.cancel_connect()
            ui.notify("Connection cancelled", type="warning")
            _update_ui_state()

        async def _disconnect() -> None:
            """Disconnect from device."""
            await svc.disconnect()
            _clear_session_state()
            ui.notify("Disconnected", type="info")
            _update_ui_state()

        async def _save_flash() -> None:
            """Save configuration to device flash."""
            try:
                await svc.save_to_flash()
                ui.notify("Saved to flash!", type="positive")
            except Exception as exc:
                ui.notify(f"Save failed: {exc}", type="negative")

        async def _reload_device_config() -> None:
            """Re-read the profile picker and receiver-config form."""
            if not svc.is_connected:
                ui.notify("Not connected", type="warning")
                return
            ui.notify("Reloading device config...", type="info")
            _update_ui_state()
            try:
                await _load_receiver_config_form()
            except Exception:
                logger.debug("Reload receiver config failed")

        # ---- Wire up event handlers ----
        connect_btn.on_click(_connect)
        disconnect_btn.on_click(_disconnect)
        cancel_btn.on_click(lambda: _cancel_connect())
        refresh_btn.on_click(lambda: _refresh_ports())
        reload_device_btn.on_click(_reload_device_config)
        apply_btn.on_click(_apply)
        save_as_btn.on_click(_open_save_as_dialog)
        save_as_confirm_btn.on_click(_confirm_save_as)
        rename_confirm_btn.on_click(_confirm_rename)
        delete_confirm_btn.on_click(_confirm_delete)
        save_flash_btn.on_click(_save_flash)

        # ---- Auto-load if already connected (navigated from another page) ----
        async def _on_page_load() -> None:
            """If device is already connected, auto-load the receiver-config form."""
            if svc.is_connected:
                _update_ui_state()
                try:
                    await _load_receiver_config_form()
                except Exception:
                    logger.debug("Auto-load receiver config failed on page load")

        # ---- Initial load ----
        _refresh_ports()
        _load_saved_device_settings()
        _update_ui_state()

        # Deferred auto-load for already-connected scenario
        ui.timer(interval=0.1, callback=_on_page_load, once=True)


def _info_item(label: str, value: str) -> None:
    """Render a small info label/value pair."""
    with ui.column().classes("gap-0"):
        ui.label(label).classes("text-caption text-grey-5")
        ui.label(value or "—").classes("text-white")
