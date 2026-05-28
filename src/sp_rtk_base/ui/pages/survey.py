"""Survey-In page — GPS connection, survey workflow, and position management.

Provides the complete survey-in workflow: connect to GPS receiver,
run a survey-in (auto-promote + auto-flash), manage the fixed base
position, and save/restore position profiles.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# pyright: reportOptionalMemberAccess=false
# pyright: reportOptionalIterable=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

import logging

from nicegui import ui

from sp_rtk_base.models.config_models import DeviceProfile
from sp_rtk_base.models.device_models import (
    BaseMode,
    DeviceCapability,
    DeviceConnectionState,
)
from sp_rtk_base.services import get_config_service, get_device_service
from sp_rtk_base.services.drivers import create_driver, list_drivers
from sp_rtk_base.services.drivers.base import GpsReceiverDriver
from sp_rtk_base.ui.layout import page_layout

logger = logging.getLogger(__name__)

BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
DEFAULT_BAUD = 115200


@ui.page("/survey")
def survey_page() -> None:
    """Render the survey-in workflow page."""
    svc = get_device_service()
    config_svc = get_config_service()

    with page_layout("Survey-In"):
        ui.label("Survey-In").classes("text-h4 text-white q-mb-md")

        # ================================================================
        # Card 1: Connection & Live Position
        # ================================================================
        with ui.card().classes("w-full q-pa-md"):
            ui.label("Connection & Live Position").classes("text-h6 text-white")
            ui.separator()

            status_row = ui.row().classes("items-center gap-2 q-mt-sm")
            error_label = ui.label("").classes("text-negative q-mt-xs")
            error_label.set_visibility(False)

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

            with ui.row().classes("gap-2 q-mt-sm items-center"):
                connect_btn = ui.button("Connect", icon="link")
                disconnect_btn = ui.button("Disconnect", icon="link_off").props(
                    "color=grey"
                )
                cancel_btn = ui.button("Cancel", icon="cancel").props(
                    "color=negative outline"
                )
                cancel_btn.set_visibility(False)
                reload_device_btn = ui.button("Reload Device Data", icon="sync").props(
                    "color=info outline"
                )
                reload_device_btn.set_visibility(False)
                refresh_btn = (
                    ui.button("", icon="refresh")
                    .props("flat round color=white")
                    .tooltip("Refresh serial port list")
                )

            info_card = ui.card().classes("w-full q-pa-md q-mt-md")
            info_card.set_visibility(False)

            # Live Position section
            position_section = ui.column().classes("w-full q-mt-md")
            position_section.set_visibility(False)

            with position_section:
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label("Live Position").classes("text-subtitle1 text-white")
                    pos_fix_badge = ui.badge("No Fix").props("color=grey outline")

                with ui.row().classes("w-full gap-4 q-mt-sm sp-metric-row"):
                    with ui.column().classes("gap-0"):
                        ui.label("Latitude").classes("text-caption text-grey-5")
                        pos_lat_label = ui.label("—").classes("text-white")
                    with ui.column().classes("gap-0"):
                        ui.label("Longitude").classes("text-caption text-grey-5")
                        pos_lon_label = ui.label("—").classes("text-white")
                    with ui.column().classes("gap-0"):
                        ui.label("Altitude (MSL)").classes("text-caption text-grey-5")
                        pos_alt_label = ui.label("—").classes("text-white")

                with ui.row().classes("w-full gap-4 q-mt-xs sp-metric-row"):
                    with ui.column().classes("gap-0"):
                        ui.label("H Accuracy").classes("text-caption text-grey-5")
                        pos_hacc_label = ui.label("—").classes("text-white")
                    with ui.column().classes("gap-0"):
                        ui.label("V Accuracy").classes("text-caption text-grey-5")
                        pos_vacc_label = ui.label("—").classes("text-white")
                    with ui.column().classes("gap-0"):
                        ui.label("Satellites").classes("text-caption text-grey-5")
                        pos_sats_label = ui.label("—").classes("text-white")
                    with ui.column().classes("gap-0"):
                        ui.label("PDOP").classes("text-caption text-grey-5")
                        pos_pdop_label = ui.label("—").classes("text-white")

                with ui.row().classes("w-full gap-4 q-mt-xs sp-metric-row"):
                    with ui.column().classes("gap-0"):
                        ui.label("RTK Status").classes("text-caption text-grey-5")
                        pos_rtk_label = ui.label("—").classes("text-white")
                    with ui.column().classes("gap-0"):
                        ui.label("Speed").classes("text-caption text-grey-5")
                        pos_speed_label = ui.label("—").classes("text-white")
                    with ui.column().classes("gap-0"):
                        ui.label("UTC Time").classes("text-caption text-grey-5")
                        pos_time_label = ui.label("—").classes("text-white")

        # ================================================================
        # Card 2: Survey-In (standalone)
        # ================================================================
        survey_card = ui.card().classes("w-full q-pa-md q-mt-md")
        survey_card.set_visibility(False)

        with survey_card:
            ui.label("Survey-In").classes("text-h6 text-white")
            ui.separator()
            ui.label(
                "Run a survey to determine the precise antenna position. "
                "Once complete, the position is automatically committed to "
                "fixed base mode and saved to flash."
            ).classes("text-grey-4 q-mt-xs text-caption")

            with ui.row().classes("w-full gap-4 q-mt-sm sp-metric-row"):
                svin_duration = ui.number(
                    "Min Duration (seconds)",
                    value=120,
                    min=60,
                    max=86400,
                    step=60,
                ).classes("col-grow")
                svin_accuracy = ui.number(
                    "Accuracy Limit (mm)",
                    value=50000,
                    min=1000,
                    max=500000,
                    step=1000,
                ).classes("col-grow")

            with ui.row().classes("gap-2 q-mt-sm items-center"):
                svin_start_btn = ui.button("Start Survey-In", icon="play_arrow").props(
                    "color=primary"
                )
                svin_cancel_btn = ui.button("Cancel Survey", icon="stop").props(
                    "color=negative outline"
                )
                svin_cancel_btn.set_visibility(False)

            # Progress section — shown as soon as Start is pressed and kept
            # visible across the whole survey lifecycle (success, failure, or
            # cancel) so the operator always has a status surface.
            svin_progress_card = (
                ui.card()
                .classes("w-full q-pa-sm q-mt-md")
                .style("background-color: #1a1a2e")
            )
            svin_progress_card.set_visibility(False)

            with svin_progress_card:
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label("Survey-In Progress").classes("text-subtitle2 text-grey-4")
                    svin_target_label = ui.label("").classes("text-caption text-grey-5")
                svin_status_label = ui.label("Idle").classes("text-white")
                # In-card error banner — replaces toast-only feedback so a
                # config-write failure stays visible after the toast fades.
                svin_error_label = ui.label("").classes(
                    "text-negative text-caption q-mt-xs"
                )
                svin_error_label.set_visibility(False)
                # Softer warning banner for "survey is running but not
                # converging" (e.g. poor antenna placement).  Hidden
                # until the convergence heuristic fires; cleared on
                # Start / Cancel.
                svin_warning_label = ui.label("").classes(
                    "text-warning text-caption q-mt-xs"
                )
                svin_warning_label.set_visibility(False)
                svin_progress_bar = ui.linear_progress(
                    value=0, show_value=False
                ).classes("q-mt-xs")

                with ui.row().classes("w-full gap-4 q-mt-xs sp-metric-row"):
                    svin_dur_label = ui.label("Duration: 0s").classes("text-grey-3")
                    svin_acc_label = ui.label("Accuracy: 0mm").classes("text-grey-3")
                    svin_obs_label = ui.label("Observations: 0").classes("text-grey-3")

                with ui.row().classes("w-full gap-4 q-mt-xs sp-metric-row"):
                    svin_pct_label = ui.label("% to target: —").classes("text-grey-3")
                    svin_eta_label = ui.label("ETA: —").classes("text-grey-3")

                # Convergence chart
                svin_chart = (
                    ui.echart(
                        {
                            "backgroundColor": "transparent",
                            "animation": True,
                            "grid": {"top": 30, "right": 20, "bottom": 40, "left": 65},
                            "tooltip": {
                                "trigger": "axis",
                                "backgroundColor": "#1a1a2e",
                                "borderColor": "#444",
                                "textStyle": {"color": "#ccc"},
                            },
                            "legend": {
                                "data": ["Accuracy (mm)", "Observations"],
                                "textStyle": {"color": "#888"},
                                "top": 0,
                            },
                            "xAxis": {
                                "type": "category",
                                "data": [],
                                "name": "Elapsed (s)",
                                "nameTextStyle": {"color": "#888"},
                                "axisLabel": {"color": "#888"},
                                "axisLine": {"lineStyle": {"color": "#444"}},
                                "splitLine": {"show": False},
                            },
                            "yAxis": [
                                {
                                    "type": "log",
                                    "name": "Accuracy (mm)",
                                    "nameTextStyle": {"color": "#888"},
                                    "axisLabel": {
                                        "color": "#888",
                                        "formatter": "{value}",
                                    },
                                    "axisLine": {"lineStyle": {"color": "#444"}},
                                    "splitLine": {
                                        "lineStyle": {
                                            "color": "#2a2a3e",
                                            "type": "dashed",
                                        }
                                    },
                                    "min": 100,
                                },
                                {
                                    "type": "value",
                                    "name": "Observations",
                                    "nameTextStyle": {"color": "#888"},
                                    "axisLabel": {"color": "#888"},
                                    "axisLine": {"lineStyle": {"color": "#444"}},
                                    "splitLine": {"show": False},
                                },
                            ],
                            "series": [
                                {
                                    "name": "Accuracy (mm)",
                                    "type": "line",
                                    "data": [],
                                    "smooth": True,
                                    "symbol": "none",
                                    "lineStyle": {"width": 2},
                                    "itemStyle": {"color": "#ff6b6b"},
                                    "areaStyle": {
                                        "color": {
                                            "type": "linear",
                                            "x": 0,
                                            "y": 0,
                                            "x2": 0,
                                            "y2": 1,
                                            "colorStops": [
                                                {
                                                    "offset": 0,
                                                    "color": "rgba(255,107,107,0.3)",
                                                },
                                                {
                                                    "offset": 1,
                                                    "color": "rgba(255,107,107,0.02)",
                                                },
                                            ],
                                        },
                                    },
                                    "yAxisIndex": 0,
                                    "markLine": {
                                        "silent": True,
                                        "data": [],
                                        "lineStyle": {
                                            "type": "dashed",
                                            "color": "#51cf66",
                                            "width": 2,
                                        },
                                        "label": {
                                            "color": "#51cf66",
                                            "fontSize": 11,
                                            "formatter": "Target: {c} mm",
                                        },
                                    },
                                },
                                {
                                    "name": "Observations",
                                    "type": "line",
                                    "data": [],
                                    "smooth": True,
                                    "symbol": "none",
                                    "lineStyle": {"width": 1, "type": "dotted"},
                                    "itemStyle": {"color": "#74c0fc"},
                                    "yAxisIndex": 1,
                                },
                            ],
                        }
                    )
                    .classes("w-full q-mt-sm")
                    .style("height: 260px")
                )

        # ================================================================
        # Card 3: Fixed Base Position (merged)
        # ================================================================
        fixed_card = ui.card().classes("w-full q-pa-md q-mt-md")
        fixed_card.set_visibility(False)

        with fixed_card:
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Fixed Base Position").classes("text-h6 text-white")
                fb_mode_badge = ui.badge("—").props("color=grey outline")
            ui.separator()

            # Read-only display
            fb_readonly = ui.column().classes("w-full")
            with fb_readonly:
                with ui.row().classes("w-full gap-4 q-mt-sm sp-metric-row"):
                    with ui.column().classes("gap-0"):
                        ui.label("Latitude").classes("text-caption text-grey-5")
                        fb_lat_label = ui.label("—").classes("text-white")
                    with ui.column().classes("gap-0"):
                        ui.label("Longitude").classes("text-caption text-grey-5")
                        fb_lon_label = ui.label("—").classes("text-white")
                    with ui.column().classes("gap-0"):
                        ui.label("Altitude").classes("text-caption text-grey-5")
                        fb_alt_label = ui.label("—").classes("text-white")
                    with ui.column().classes("gap-0"):
                        ui.label("Accuracy").classes("text-caption text-grey-5")
                        fb_acc_label = ui.label("—").classes("text-white")

                # Soft hint under the mode badge.  Shown when the
                # receiver is in survey_in mode so the operator knows
                # Reset GPS is the right next action if the survey
                # has stalled (e.g. poor antenna placement).  Hidden
                # for disabled / fixed modes.
                fb_mode_hint = ui.label("").classes("text-caption text-warning q-mt-xs")
                fb_mode_hint.set_visibility(False)

                with ui.row().classes("gap-2 q-mt-md"):
                    fb_edit_btn = ui.button("Edit", icon="edit").props(
                        "color=primary outline"
                    )
                    fb_save_btn = ui.button("Save Position", icon="bookmark_add").props(
                        "color=primary outline"
                    )
                    fb_load_btn = ui.button("Load Saved", icon="folder_open").props(
                        "color=primary outline"
                    )
                    # Hardware-reset the receiver to clear stale
                    # BBR-backed survey state (NAV-SVIN.dur).  Becomes
                    # amber when mode=survey_in to draw attention.
                    fb_reset_btn = ui.button("Reset GPS", icon="restart_alt").props(
                        "color=primary outline"
                    )

            # Edit mode (hidden by default)
            fb_editmode = ui.column().classes("w-full")
            fb_editmode.set_visibility(False)
            with fb_editmode:
                with ui.row().classes("w-full gap-4 q-mt-sm sp-metric-row"):
                    fb_edit_lat = ui.number(
                        "Latitude (°)",
                        value=0.0,
                        min=-90.0,
                        max=90.0,
                        step=0.0000001,
                        format="%.7f",
                    ).classes("col-grow")
                    fb_edit_lon = ui.number(
                        "Longitude (°)",
                        value=0.0,
                        min=-180.0,
                        max=180.0,
                        step=0.0000001,
                        format="%.7f",
                    ).classes("col-grow")
                with ui.row().classes("w-full gap-4 sp-metric-row"):
                    fb_edit_alt = ui.number(
                        "Altitude (m)",
                        value=0.0,
                        min=-1000.0,
                        max=100000.0,
                        step=0.01,
                        format="%.2f",
                    ).classes("col-grow")
                    fb_edit_acc = ui.number(
                        "Accuracy (mm)",
                        value=1000,
                        min=1,
                        max=100000,
                        step=100,
                    ).classes("col-grow")

                with ui.row().classes("gap-2 q-mt-md"):
                    fb_commit_btn = ui.button("Commit", icon="check").props(
                        "color=positive"
                    )
                    fb_cancel_btn = ui.button("Cancel", icon="close").props(
                        "flat color=grey"
                    )

        # ================================================================
        # Card 4: Saved Positions
        # ================================================================
        with ui.card().classes("w-full q-pa-md q-mt-md"):
            ui.label("Saved Positions").classes("text-h6 text-white")
            ui.separator()
            ui.label(
                "Previously surveyed positions. Restore to commit directly "
                "to the device (RAM + flash)."
            ).classes("text-grey-4 q-mt-xs text-caption")
            saved_pos_container = ui.column().classes("w-full q-mt-sm gap-2")

        # ================================================================
        # State & Event Handlers
        # ================================================================

        svin_timer: ui.timer | None = None
        pos_timer: ui.timer | None = None

        _svin_chart_times: list[str] = []
        _svin_chart_acc: list[float] = []
        _svin_chart_obs: list[int] = []
        # Rolling samples used for ETA extrapolation.  Each entry is
        # (elapsed_seconds, accuracy_mm).  We use the most recent
        # window (~30 s) to estimate the convergence slope and project
        # how long until accuracy crosses the target threshold.
        _svin_eta_samples: list[tuple[int, float]] = []
        _ETA_WINDOW_SECONDS: int = 30

        # Receiver-side ``dur`` value captured on the *first* NAV-SVIN
        # poll after a fresh "Start Survey-In" press.  All displayed
        # durations and chart x-axis values are offset by this number
        # so the UI always counts from zero, even on the off chance
        # the receiver's NAV-SVIN dur counter still carries a small
        # residue from the previous session that survived the
        # configure_survey_in TMODE-reset verification window.
        # ``None`` means "no survey started yet from this UI session".
        _svin_dur_offset: int | None = None

        # Current fixed base position (from device read-back)
        _fb_lat: float = 0.0
        _fb_lon: float = 0.0
        _fb_alt: float = 0.0
        _fb_acc: float = 0.0

        def _refresh_ports() -> None:
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
            profile = config_svc.get_device_profile()
            if profile and profile.port:
                port_select.value = profile.port
            if profile and profile.baud_rate:
                baud_select.value = profile.baud_rate
            if profile and profile.vendor:
                driver_select.value = profile.vendor

        def _save_device_settings() -> None:
            try:
                config_svc.save_device_profile(
                    DeviceProfile(
                        port=str(port_select.value or ""),
                        baud_rate=int(baud_select.value or DEFAULT_BAUD),
                        vendor=str(driver_select.value or "ublox"),
                    )
                )
            except Exception:
                pass

        def _update_ui_state() -> None:
            state = svc.state
            connected = state == DeviceConnectionState.CONNECTED
            caps = svc.capabilities

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

            connecting = state == DeviceConnectionState.CONNECTING
            connect_btn.set_visibility(not connected and not connecting)
            disconnect_btn.set_visibility(connected)
            cancel_btn.set_visibility(connecting)
            reload_device_btn.set_visibility(connected)

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

            position_section.set_visibility(connected)
            survey_card.set_visibility(connected and DeviceCapability.SURVEY_IN in caps)
            fixed_card.set_visibility(connected)

            status = svc.get_status()
            if status.last_error:
                error_label.text = f"Error: {status.last_error}"
                error_label.set_visibility(True)
            else:
                error_label.set_visibility(False)

        # ---- Live Position Polling ----

        async def _poll_position() -> None:
            try:
                pos = await svc.get_position()
                _FIX_COLORS = {
                    "no_fix": "grey",
                    "dead_reckoning": "orange",
                    "2d": "amber",
                    "3d": "green",
                    "gnss_dr": "light-green",
                    "time_only": "blue-grey",
                }
                _FIX_LABELS = {
                    "no_fix": "No Fix",
                    "dead_reckoning": "Dead Reckoning",
                    "2d": "2D Fix",
                    "3d": "3D Fix",
                    "gnss_dr": "GNSS+DR",
                    "time_only": "Time Only",
                }
                fix_val = (
                    pos.fix_type.value
                    if hasattr(pos.fix_type, "value")
                    else str(pos.fix_type)
                )
                pos_fix_badge.text = _FIX_LABELS.get(fix_val, fix_val)
                pos_fix_badge.props(f"color={_FIX_COLORS.get(fix_val, 'grey')}")

                if pos.latitude != 0.0 or pos.longitude != 0.0:
                    pos_lat_label.text = f"{pos.latitude:.7f}°"
                    pos_lon_label.text = f"{pos.longitude:.7f}°"
                    pos_alt_label.text = f"{pos.altitude_msl_m:.2f} m"
                else:
                    pos_lat_label.text = "—"
                    pos_lon_label.text = "—"
                    pos_alt_label.text = "—"

                pos_hacc_label.text = (
                    f"{pos.horizontal_accuracy_m:.3f} m"
                    if pos.horizontal_accuracy_m > 0
                    else "—"
                )
                pos_vacc_label.text = (
                    f"{pos.vertical_accuracy_m:.3f} m"
                    if pos.vertical_accuracy_m > 0
                    else "—"
                )
                pos_sats_label.text = str(pos.num_satellites)
                pos_pdop_label.text = f"{pos.pdop:.2f}" if pos.pdop > 0 else "—"

                _RTK_COLORS = {
                    "none": "text-grey-3",
                    "float": "text-amber",
                    "fixed": "text-green",
                }
                pos_rtk_label.text = pos.rtk_status.capitalize()
                pos_rtk_label.classes(
                    replace=_RTK_COLORS.get(pos.rtk_status, "text-white")
                )

                pos_speed_label.text = (
                    f"{pos.speed_m_s:.2f} m/s" if pos.speed_m_s > 0 else "0.00 m/s"
                )
                pos_time_label.text = (
                    pos.timestamp.strftime("%H:%M:%S UTC") if pos.timestamp else "—"
                )
            except Exception:
                pass

        # ---- Fixed Base Position Read-Back ----

        async def _read_fixed_base() -> None:
            nonlocal _fb_lat, _fb_lon, _fb_alt, _fb_acc
            try:
                bc = await svc.get_base_config()
                _MODE_COLORS: dict[BaseMode, str] = {
                    BaseMode.DISABLED: "grey",
                    BaseMode.SURVEY_IN: "amber",
                    BaseMode.FIXED: "green",
                }
                fb_mode_badge.text = bc.mode.value.replace("_", " ").title()
                fb_mode_badge.props(f"color={_MODE_COLORS.get(bc.mode, 'grey')}")

                # Hint + Reset-button styling per mode.  Survey-In is
                # the only "stuck-able" state — we draw the operator's
                # eye to Reset GPS when the receiver is in that mode.
                if bc.mode == BaseMode.SURVEY_IN:
                    fb_mode_hint.text = (
                        "Receiver in Survey-In mode.  If the survey "
                        "has stalled (poor antenna placement, divergent "
                        "accuracy), click Reset GPS for a clean start."
                    )
                    fb_mode_hint.set_visibility(True)
                    fb_reset_btn.props("color=warning outline")
                else:
                    fb_mode_hint.text = ""
                    fb_mode_hint.set_visibility(False)
                    fb_reset_btn.props("color=primary outline")

                _fb_lat = bc.latitude
                _fb_lon = bc.longitude
                _fb_alt = bc.altitude_m
                _fb_acc = float(bc.accuracy_mm)

                has_pos = bc.mode == BaseMode.FIXED or (
                    bc.latitude != 0.0 or bc.longitude != 0.0
                )
                if has_pos:
                    fb_lat_label.text = f"{bc.latitude:.7f}°"
                    fb_lon_label.text = f"{bc.longitude:.7f}°"
                    fb_alt_label.text = f"{bc.altitude_m:.3f} m"
                    fb_acc_label.text = f"{bc.accuracy_mm} mm"
                else:
                    fb_lat_label.text = "—"
                    fb_lon_label.text = "—"
                    fb_alt_label.text = "—"
                    fb_acc_label.text = "—"
            except Exception as exc:
                fb_mode_badge.text = "Error"
                fb_mode_badge.props("color=negative")
                logger.debug("Failed to read base config: %s", exc)

        async def _commit_fixed_base(
            lat: float, lon: float, alt: float, acc: int
        ) -> bool:
            """Write fixed base position to device RAM + flash. Returns True on success."""
            from sp_rtk_base.models.device_models import FixedBaseConfig

            try:
                await svc.configure_fixed_base(
                    FixedBaseConfig(
                        latitude=lat,
                        longitude=lon,
                        altitude_m=alt,
                        accuracy_mm=acc,
                    )
                )
                await svc.save_to_flash()
                return True
            except Exception as exc:
                ui.notify(f"Commit failed: {exc}", type="negative")
                logger.exception("Fixed base commit failed")
                return False

        # ---- Edit Mode ----

        def _enter_edit_mode() -> None:
            fb_edit_lat.value = _fb_lat
            fb_edit_lon.value = _fb_lon
            fb_edit_alt.value = _fb_alt
            fb_edit_acc.value = _fb_acc
            fb_readonly.set_visibility(False)
            fb_editmode.set_visibility(True)

        def _cancel_edit_mode() -> None:
            fb_editmode.set_visibility(False)
            fb_readonly.set_visibility(True)

        async def _commit_edit() -> None:
            lat = float(fb_edit_lat.value or 0)
            lon = float(fb_edit_lon.value or 0)
            alt = float(fb_edit_alt.value or 0)
            acc = int(fb_edit_acc.value or 1000)
            ok = await _commit_fixed_base(lat, lon, alt, acc)
            if ok:
                ui.notify("Position committed to device ✓", type="positive")
                fb_editmode.set_visibility(False)
                fb_readonly.set_visibility(True)
                await _read_fixed_base()

        # ---- Save Position ----

        def _save_position_dialog() -> None:
            from sp_rtk_base.models.config_models import BaseStationPosition

            with (
                ui.dialog() as dlg,
                ui.card().classes("q-pa-md").style("min-width: 350px"),
            ):
                ui.label("Save Position Profile").classes("text-h6 text-white")
                ui.separator()
                name_input = ui.input(
                    "Profile Name", placeholder="e.g. Office Roof"
                ).classes("w-full q-mt-sm")
                ui.label(
                    f"Lat: {_fb_lat:.7f}°  Lon: {_fb_lon:.7f}°  "
                    f"Alt: {_fb_alt:.3f}m  Acc: {_fb_acc:.0f}mm"
                ).classes("text-grey-4 q-mt-sm text-caption")

                def _do_save() -> None:
                    """Persist the named position profile.

                    Defined at the dialog scope (not nested inside a slot
                    context manager) so the closure over ``dlg`` and
                    ``name_input`` survives the synchronous handler path.
                    Any exception is surfaced to the operator instead of
                    being silently swallowed by the UI event loop.
                    """
                    try:
                        name = str(name_input.value or "").strip()
                        if not name:
                            ui.notify("Enter a profile name", type="warning")
                            return
                        config_svc.save_base_position(
                            BaseStationPosition(
                                name=name,
                                latitude=_fb_lat,
                                longitude=_fb_lon,
                                altitude_m=_fb_alt,
                                accuracy_mm=_fb_acc,
                                source="survey_in",
                            )
                        )
                        ui.notify(f"Position '{name}' saved ✓", type="positive")
                        dlg.close()
                        _refresh_saved_positions()
                    except Exception as exc:
                        logger.exception("Save position failed")
                        ui.notify(f"Save failed: {exc}", type="negative")

                with ui.row().classes("gap-2 q-mt-md justify-end"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")
                    ui.button("Save", on_click=_do_save).props("color=primary")
            dlg.open()

        # ---- Load Saved Position ----

        def _load_saved_dialog() -> None:
            positions = config_svc.get_base_positions()
            if not positions:
                ui.notify("No saved positions", type="warning")
                return

            with (
                ui.dialog() as dlg,
                ui.card().classes("q-pa-md").style("min-width: 400px"),
            ):
                ui.label("Load Saved Position").classes("text-h6 text-white")
                ui.separator()
                ui.label("Select a position to commit directly to the device.").classes(
                    "text-grey-4 q-mt-xs text-caption"
                )

                for pos in positions:
                    _pos = pos

                    async def _pick(p: object = _pos) -> None:
                        dlg.close()
                        ui.notify("Committing position to device...", type="info")
                        # `p` is typed `object` because it's used as a default
                        # argument to bind the loop var.  At runtime it's a
                        # BaseStationPosition; pyright can't see that.
                        ok = await _commit_fixed_base(
                            p.latitude,  # pyright: ignore[reportAttributeAccessIssue,reportUnknownArgumentType]
                            p.longitude,  # pyright: ignore[reportAttributeAccessIssue,reportUnknownArgumentType]
                            p.altitude_m,  # pyright: ignore[reportAttributeAccessIssue,reportUnknownArgumentType]
                            int(p.accuracy_mm),  # pyright: ignore[reportAttributeAccessIssue,reportUnknownArgumentType]
                        )
                        if ok:
                            ui.notify(
                                f"Position '{p.name}' committed ✓",  # pyright: ignore[reportAttributeAccessIssue]
                                type="positive",
                            )
                            await _read_fixed_base()

                    with (
                        ui.card()
                        .classes("w-full q-pa-sm cursor-pointer")
                        .style("background-color: #1a1a2e")
                        .on("click", _pick)
                    ):
                        ui.label(pos.name).classes("text-subtitle2 text-white")
                        ui.label(
                            f"{pos.latitude:.7f}°, {pos.longitude:.7f}°, "
                            f"{pos.altitude_m:.3f}m  (±{pos.accuracy_mm:.0f}mm)"
                        ).classes("text-caption text-grey-4")

                ui.button("Cancel", on_click=dlg.close).props("flat").classes("q-mt-md")
            dlg.open()

        # ---- Connection ----

        async def _connect() -> None:
            nonlocal pos_timer
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
                _save_device_settings()
                if pos_timer is not None:
                    pos_timer.active = False
                pos_timer = ui.timer(2.0, _poll_position)
            except Exception as exc:
                ui.notify(f"Connection failed: {exc}", type="negative")
                logger.exception("Device connect failed")
            _update_ui_state()
            if svc.is_connected:
                try:
                    await _read_fixed_base()
                except Exception:
                    logger.warning("Failed to read base config on connect")

        def _cancel_connect() -> None:
            svc.cancel_connect()
            ui.notify("Connection cancelled", type="warning")
            _update_ui_state()

        async def _disconnect() -> None:
            nonlocal svin_timer, pos_timer
            if svin_timer is not None:
                svin_timer.active = False
                svin_timer = None
            if pos_timer is not None:
                pos_timer.active = False
                pos_timer = None
            await svc.disconnect()
            svin_progress_card.set_visibility(False)
            ui.notify("Disconnected", type="info")
            _update_ui_state()

        async def _reload_device_data() -> None:
            nonlocal pos_timer
            if not svc.is_connected:
                ui.notify("Not connected", type="warning")
                return
            ui.notify("Reloading device data...", type="info")
            _update_ui_state()
            if pos_timer is None:
                pos_timer = ui.timer(2.0, _poll_position)
            try:
                await _poll_position()
            except Exception:
                pass
            try:
                await _read_fixed_base()
            except Exception:
                pass

        # ---- Survey-In ----

        async def _confirm_start_survey() -> None:
            """Show confirmation dialog before starting survey-in."""
            with ui.dialog() as dlg, ui.card().classes("q-pa-md"):
                ui.label("Start Survey-In?").classes("text-h6 text-white")
                ui.separator()
                ui.label(
                    "This will overwrite the current base position. "
                    "The survey may take several minutes depending on "
                    "your accuracy target."
                ).classes("text-grey-4 q-mt-sm")

                with ui.row().classes("gap-2 q-mt-md justify-end"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")

                    async def _confirmed() -> None:
                        dlg.close()
                        await _start_survey_in()

                    ui.button("Start Survey", on_click=_confirmed).props(
                        "color=primary"
                    )
            dlg.open()

        async def _start_survey_in() -> None:
            nonlocal svin_timer, _svin_dur_offset
            from sp_rtk_base.models.device_models import SurveyInConfig

            # Force re-snapshot of the receiver's ``dur`` counter on the
            # next poll.  Without this, a second Start press while the
            # previous offset is still cached would display negative
            # elapsed times until the receiver counter overtakes the
            # stale offset.
            _svin_dur_offset = None

            dur = int(svin_duration.value or 120)
            acc = int(svin_accuracy.value or 50000)

            # Reveal the progress card *immediately* — before the
            # configure call returns — so the operator gets feedback
            # even if the device write hangs or fails.  Previously the
            # card was hidden until after ``configure_survey_in`` had
            # both completed AND notified via toast — that path made
            # the page look frozen for the 2-5s configure round-trip
            # and lost the error entirely if the toast was dismissed.
            svin_progress_card.set_visibility(True)
            svin_error_label.set_visibility(False)
            svin_error_label.text = ""
            svin_warning_label.set_visibility(False)
            svin_warning_label.text = ""
            svin_status_label.text = "Configuring receiver..."
            svin_status_label.classes(replace="text-warning")
            svin_target_label.text = f"Target: {acc:,} mm"
            svin_dur_label.text = "Duration: 0s"
            svin_acc_label.text = "Accuracy: —"
            svin_obs_label.text = "Observations: 0"
            svin_pct_label.text = "% to target: —"
            svin_eta_label.text = "ETA: —"
            svin_progress_bar.value = 0.0
            svin_start_btn.set_visibility(False)
            svin_cancel_btn.set_visibility(True)

            _svin_chart_times.clear()
            _svin_chart_acc.clear()
            _svin_chart_obs.clear()
            _svin_eta_samples.clear()

            opts = svin_chart.options
            opts["xAxis"]["data"] = []
            opts["series"][0]["data"] = []
            opts["series"][1]["data"] = []
            opts["series"][0]["markLine"]["data"] = [{"yAxis": acc}]
            svin_chart.update()

            try:
                await svc.configure_survey_in(
                    SurveyInConfig(min_duration_seconds=dur, accuracy_limit_mm=acc)
                )
            except Exception as exc:
                # Surface the error *in the card itself* so it persists.
                # The toast is still emitted for consistency with the
                # rest of the app, but the in-card banner is the
                # primary signal — toasts fade in ~6 seconds.
                err_msg = f"Failed to configure survey-in: {exc}"
                svin_error_label.text = err_msg
                svin_error_label.set_visibility(True)
                svin_status_label.text = "⚠ Configuration failed"
                svin_status_label.classes(replace="text-negative")
                svin_cancel_btn.set_visibility(False)
                svin_start_btn.set_visibility(True)
                ui.notify(err_msg, type="negative")
                logger.exception("configure_survey_in failed")
                return

            ui.notify("Survey-in started", type="positive")
            svin_status_label.text = "Active — collecting..."
            svin_status_label.classes(replace="text-warning")

            if svin_timer is not None:
                svin_timer.active = False
            svin_timer = ui.timer(2.0, _poll_survey_in)

        async def _confirm_cancel_survey() -> None:
            """Show confirmation dialog before cancelling survey-in."""
            with ui.dialog() as dlg, ui.card().classes("q-pa-md"):
                ui.label("Cancel Survey-In?").classes("text-h6 text-white")
                ui.separator()
                ui.label(
                    "This will abort the survey and clear the receiver's "
                    "TMODE configuration.  Any progress will be lost.  "
                    "The current fixed-base position (if saved to flash) "
                    "is unaffected until power-cycle."
                ).classes("text-grey-4 q-mt-sm")

                with ui.row().classes("gap-2 q-mt-md justify-end"):
                    ui.button("Keep Surveying", on_click=dlg.close).props("flat")

                    async def _confirmed() -> None:
                        dlg.close()
                        await _cancel_survey_in()

                    ui.button("Cancel Survey", on_click=_confirmed).props(
                        "color=negative"
                    )
            dlg.open()

        async def _cancel_survey_in() -> None:
            """Stop the polling timer and send TMODE=0 to the receiver.

            On failure the Cancel button stays visible so the operator
            can retry — flipping to "Start" would imply the survey is
            no longer running when in fact the device may still be
            actively surveying.  The poll timer is restarted on
            failure so live state continues to surface while the
            operator decides what to do.
            """
            nonlocal svin_timer, _svin_dur_offset
            if svin_timer is not None:
                svin_timer.active = False
                svin_timer = None
            try:
                await svc.cancel_survey_in()
            except Exception as exc:
                err_msg = f"Cancel failed: {exc}"
                svin_error_label.text = err_msg
                svin_error_label.set_visibility(True)
                ui.notify(err_msg, type="negative")
                logger.exception("cancel_survey_in failed")
                # Keep Cancel button visible so the operator can retry,
                # and resume polling so they see live device state.
                svin_cancel_btn.set_visibility(True)
                svin_start_btn.set_visibility(False)
                svin_status_label.text = (
                    "Cancel failed — receiver may still be surveying"
                )
                svin_status_label.classes(replace="text-negative")
                svin_timer = ui.timer(2.0, _poll_survey_in)
                return

            svin_status_label.text = "Cancelled by operator"
            svin_status_label.classes(replace="text-grey-3")
            svin_warning_label.set_visibility(False)
            svin_warning_label.text = ""
            svin_progress_bar.value = 0.0
            svin_cancel_btn.set_visibility(False)
            svin_start_btn.set_visibility(True)
            # Clear the snapshot so a subsequent Start re-captures fresh.
            _svin_dur_offset = None
            ui.notify("Survey-in cancelled", type="info")

        async def _poll_survey_in() -> None:
            nonlocal svin_timer, _svin_dur_offset
            try:
                progress = await svc.get_survey_in_status()

                # Snapshot the receiver's ``dur`` counter on the first
                # poll after Start so the UI counts from 0 regardless
                # of what the receiver's accumulator reports.  We
                # used to gate this on ``progress.active`` but the
                # ZED-F9P (HPG 1.12) leaves ``active=False`` even
                # while genuinely surveying, so the gate never fired
                # and the UI displayed the raw 60000+ s accumulator.
                # The driver's ``configure_survey_in`` now guarantees
                # via CFG-RST that ``dur`` is < 30 s by the time it
                # returns, so capturing the first observed dur as the
                # offset is correct regardless of ``active``.
                is_first_poll_after_start = _svin_dur_offset is None
                if is_first_poll_after_start:
                    _svin_dur_offset = int(progress.duration_seconds)
                    logger.info(
                        "Survey-in start: captured dur offset = %ds",
                        _svin_dur_offset,
                    )

                # Effective elapsed for display = receiver_dur - offset.
                raw_dur = int(progress.duration_seconds)
                elapsed = max(0, raw_dur - (_svin_dur_offset or 0))

                if progress.valid:
                    svin_status_label.text = "✓ Complete — committing..."
                    svin_status_label.classes(replace="text-positive")

                    if svin_timer is not None:
                        svin_timer.active = False
                        svin_timer = None

                    # Auto-pipeline: promote → flash → refresh
                    await _auto_commit_survey(progress)
                    return
                elif progress.active or elapsed > 0:
                    # On ZED-F9P HPG 1.12 ``progress.active`` stays
                    # False even while the receiver is genuinely
                    # surveying, so ``elapsed > 0`` (dur has ticked
                    # past the offset we captured at Start) is the
                    # authoritative signal that the survey is making
                    # progress.
                    svin_status_label.text = "Active — collecting..."
                    svin_status_label.classes(replace="text-warning")
                elif is_first_poll_after_start:
                    svin_status_label.text = "Waiting for receiver..."
                    svin_status_label.classes(replace="text-warning")
                else:
                    svin_status_label.text = "Idle"
                    svin_status_label.classes(replace="text-grey-3")

                svin_dur_label.text = f"Duration: {elapsed}s"
                svin_acc_label.text = f"Accuracy: {progress.mean_accuracy_mm:.0f}mm"
                svin_obs_label.text = f"Observations: {progress.observations}"

                dur_target = int(svin_duration.value or 120)
                acc_target = float(svin_accuracy.value or 50000)
                cur_acc = float(progress.mean_accuracy_mm)

                # Duration-based progress bar (primary completion gate is
                # min_duration_seconds — survey only finishes once BOTH
                # the min duration AND the accuracy limit are met).
                pct_dur = min(elapsed / max(dur_target, 1), 1.0)
                svin_progress_bar.value = pct_dur

                # "% to target accuracy" — how close are we to the
                # configured limit?  Uses log-style ratio because the
                # convergence curve is roughly geometric.
                # 0% = accuracy ≥ start (large), 100% = accuracy ≤ target.
                if cur_acc <= 0:
                    pct_acc_str = "—"
                elif cur_acc <= acc_target:
                    pct_acc_str = "100% (target reached)"
                else:
                    # Geometric progress: assume the first observed
                    # accuracy is the starting point and we're heading
                    # towards ``acc_target``.  Clamp to [0, 99] before
                    # the duration gate completes the survey.
                    start_acc = _svin_chart_acc[0] if _svin_chart_acc else cur_acc
                    if start_acc <= acc_target:
                        pct_acc = 100.0
                    else:
                        import math

                        # log(start/cur) / log(start/target)
                        num = math.log(max(start_acc, 1.0) / max(cur_acc, 1.0))
                        den = math.log(max(start_acc, 1.0) / max(acc_target, 1.0))
                        pct_acc = max(
                            0.0, min(99.0, (num / den) * 100.0 if den > 0 else 0.0)
                        )
                    pct_acc_str = f"{pct_acc:.0f}%"
                svin_pct_label.text = f"% to target: {pct_acc_str}"

                # ETA: linear extrapolation from the last
                # ``_ETA_WINDOW_SECONDS`` of samples.  We need at least
                # two samples and a meaningfully-negative slope.
                _svin_eta_samples.append((elapsed, cur_acc))
                # Drop samples older than the window
                while (
                    _svin_eta_samples
                    and _svin_eta_samples[0][0] < elapsed - _ETA_WINDOW_SECONDS
                ):
                    _svin_eta_samples.pop(0)

                eta_str = "—"
                if cur_acc <= acc_target and elapsed >= dur_target:
                    eta_str = "complete"
                elif len(_svin_eta_samples) >= 2:
                    t0, a0 = _svin_eta_samples[0]
                    t1, a1 = _svin_eta_samples[-1]
                    dt = t1 - t0
                    da = a1 - a0  # negative when converging
                    if dt > 0 and da < -0.1 and cur_acc > acc_target:
                        # Linear projection: when does acc reach target?
                        slope = da / dt  # mm per second (negative)
                        seconds_to_target = int((acc_target - cur_acc) / slope)
                        # Also need min_duration to elapse
                        seconds_to_min_dur = max(0, dur_target - elapsed)
                        eta_seconds = max(seconds_to_target, seconds_to_min_dur)
                        if eta_seconds < 60:
                            eta_str = f"~{eta_seconds}s"
                        elif eta_seconds < 3600:
                            eta_str = f"~{eta_seconds // 60}m {eta_seconds % 60}s"
                        else:
                            eta_str = (
                                f"~{eta_seconds // 3600}h {(eta_seconds % 3600) // 60}m"
                            )
                    elif cur_acc <= acc_target:
                        # Accuracy already met — only waiting on min duration
                        seconds_to_min_dur = max(0, dur_target - elapsed)
                        eta_str = (
                            f"~{seconds_to_min_dur}s (duration)"
                            if seconds_to_min_dur > 0
                            else "any moment"
                        )
                svin_eta_label.text = f"ETA: {eta_str}"

                # Not-converging warning: when the survey has been
                # running 2x the configured minimum duration AND the
                # current accuracy is still more than 2x the target,
                # the receiver is almost certainly not going to
                # converge.  The most common cause is poor antenna
                # placement (indoor, under foliage, near reflective
                # surfaces) — surface the diagnostic now rather than
                # let the operator wait indefinitely.
                if elapsed > 2 * dur_target and cur_acc > 2 * acc_target:
                    svin_warning_label.text = (
                        f"⚠ Survey may not be converging — running "
                        f"{elapsed}s with accuracy {cur_acc:.0f}mm "
                        f"(target {acc_target:.0f}mm).  Check antenna "
                        "placement (sky view, multipath sources) or "
                        "cancel and try a less ambitious accuracy "
                        "target."
                    )
                    svin_warning_label.set_visibility(True)
                else:
                    svin_warning_label.set_visibility(False)

                # Update chart
                acc_val = max(progress.mean_accuracy_mm, 1.0)
                _svin_chart_times.append(str(progress.duration_seconds))
                _svin_chart_acc.append(round(acc_val, 1))
                _svin_chart_obs.append(progress.observations)

                opts = svin_chart.options
                opts["xAxis"]["data"] = _svin_chart_times
                opts["series"][0]["data"] = _svin_chart_acc
                opts["series"][1]["data"] = _svin_chart_obs

                acc_target = float(svin_accuracy.value or 50000)
                if acc_val <= acc_target:
                    line_color = "#51cf66"
                elif acc_val <= acc_target * 2:
                    line_color = "#fcc419"
                else:
                    line_color = "#ff6b6b"
                opts["series"][0]["itemStyle"]["color"] = line_color
                svin_chart.update()
            except Exception:
                pass

        async def _auto_commit_survey(progress: object) -> None:
            """Auto-promote survey result to fixed base and save to flash."""
            import httpx

            try:
                # Step 1: Promote survey-in → fixed base
                async with httpx.AsyncClient(
                    base_url="http://localhost:8080"
                ) as client:
                    resp = await client.post(
                        "/api/device/promote-survey-in", timeout=15.0
                    )
                    if resp.status_code != 200:
                        detail = resp.json().get("detail", resp.text)
                        ui.notify(f"Auto-promote failed: {detail}", type="negative")
                        svin_status_label.text = "⚠ Promote failed"
                        svin_status_label.classes(replace="text-negative")
                        svin_start_btn.set_visibility(True)
                        return

                # Step 2: Save to flash
                await svc.save_to_flash()

                # Step 3: Refresh the fixed base card
                await _read_fixed_base()

                svin_status_label.text = "✓ Survey complete — position committed!"
                svin_status_label.classes(replace="text-positive")
                svin_progress_bar.value = 1.0
                ui.notify(
                    "Survey complete — position committed to device ✓",
                    type="positive",
                )
                svin_start_btn.set_visibility(True)
            except Exception as exc:
                svin_status_label.text = f"⚠ Auto-commit error: {exc}"
                svin_status_label.classes(replace="text-negative")
                svin_start_btn.set_visibility(True)
                logger.exception("Auto-commit survey failed")

        # ---- Saved Positions List ----

        def _refresh_saved_positions() -> None:
            positions = config_svc.get_base_positions()
            saved_pos_container.clear()
            with saved_pos_container:
                if not positions:
                    ui.label("No saved positions yet").classes(
                        "text-grey-5 text-italic"
                    )
                    return
                for pos in positions:
                    with (
                        ui.card()
                        .classes("w-full q-pa-sm")
                        .style("background-color: #1a1a2e")
                    ):
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.column().classes("gap-0"):
                                ui.label(pos.name).classes("text-subtitle2 text-white")
                                ui.label(
                                    f"{pos.latitude:.7f}°, {pos.longitude:.7f}°, "
                                    f"{pos.altitude_m:.3f}m  (±{pos.accuracy_mm:.0f}mm)"
                                ).classes("text-caption text-grey-4")

                            with ui.row().classes("gap-1"):
                                _name = pos.name
                                _lat = pos.latitude
                                _lon = pos.longitude
                                _alt = pos.altitude_m
                                _acc = int(pos.accuracy_mm)

                                async def _restore(
                                    n: str = _name,
                                    la: float = _lat,
                                    lo: float = _lon,
                                    al: float = _alt,
                                    ac: int = _acc,
                                ) -> None:
                                    ui.notify(f"Restoring '{n}'...", type="info")
                                    ok = await _commit_fixed_base(la, lo, al, ac)
                                    if ok:
                                        ui.notify(f"'{n}' restored ✓", type="positive")
                                        await _read_fixed_base()

                                async def _delete(n: str = _name) -> None:
                                    if config_svc.delete_base_position(n):
                                        ui.notify(f"'{n}' deleted", type="info")
                                    _refresh_saved_positions()

                                ui.button(
                                    "Restore", icon="restore", on_click=_restore
                                ).props("dense color=positive size=sm")
                                ui.button("", icon="delete", on_click=_delete).props(
                                    "dense flat color=negative size=sm"
                                )

        # ---- Wire up events ----
        connect_btn.on_click(_connect)
        disconnect_btn.on_click(_disconnect)
        cancel_btn.on_click(lambda: _cancel_connect())
        refresh_btn.on_click(lambda: _refresh_ports())
        reload_device_btn.on_click(_reload_device_data)
        svin_start_btn.on_click(_confirm_start_survey)
        svin_cancel_btn.on_click(_confirm_cancel_survey)

        fb_edit_btn.on_click(lambda: _enter_edit_mode())
        fb_cancel_btn.on_click(lambda: _cancel_edit_mode())
        fb_commit_btn.on_click(_commit_edit)
        fb_save_btn.on_click(_save_position_dialog)
        fb_load_btn.on_click(_load_saved_dialog)

        async def _reset_receiver() -> None:
            """Hardware-reset the receiver and refresh UI state.

            Issues UBX-CFG-RST resetMode=0 via the new
            POST /api/device/reset endpoint.  Takes ~5-8 s end-to-end
            (chip reset + USB re-enumeration + reconnect).
            """
            with ui.dialog() as dlg, ui.card().classes("q-pa-md"):
                ui.label("Reset GPS Receiver?").classes("text-h6 text-white")
                ui.separator()
                ui.label(
                    "Issues a hardware reset to the GPS receiver.  This is "
                    "the only way to clear the BBR-backed survey-in "
                    "accumulator on ZED-F9P (HPG 1.12) firmware.  Saved "
                    "fixed-base coordinates in flash are preserved — only "
                    "the survey state is cleared.  Takes ~5-8 seconds."
                ).classes("text-grey-4 q-mt-sm")

                with ui.row().classes("gap-2 q-mt-md justify-end"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")

                    async def _confirmed() -> None:
                        dlg.close()
                        fb_reset_btn.set_enabled(False)
                        ui.notify(
                            "Resetting GPS (this takes ~5-8 seconds)...",
                            type="info",
                        )
                        try:
                            await svc.reset_receiver()
                            ui.notify("GPS reset complete", type="positive")
                            await _read_fixed_base()
                        except Exception as exc:
                            ui.notify(f"Reset failed: {exc}", type="negative")
                            logger.exception("reset_receiver failed")
                        finally:
                            fb_reset_btn.set_enabled(True)

                    ui.button("Reset GPS", on_click=_confirmed).props("color=warning")
            dlg.open()

        fb_reset_btn.on_click(_reset_receiver)

        # ---- Auto-load if already connected ----
        async def _on_page_load() -> None:
            nonlocal pos_timer
            if svc.is_connected:
                _update_ui_state()
                if pos_timer is None:
                    pos_timer = ui.timer(2.0, _poll_position)
                try:
                    await _read_fixed_base()
                except Exception:
                    logger.debug("Auto-load base config failed on page load")

        # ---- Initial load ----
        _refresh_ports()
        _load_saved_device_settings()
        _update_ui_state()
        _refresh_saved_positions()
        ui.timer(interval=0.1, callback=_on_page_load, once=True)


def _info_item(label: str, value: str) -> None:
    """Render a small info label/value pair."""
    with ui.column().classes("gap-0"):
        ui.label(label).classes("text-caption text-grey-5")
        ui.label(value or "—").classes("text-white")
