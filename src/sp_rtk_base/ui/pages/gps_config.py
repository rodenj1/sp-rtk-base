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

Issue #66 (this revision) makes picking a profile actually write into
the form — the whole ``ReceiverConfig`` shape (ports, GNSS,
baud/measurement-rate, role fields, optimisations, plus the matrix and
data-link ports), not just the matrix — and adds the second,
independent "modified from X" indicator (form vs. *selected profile*,
gating Save-as) alongside the existing "out of sync" one (form vs.
*live receiver*, gating Apply). Save-as, rename, delete and export
round out the custom-profile lifecycle. The ports/GNSS/baud/role
section remains a read-only *display* of form state rather than a
click-to-edit grid — the driver has no read-back for most of those
fields (baud excepted), so there's nothing yet to reconcile an edit
against; turning that into an editable grid is future work, same as
#65 deferred it.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# pyright: reportOptionalMemberAccess=false
# pyright: reportOptionalIterable=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from nicegui import ui
from pydantic import ValidationError

from sp_rtk_base.models.config_models import DeviceProfile
from sp_rtk_base.models.device_models import (
    ALL_RTCM_MESSAGE_IDS,
    RTCM_MESSAGE_GROUPS,
    ApplyConfigCellDiff,
    CurrentBaseConfig,
    DeviceCapability,
    DeviceConnectionState,
    DynModel,
    GnssConfig,
    GnssConstellation,
    PortId,
    PortProtocolConfig,
    RtcmOutputPort,
    RtcmPortConfig,
    RtcmRowId,
    SurveyInProgress,
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
    BaudConfig,
    PortProtocolSet,
    Profile,
    ReceiverConfig,
    RtcmStreamConfig,
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

#: Ports the boolean RTCM matrix covers — matches
#: ``RtcmStreamConfig.matrix``'s column set (``PortId``), not the full
#: 5-port live read-back.
MATRIX_PORTS: list[PortId] = [PortId.UART1, PortId.UART2, PortId.USB]

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


@dataclass
class FormExtras:
    """The hardware-section fields beyond the matrix and data-link ports.

    Not editable via any widget on this page yet (see the module
    docstring) — this only carries values a *profile pick* pre-fills,
    so Apply can push them and Save-as / "modified from X" can compare
    against them. ``None`` mirrors ``ReceiverConfig``'s own "leave
    untouched" semantics for every field except ``meas_period_ms``,
    which ``ReceiverConfig`` itself always defaults to ``1000``.
    """

    ports: dict[PortId, PortProtocolSet] | None = None
    constellations: list[GnssConstellation] | None = None
    baud: BaudConfig | None = None
    meas_period_ms: int = 1000
    dyn_model: DynModel | None = None
    tmode_mode: TmodeMode | None = None
    elevation_mask_deg: int | None = None
    bds_b2_enabled: bool | None = None
    spi_enabled: bool | None = None


def build_apply_config(
    matrix: dict[RtcmRowId, dict[PortId, bool]],
    data_link_port: list[PortId],
    extras: FormExtras | None = None,
) -> ReceiverConfig:
    """Build a ``ReceiverConfig`` from the form state.

    *extras* defaults to an all-``None`` :class:`FormExtras` — nothing
    but the matrix and data-link ports set. ``_apply()`` always uses
    that default (Apply stays scoped to the matrix + data-link ports,
    per #65 — the other fields have no live read-back to verify a
    write against). Save-as and the "modified from X" comparison pass
    the real *extras* through, since those only compare in-form state
    and never touch the receiver.

    Raises:
        pydantic.ValidationError: If the resulting config fails a
            context-free rule (e.g. 1005 missing from every chosen
            data-link port) — a client-side pre-write refusal, nothing
            is sent to the receiver.
    """
    extras = extras or FormExtras()
    return ReceiverConfig(
        ports=extras.ports,
        constellations=extras.constellations,
        baud=extras.baud,
        meas_period_ms=extras.meas_period_ms,
        dyn_model=extras.dyn_model,
        tmode_mode=extras.tmode_mode,
        elevation_mask_deg=extras.elevation_mask_deg,
        bds_b2_enabled=extras.bds_b2_enabled,
        spi_enabled=extras.spi_enabled,
        data_link_port=data_link_port,
        rtcm_stream=RtcmStreamConfig(matrix=matrix),
    )


def profile_matrix_to_form_matrix(
    profile: Profile,
) -> dict[RtcmRowId, dict[PortId, bool]]:
    """Normalize a profile's sparse matrix to the full catalog x port grid.

    Mirrors :func:`rtcm_config_to_matrix` — the form's out-of-sync
    comparison relies on every matrix always covering every catalog
    row x :data:`MATRIX_PORTS` cell, whether it was seeded from a live
    read-back or, here, a profile pick.
    """
    matrix = profile.rtcm_stream.matrix
    return {
        row_id: {port: matrix.get(row_id, {}).get(port, False) for port in MATRIX_PORTS}
        for row_id in ALL_RTCM_MESSAGE_IDS
    }


def profile_to_form_extras(profile: Profile) -> FormExtras:
    """Everything a profile pick writes into the form besides matrix/data-link."""
    return FormExtras(
        ports=profile.ports,
        constellations=profile.constellations,
        baud=profile.baud,
        meas_period_ms=profile.meas_period_ms,
        dyn_model=profile.dyn_model,
        tmode_mode=profile.tmode_mode,
        elevation_mask_deg=profile.elevation_mask_deg,
        bds_b2_enabled=profile.bds_b2_enabled,
        spi_enabled=profile.spi_enabled,
    )


def receiver_config_from_profile(profile: Profile) -> ReceiverConfig:
    """The ``ReceiverConfig`` a profile carries, with identity stripped.

    Used to compare "the form" against "the selected profile" as the
    same type — a ``Profile`` is a ``ReceiverConfig`` plus identity
    fields, and those must not participate in the "modified from X"
    equality check.

    Deliberately built through :func:`build_apply_config` with the
    same :func:`profile_matrix_to_form_matrix`/:func:`profile_to_form_extras`
    normalization :func:`_select_profile` (in the page closure) uses to
    seed the form from this same profile — a stored profile's matrix is
    *sparse* (absent cell = off), while the form's is always *dense*
    (every catalog row x port present). Comparing a freshly-picked,
    untouched form against ``ReceiverConfig(**profile.model_dump())``
    would almost always report "modified" — the same on/off state,
    represented as two differently-shaped dicts that don't compare
    equal — unless both sides go through the identical normalization.
    """
    return build_apply_config(
        profile_matrix_to_form_matrix(profile),
        list(profile.data_link_port),
        profile_to_form_extras(profile),
    )


def is_modified_from_profile(
    form_config: ReceiverConfig, profile: Profile | None
) -> bool:
    """Whether *form_config* diverges from *profile* — the second, independent
    indicator (form vs. selected profile), distinct from "out of sync" (form
    vs. live receiver). ``False`` when no profile is selected — there is
    nothing to have diverged from."""
    if profile is None:
        return False
    return form_config != receiver_config_from_profile(profile)


def save_as_enabled(
    form_config: ReceiverConfig | None, profile: Profile | None
) -> bool:
    """Save-as is available whenever the form is valid, suppressed only when
    a *selected* profile still exactly equals the form."""
    if form_config is None:
        return False
    return profile is None or is_modified_from_profile(form_config, profile)


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
    form_config: ReceiverConfig,
    hardware: str,
    forked_from: str | None,
) -> Profile:
    """Construct the ``Profile`` document Save-as persists."""
    return Profile(
        **form_config.model_dump(),
        name=name,
        version=1,
        hardware=hardware,
        forked_from=forked_from,
    )


