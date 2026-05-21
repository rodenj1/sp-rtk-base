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

            svin_start_btn = (
                ui.button("Start Survey-In", icon="play_arrow")
                .props("color=primary")
                .classes("q-mt-sm")
            )

            # Progress section (hidden until survey starts)
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
                svin_progress_bar = ui.linear_progress(
                    value=0, show_value=False
                ).classes("q-mt-xs")

                with ui.row().classes("w-full gap-4 q-mt-xs sp-metric-row"):
                    svin_dur_label = ui.label("Duration: 0s").classes("text-grey-3")
                    svin_acc_label = ui.label("Accuracy: 0mm").classes("text-grey-3")
                    svin_obs_label = ui.label("Observations: 0").classes("text-grey-3")

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

        async def _save_position_dialog() -> None:
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

                with ui.row().classes("gap-2 q-mt-md justify-end"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")

                    async def _do_save() -> None:
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

                    ui.button("Save", on_click=_do_save).props("color=primary")
            dlg.open()

        # ---- Load Saved Position ----

        async def _load_saved_dialog() -> None:
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
            nonlocal svin_timer
            from sp_rtk_base.models.device_models import SurveyInConfig

            dur = int(svin_duration.value or 120)
            acc = int(svin_accuracy.value or 50000)
            try:
                await svc.configure_survey_in(
                    SurveyInConfig(min_duration_seconds=dur, accuracy_limit_mm=acc)
                )
                ui.notify("Survey-in started", type="positive")
                svin_progress_card.set_visibility(True)
                svin_start_btn.set_visibility(False)

                _svin_chart_times.clear()
                _svin_chart_acc.clear()
                _svin_chart_obs.clear()

                svin_target_label.text = f"Target: {acc:,} mm"
                opts = svin_chart.options
                opts["xAxis"]["data"] = []
                opts["series"][0]["data"] = []
                opts["series"][1]["data"] = []
                opts["series"][0]["markLine"]["data"] = [{"yAxis": acc}]
                svin_chart.update()

                if svin_timer is not None:
                    svin_timer.active = False
                svin_timer = ui.timer(2.0, _poll_survey_in)
            except Exception as exc:
                ui.notify(f"Survey-in failed: {exc}", type="negative")

        async def _poll_survey_in() -> None:
            nonlocal svin_timer
            try:
                progress = await svc.get_survey_in_status()
                if progress.active:
                    svin_status_label.text = "Active — collecting..."
                    svin_status_label.classes(replace="text-warning")
                elif progress.valid:
                    svin_status_label.text = "✓ Complete — committing..."
                    svin_status_label.classes(replace="text-positive")

                    if svin_timer is not None:
                        svin_timer.active = False
                        svin_timer = None

                    # Auto-pipeline: promote → flash → refresh
                    await _auto_commit_survey(progress)
                    return
                else:
                    svin_status_label.text = "Idle"
                    svin_status_label.classes(replace="text-grey-3")

                svin_dur_label.text = f"Duration: {progress.duration_seconds}s"
                svin_acc_label.text = f"Accuracy: {progress.mean_accuracy_mm:.0f}mm"
                svin_obs_label.text = f"Observations: {progress.observations}"

                dur_target = int(svin_duration.value or 120)
                pct = min(progress.duration_seconds / max(dur_target, 1), 1.0)
                svin_progress_bar.value = pct

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
        fb_edit_btn.on_click(lambda: _enter_edit_mode())
        fb_cancel_btn.on_click(lambda: _cancel_edit_mode())
        fb_commit_btn.on_click(_commit_edit)
        fb_save_btn.on_click(_save_position_dialog)
        fb_load_btn.on_click(_load_saved_dialog)

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
