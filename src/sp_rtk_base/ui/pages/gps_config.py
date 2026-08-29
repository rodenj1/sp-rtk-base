"""Advanced GPS Configuration page — profile picker, live-seeded form, Apply, handoff.

Provides the profile-based GPS receiver setup flow (issue #54): a
profile picker tagged with hardware compatibility, a receiver-
configuration view seeded from the live device (RTCM matrix, port
protocols, GNSS constellations), save-to-flash, and relay handoff.

Issue #64 shipped the read-only shell; issue #65 (this revision) makes
the RTCM matrix and data-link port(s) editable and wires them to
``POST /api/device/apply-config`` via ``DeviceService.apply_receiver_config``,
plus the "receiver out of sync" indicator (form vs. live receiver,
cleared only by a successful apply). Selecting a profile is still
rendering-only — it does not write into the form; that wiring, plus
editing ports/GNSS/role fields, lands in a later ticket (#66).
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# pyright: reportOptionalMemberAccess=false
# pyright: reportOptionalIterable=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

import logging
from dataclasses import dataclass

from nicegui import ui
from pydantic import ValidationError

from sp_rtk_base.models.config_models import DeviceProfile
from sp_rtk_base.models.device_models import (
    ALL_RTCM_MESSAGE_IDS,
    RTCM_MESSAGE_GROUPS,
    ApplyConfigCellDiff,
    DeviceCapability,
    DeviceConnectionState,
    GnssConfig,
    PortId,
    PortProtocolConfig,
    RtcmOutputPort,
    RtcmPortConfig,
    RtcmRowId,
)
from sp_rtk_base.models.hardware_identity import (
    HARDWARE_UNKNOWN,
    HardwareConfidence,
    HardwareIdentity,
    default_selection,
    identity_from_target,
    incompatible_reason,
    is_compatible,
)
from sp_rtk_base.models.profile_models import Profile, ReceiverConfig, RtcmStreamConfig
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
from sp_rtk_base.services.profile_store import ProfileStore
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


def build_apply_config(
    matrix: dict[RtcmRowId, dict[PortId, bool]],
    data_link_port: list[PortId],
) -> ReceiverConfig:
    """Build the ``ReceiverConfig`` Apply pushes from the editable form state.

    Only the RTCM matrix and the data-link ports are editable on this
    page (issue #65) — every other field is left at its schema default
    / ``None`` so Apply never touches ports, GNSS, role fields or baud
    that this page doesn't yet expose for editing.

    Raises:
        pydantic.ValidationError: If the resulting config fails a
            context-free rule (e.g. 1005 missing from every chosen
            data-link port) — a client-side pre-write refusal, nothing
            is sent to the receiver.
    """
    return ReceiverConfig(
        data_link_port=data_link_port,
        rtcm_stream=RtcmStreamConfig(matrix=matrix),
    )


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
                "Port protocols and GNSS reflect the receiver and aren't "
                "editable here yet. The RTCM matrix and data-link port(s) "
                "below are — edit, then Apply."
            ).classes("text-grey-4 q-mt-xs text-caption")
            ui.separator()

            ui.label("Port Protocols").classes("text-subtitle2 text-white q-mt-sm")
            ports_view = ui.column().classes("q-mt-xs gap-1 w-full")

            ui.label("GNSS Constellations").classes("text-subtitle2 text-white q-mt-md")
            gnss_view = ui.row().classes("q-mt-xs gap-2 flex-wrap")

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

            apply_result_label = (
                ui.label("")
                .classes("apply-result text-caption q-mt-sm q-pa-sm")
                .style("border: 1px solid #333; border-radius: 4px")
            )
            apply_result_label.set_visibility(False)
            apply_diff_list = ui.column().classes("apply-diff-list q-mt-xs gap-0")

        # ================================================================
        # Section D: Save to Flash (hidden until connected + capable)
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
        # Section E: Handoff to Relay
        # ================================================================
        handoff_card = ui.card().classes("w-full q-pa-md q-mt-md")
        handoff_card.set_visibility(False)

        with handoff_card:
            ui.label("Handoff to Relay").classes("text-h6 text-white")
            ui.separator()
            ui.label(
                "Disconnect the device driver, configure the relay input "
                "with the same serial port, and start the relay engine."
            ).classes("text-grey-4 q-mt-sm")
            handoff_btn = (
                ui.button("Handoff & Start Relay", icon="swap_horiz")
                .props("color=positive")
                .classes("q-mt-sm")
            )

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
            info = svc.device_info
            identity = resolve_identity(
                info.hardware_target if info else None,
                info.hardware_confidence if info else None,
            )
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
                    row_classes = f"profile-row profile-row-{entry.profile.name} items-center gap-2 q-py-xs"
                    with ui.row().classes(row_classes) as row:
                        name_classes = (
                            "text-white" if entry.compatible else "text-grey-6"
                        )
                        ui.label(entry.profile.name).classes(
                            f"profile-name {name_classes}"
                        )
                        ui.badge("built-in" if entry.is_builtin else "custom").props(
                            "outline color=grey"
                        )
                        if entry.is_default:
                            ui.badge("Suggested").classes(
                                "profile-suggested-badge"
                            ).props("color=primary")
                        if not entry.compatible and entry.incompatible_reason:
                            row.classes("opacity-60")
                            ui.icon("info").classes(
                                "profile-incompatible-icon text-grey-5"
                            ).tooltip(entry.incompatible_reason)

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
            handoff_card.set_visibility(connected)
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

        def _out_of_sync() -> bool:
            return form_matrix != live_matrix

        def _render_ports_table(ports: PortProtocolConfig) -> None:
            ports_view.clear()
            with ports_view:
                for port_id in (PortId.UART1, PortId.UART2, PortId.USB):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(port_id.value).classes("text-white").style(
                            "width: 60px; flex-shrink: 0"
                        )
                        ui.label("IN").classes("text-caption text-grey-5")
                        for proto in ports.enabled_in(port_id):
                            ui.badge(proto.value).props("outline color=grey")
                        ui.label("OUT").classes("text-caption text-grey-5 q-ml-md")
                        for proto in ports.enabled_out(port_id):
                            ui.badge(proto.value).props("outline color=primary")

        def _render_gnss(gnss: GnssConfig) -> None:
            enabled_map: dict[str, bool] = {
                sys_cfg.constellation.value: sys_cfg.enabled for sys_cfg in gnss.systems
            }
            gnss_view.clear()
            with gnss_view:
                for c_val, c_name in _GNSS_DISPLAY:
                    enabled = enabled_map.get(c_val, False)
                    ui.badge(c_name).props(
                        "color=positive" if enabled else "outline color=grey"
                    )

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

        def _on_form_changed() -> None:
            """Recompute every reactive bit of the form after an edit.

            Includes the data-link picker so its "inferred from current
            RTCM state" hint stays current after a matrix toggle, not
            just after a reseed.
            """
            _render_data_link_picker()
            _render_sync_indicator()
            _render_apply_gate()

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
            """Push the current form (matrix + data-link ports) to the receiver."""
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

        async def _load_receiver_config_form() -> None:
            """Seed the form from the live receiver.

            The RTCM matrix and data-link ports are freshly inferred here
            every time — on connect, reload, and reconnect — so the form
            starts in sync by construction (issue #65), matching the rest
            of this page's existing live-seeding behaviour.
            """
            nonlocal form_matrix, live_matrix, form_data_link_ports
            rtcm = await svc.get_rtcm_port_config()
            ports = await svc.get_port_protocols()
            gnss = await svc.get_gnss_config()

            form_matrix = rtcm_config_to_matrix(rtcm)
            live_matrix = copy_matrix(form_matrix)
            form_data_link_ports = infer_data_link_ports(form_matrix)

            _render_ports_table(ports)
            _render_gnss(gnss)
            _render_matrix()
            _render_advisory(rtcm)
            _clear_apply_result()
            _on_form_changed()

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

        async def _handoff_to_relay() -> None:
            """Disconnect device and hand off serial port to the relay."""
            import httpx

            try:
                async with httpx.AsyncClient(
                    base_url="http://localhost:8080"
                ) as client:
                    resp = await client.post("/api/device/handoff", timeout=15.0)
                    if resp.status_code == 200:
                        ui.notify("Handed off to relay! ✓", type="positive")
                        ui.navigate.to("/")
                    else:
                        detail = resp.json().get("detail", resp.text)
                        ui.notify(f"Handoff failed: {detail}", type="negative")
            except Exception as exc:
                ui.notify(f"Handoff error: {exc}", type="negative")
            _update_ui_state()

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
        save_flash_btn.on_click(_save_flash)
        handoff_btn.on_click(_handoff_to_relay)

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