def resolve_ports_display(
    live: PortProtocolConfig,
    form_ports: dict[PortId, PortProtocolSet] | None,
) -> dict[PortId, tuple[list[str], list[str]]]:
    """Per-port (in, out) protocol name lists the "Port Protocols" display
    renders — the live read-back, unless a profile pick set *form_ports*, in
    which case the form is the source of truth (issue #66)."""
    if form_ports is not None:
        empty = PortProtocolSet()
        return {
            port: (
                [p.value for p in form_ports.get(port, empty).in_],
                [p.value for p in form_ports.get(port, empty).out],
            )
            for port in (PortId.UART1, PortId.UART2, PortId.USB)
        }
    return {
        port: (
            [p.value for p in live.enabled_in(port)],
            [p.value for p in live.enabled_out(port)],
        )
        for port in (PortId.UART1, PortId.UART2, PortId.USB)
    }


def resolve_gnss_display(
    live: GnssConfig,
    form_constellations: list[GnssConstellation] | None,
) -> dict[str, bool]:
    """Constellation -> enabled map the "GNSS Constellations" display
    renders — the live read-back, unless a profile pick set
    *form_constellations*, in which case only those are enabled."""
    if form_constellations is not None:
        wanted = set(form_constellations)
        return {c.value: c in wanted for c in GnssConstellation}
    return {sys_cfg.constellation.value: sys_cfg.enabled for sys_cfg in live.systems}


def _tri_state_text(value: bool | None) -> str:
    """``"on"``/``"off"``/``"unchanged"`` for an optional bool form field."""
    return "unchanged" if value is None else ("on" if value else "off")


def hw_extras_display(extras: FormExtras) -> list[tuple[str, str, str]]:
    """(css-class, label, value) triples the "Hardware Section" display
    renders — every :class:`FormExtras` field the matrix/ports/GNSS views
    don't already cover."""
    baud_parts: list[str] = []
    if extras.baud is not None:
        if extras.baud.uart1 is not None:
            baud_parts.append(f"UART1={extras.baud.uart1}")
        if extras.baud.uart2 is not None:
            baud_parts.append(f"UART2={extras.baud.uart2}")

    hz = 1000 / extras.meas_period_ms
    return [
        ("hw-field-meas-rate", "Measurement Rate", f"{hz:g} Hz"),
        ("hw-field-baud", "Baud", ", ".join(baud_parts) or "unchanged"),
        (
            "hw-field-dyn-model",
            "Dynamics Model",
            extras.dyn_model.value if extras.dyn_model else "unchanged",
        ),
        (
            "hw-field-tmode",
            "Time Mode",
            extras.tmode_mode.value if extras.tmode_mode else "unchanged",
        ),
        (
            "hw-field-elevation-mask",
            "Elevation Mask",
            f"{extras.elevation_mask_deg}°"
            if extras.elevation_mask_deg is not None
            else "unchanged",
        ),
        ("hw-field-bds-b2", "BeiDou B2", _tri_state_text(extras.bds_b2_enabled)),
        ("hw-field-spi", "SPI", _tri_state_text(extras.spi_enabled)),
    ]


def copy_matrix(
    matrix: dict[RtcmRowId, dict[PortId, bool]],
) -> dict[RtcmRowId, dict[PortId, bool]]:
    """Deep-copy a row x port matrix so the copy can diverge independently.

    A shallow ``dict(matrix)`` shares the per-row inner dicts with the
    original — mutating one cell would silently mutate both the form
    and the "last known live" snapshot, breaking the out-of-sync
    comparison.
    """
    return {row: dict(ports) for row, ports in matrix.items()}


def format_cell_diff(diff: ApplyConfigCellDiff) -> str:
    """Render one post-apply read-back mismatch as a human-readable line."""
    expected = "on" if diff.expected else "off"
    actual = "on" if diff.actual else "off"
    return (
        f"{diff.row_id.value} on {diff.port.value}: expected {expected}, got {actual}"
    )


def row_slug(row_id: RtcmRowId) -> str:
    """A CSS-class-safe token for *row_id* (``"4072.0"`` -> ``"4072_0"``) — a
    stable hook the e2e suite uses to target a specific matrix row/cell."""
    return row_id.value.replace(".", "_")


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
            picker_list = ui.column().classes("q-mt-sm gap-1 w-full")

        # ================================================================
        # Section C: Receiver configuration — read-only, seeded from the
        # live receiver (hidden until connected)
        # ================================================================
        config_card = ui.card().classes("w-full q-pa-md q-mt-md")
        config_card.set_visibility(False)

        with config_card:
            ui.label("Receiver Configuration").classes("text-h6 text-white")
            ui.label(
                "Port protocols, GNSS and the fields below reflect the live "
                "receiver, or a picked profile once one's selected — this "
                "display isn't click-to-edit yet. The RTCM matrix and "
                "data-link port(s) further down are — edit, then Apply."
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
                modified_badge = (
                    ui.badge("").classes("modified-badge").props("color=warning")
                )
                modified_badge.set_visibility(False)

            apply_result_label = (
                ui.label("")
                .classes("apply-result text-caption q-mt-sm q-pa-sm")
                .style("border: 1px solid #333; border-radius: 4px")
            )
            apply_result_label.set_visibility(False)
            apply_diff_list = ui.column().classes("apply-diff-list q-mt-xs gap-0")

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
            """Render the profile picker from the current device identity."""
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

            picker_list.clear()
            with picker_list:
                for entry in entries:
                    is_selected = (
                        selected_profile is not None
                        and selected_profile.name == entry.profile.name
                    )
                    is_incompatible = (
                        not entry.compatible and entry.incompatible_reason is not None
                    )
                    row_classes = f"profile-row profile-row-{entry.profile.name} items-center gap-2 q-py-xs justify-between"
                    if is_selected:
                        row_classes += " profile-row-selected"
                    with ui.row().classes(row_classes) as row:
                        name_classes = (
                            "text-white" if entry.compatible else "text-grey-6"
                        )
                        # The clickable "select" target is its own inner
                        # row so the rename/delete/export icons — siblings
                        # inside the outer row, not descendants of this one
                        # — never bubble a click into profile selection.
                        select_classes = "items-center gap-2" + (
                            " cursor-pointer" if entry.compatible else ""
                        )
                        with ui.row().classes(select_classes) as select_area:
                            if entry.compatible:
                                select_area.on(
                                    "click",
                                    lambda _, p=entry.profile: _select_profile(p),
                                )
                            if is_selected:
                                ui.icon("check_circle").classes(
                                    "profile-selected-icon text-primary"
                                )
                            ui.label(display_label(entry.profile)).classes(
                                f"profile-name {name_classes}"
                            )
                            ui.badge(
                                "built-in" if entry.is_builtin else "custom"
                            ).props("outline color=grey")
                            if entry.is_default:
                                ui.badge("Suggested").classes(
                                    "profile-suggested-badge"
                                ).props("color=primary")
                            if entry.incompatible_reason:
                                ui.icon("info").classes(
                                    "profile-incompatible-icon text-grey-5"
                                ).tooltip(entry.incompatible_reason)

                        if is_incompatible:
                            row.classes("opacity-60")

                        if not entry.is_builtin:
                            with ui.row().classes("gap-2 items-center"):
                                ui.icon("edit").classes(
                                    "profile-rename-icon text-grey-4 cursor-pointer"
                                ).on(
                                    "click",
                                    lambda _, p=entry.profile: _open_rename_dialog(p),
                                ).tooltip("Rename")
                                ui.icon("delete").classes(
                                    "profile-delete-icon text-grey-4 cursor-pointer"
                                ).on(
                                    "click",
                                    lambda _, p=entry.profile: _open_delete_dialog(p),
                                ).tooltip("Delete")
                                ui.icon("download").classes(
                                    "profile-export-icon text-grey-4 cursor-pointer"
                                ).on(
                                    "click",
                                    lambda _, n=entry.profile.name: _export_profile(n),
                                ).tooltip("Export")

        def _select_profile(profile: Profile) -> None:
            """Pick a profile — pre-fills the whole form (issue #66).

            This is expected to put the form out of sync with the
            receiver (per spec that's correct and legible, precisely
            what Apply is for) — ``_on_form_changed`` recomputes that
            indicator same as any other edit. The form remains the
            source of truth afterwards: further matrix/data-link edits
            mutate it same as before, independent of the profile pick.
            """
            nonlocal selected_profile, form_matrix, form_data_link_ports, form_extras
            selected_profile = profile
            form_matrix = profile_matrix_to_form_matrix(profile)
            form_data_link_ports = list(profile.data_link_port)
            form_extras = profile_to_form_extras(profile)

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

            form_config = _current_form_config()
            if form_config is None:
                save_as_error_label.text = (
                    "The form doesn't currently validate — fix the RTCM "
                    "matrix / data-link ports before saving."
                )
                save_as_error_label.set_visibility(True)
                return

            hardware = resolve_save_hardware(selected_profile, _current_identity())
            forked_from = selected_profile.name if selected_profile else None

            try:
                profile = build_saved_profile(name, form_config, hardware, forked_from)
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

        # Editable form state (issue #65) — seeded from the live receiver on
        # every load/reload/reconnect, then mutated by matrix clicks and the
        # data-link checkboxes until the next Apply or reseed.
        form_matrix: dict[RtcmRowId, dict[PortId, bool]] = rtcm_config_to_matrix(
            RtcmPortConfig()
        )
        live_matrix: dict[RtcmRowId, dict[PortId, bool]] = copy_matrix(form_matrix)
        form_data_link_ports: list[PortId] = []

        # Issue #66 additions — the rest of the "hardware section" (ports,
        # GNSS, baud, role fields, optimisations), the profile a pick
        # selected (None = no pick, the default/transient state), and the
        # live ports/GNSS read-backs the "Port Protocols"/"GNSS
        # Constellations" display falls back to when no profile is picked.
        form_extras = FormExtras()
        selected_profile: Profile | None = None
        live_ports = PortProtocolConfig()
        live_gnss = GnssConfig()
        rename_target: str | None = None
        delete_target: str | None = None
        delete_target_label: str = ""

        def _out_of_sync() -> bool:
            return form_matrix != live_matrix

        def _current_form_config() -> ReceiverConfig | None:
            """The form as a ``ReceiverConfig``, or ``None`` if it doesn't
            currently validate (e.g. no data-link port selected)."""
            try:
                return build_apply_config(
                    form_matrix, form_data_link_ports, form_extras
                )
            except ValidationError:
                return None

        def _render_ports_view() -> None:
            ports_view.clear()
            with ports_view:
                display = resolve_ports_display(live_ports, form_extras.ports)
                for port_id in (PortId.UART1, PortId.UART2, PortId.USB):
                    in_names, out_names = display[port_id]
                    with ui.row().classes("items-center gap-2"):
                        ui.label(port_id.value).classes("text-white").style(
                            "width: 60px; flex-shrink: 0"
                        )
                        ui.label("IN").classes("text-caption text-grey-5")
                        for name in in_names:
                            ui.badge(name).props("outline color=grey")
                        ui.label("OUT").classes("text-caption text-grey-5 q-ml-md")
                        for name in out_names:
                            ui.badge(name).props("outline color=primary")

        def _render_gnss_view() -> None:
            gnss_view.clear()
            with gnss_view:
                enabled_map = resolve_gnss_display(
                    live_gnss, form_extras.constellations
                )
                for c_val, c_name in _GNSS_DISPLAY:
                    enabled = enabled_map.get(c_val, False)
                    ui.badge(c_name).props(
                        "color=positive" if enabled else "outline color=grey"
                    )

        def _render_hw_extras_view() -> None:
            hw_extras_view.clear()
            with hw_extras_view:
                for css_class, label, value in hw_extras_display(form_extras):
                    with ui.column().classes(f"{css_class} gap-0"):
                        ui.label(label).classes("text-caption text-grey-5")
                        ui.label(value).classes("text-white")

        def _toggle_matrix_cell(msg_id: RtcmRowId, port: PortId) -> None:
            form_matrix[msg_id][port] = not form_matrix[msg_id][port]
            _render_matrix()
            _on_form_changed()

        def _render_matrix() -> None:
            matrix_view.clear()
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
                                on = form_matrix[msg_id][port]
                                cell_classes = (
                                    f"rtcm-cell rtcm-cell-{slug}-{port.value} "
                                    "text-center cursor-pointer"
                                    + (" text-positive" if on else " text-grey-7")
                                )
                                ui.label("✓" if on else "-").classes(
                                    cell_classes
                                ).style("width: 70px; flex-shrink: 0").on(
                                    "click",
                                    lambda _, m=msg_id, p=port: _toggle_matrix_cell(
                                        m, p
                                    ),
                                )

        def _render_data_link_picker() -> None:
            inferred = infer_data_link_ports(form_matrix)
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
            if _out_of_sync():
                sync_badge.text = "Receiver out of sync"
                sync_badge.props("color=warning")
            else:
                sync_badge.text = "In sync"
                sync_badge.props("color=positive")

        def _render_apply_gate() -> None:
            reason = apply_blocked_reason(form_data_link_ports)
            apply_btn.set_enabled(reason is None)
            data_link_blocked_label.text = reason or ""
            data_link_blocked_label.set_visibility(reason is not None)

        def _render_modified_indicator() -> None:
            """The second, independent indicator — form vs. *selected
            profile*, distinct from "out of sync" (form vs. live receiver).
            Hidden when no profile is selected: nothing to have diverged
            from."""
            form_config = _current_form_config()
            if selected_profile is None or form_config is None:
                modified_badge.set_visibility(False)
                return
            modified_badge.set_visibility(True)
            if is_modified_from_profile(form_config, selected_profile):
                modified_badge.text = f"Modified from {display_label(selected_profile)}"
                modified_badge.props("color=warning")
            else:
                modified_badge.text = f"Matches {display_label(selected_profile)}"
                modified_badge.props("color=positive")

        def _render_save_as_gate() -> None:
            save_as_btn.set_enabled(
                save_as_enabled(_current_form_config(), selected_profile)
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
            apply_diff_list.clear()
            apply_result_label.text = text
            apply_result_label.classes(
                remove="text-negative" if ok else "text-positive",
                add="text-positive" if ok else "text-negative",
            )
            apply_result_label.set_visibility(True)

        def _clear_apply_result() -> None:
            apply_result_label.set_visibility(False)
            apply_diff_list.clear()

        def _show_apply_diff(diff: list[ApplyConfigCellDiff]) -> None:
            apply_diff_list.clear()
            with apply_diff_list:
                for cell in diff:
                    ui.label(format_cell_diff(cell)).classes(
                        "text-caption text-warning"
                    )

        async def _apply() -> None:
            """Push the current form (matrix + data-link ports) to the receiver.

            Deliberately excludes ``form_extras`` (issue #66 review):
            those fields have no live read-back, so a profile-populated
            value pushed through Apply could never be verified by the
            read-back-diff below, unlike the matrix. Extending Apply to
            the full hardware section is out of #66's scope — it isn't
            in the acceptance criteria, and #65 deliberately scoped
            Apply to "only the RTCM matrix and the data-link ports".
            ``form_extras`` is still used for Save-as/"modified from X",
            which compare in-form state and never touch the receiver.
            """
            nonlocal live_matrix
            try:
                config = build_apply_config(form_matrix, form_data_link_ports)
            except ValidationError as exc:
                _set_apply_result(
                    f"Apply refused: {exc.errors()[0]['msg']} — nothing was written.",
                    ok=False,
                )
                return

            try:
                result = await svc.apply_receiver_config(config)
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

            if result.status == "ok":
                live_matrix = copy_matrix(form_matrix)
                _set_apply_result("Applied and verified ✓", ok=True)
                ui.notify("Applied and verified ✓", type="positive")
            else:
                # The writes landed but the read-back disagrees — reflect
                # the receiver's *actual* state so "out of sync" stays
                # honest even though only a successful apply clears it.
                for cell in result.diff:
                    live_matrix[cell.row_id][cell.port] = cell.actual
                _set_apply_result(
                    "Applied, but verification found mismatches — nothing "
                    "was rolled back.",
                    ok=False,
                )
                _show_apply_diff(result.diff)
                ui.notify("Verification found mismatches", type="warning")

            if result.warnings:
                ui.notify(" ".join(result.warnings), type="warning")

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
            nonlocal form_matrix, live_matrix, form_data_link_ports
            nonlocal form_extras, selected_profile, live_ports, live_gnss
            rtcm = await svc.get_rtcm_port_config()
            ports = await svc.get_port_protocols()
            gnss = await svc.get_gnss_config()

            form_matrix = rtcm_config_to_matrix(rtcm)
            live_matrix = copy_matrix(form_matrix)
            form_data_link_ports = infer_data_link_ports(form_matrix)
            form_extras = FormExtras()
            selected_profile = None
            live_ports = ports
            live_gnss = gnss

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
