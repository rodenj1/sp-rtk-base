"""Advanced GPS Configuration page — RTCM, GNSS, save-to-flash, handoff.

Provides advanced GPS receiver configuration including RTCM message
output ports, GNSS constellation selection, save to flash, and
relay handoff.
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
    RTCM_MESSAGE_GROUPS,
    DeviceCapability,
    DeviceConnectionState,
    RtcmOutputPort,
    RtcmPortConfig,
)
from sp_rtk_base.services import get_config_service, get_device_service
from sp_rtk_base.services.drivers import create_driver, list_drivers
from sp_rtk_base.services.drivers.base import GpsReceiverDriver
from sp_rtk_base.ui.layout import page_layout

logger = logging.getLogger(__name__)

BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
DEFAULT_BAUD = 115200

# Ports to show in the RTCM table (SPI is excluded — rarely used)
VISIBLE_PORTS: list[RtcmOutputPort] = [
    RtcmOutputPort.USB,
    RtcmOutputPort.UART1,
    RtcmOutputPort.UART2,
    RtcmOutputPort.I2C,
]


@ui.page("/gps-config")
def gps_config_page() -> None:
    """Render the advanced GPS configuration page."""
    svc = get_device_service()
    config_svc = get_config_service()

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
        # Section B: RTCM Message Output (hidden until connected)
        # ================================================================
        rtcm_section = ui.column().classes("w-full gap-4 q-mt-md")
        rtcm_section.set_visibility(False)

        with rtcm_section:
            with ui.card().classes("w-full q-pa-md"):
                ui.label("RTCM Message Output").classes("text-h6 text-white")
                ui.separator()
                ui.label(
                    "Enable RTCM messages per output port. "
                    "Rate is messages per navigation epoch (1 = every epoch)."
                ).classes("text-grey-4 q-mt-xs text-caption")

                # Build checkbox grid: rtcm_port_checks[msg_id][port] = checkbox
                rtcm_port_checks: dict[int, dict[str, ui.checkbox]] = {}

                with ui.column().classes("q-mt-sm gap-0 w-full"):
                    # Header row
                    with (
                        ui.row()
                        .classes("items-center w-full gap-0")
                        .style("border-bottom: 1px solid #333; padding-bottom: 4px")
                    ):
                        ui.label("Group").classes("text-caption text-grey-5").style(
                            "width: 100px; flex-shrink: 0"
                        )
                        ui.label("Message").classes("text-caption text-grey-5").style(
                            "width: 180px; flex-shrink: 0"
                        )
                        for port in VISIBLE_PORTS:
                            ui.label(port.value).classes(
                                "text-caption text-grey-5 text-center"
                            ).style("width: 65px; flex-shrink: 0")

                    # Message rows grouped
                    for group_name, messages in RTCM_MESSAGE_GROUPS:
                        for idx, (msg_id, msg_desc) in enumerate(messages):
                            with (
                                ui.row()
                                .classes("items-center w-full gap-0")
                                .style("border-bottom: 1px solid #222; padding: 2px 0")
                            ):
                                # Group label (only on first row)
                                if idx == 0:
                                    ui.label(group_name).classes(
                                        "text-white text-caption"
                                    ).style("width: 100px; flex-shrink: 0")
                                else:
                                    ui.label("").style("width: 100px; flex-shrink: 0")

                                # Message ID + description
                                ui.label(f"{msg_id} {msg_desc}").classes(
                                    "text-grey-3 text-caption"
                                ).style("width: 180px; flex-shrink: 0")

                                # Per-port checkboxes
                                rtcm_port_checks[msg_id] = {}
                                for port in VISIBLE_PORTS:
                                    cb = ui.checkbox(
                                        "",
                                        value=False,
                                    ).style("width: 65px; flex-shrink: 0")
                                    rtcm_port_checks[msg_id][port.value] = cb

                # Rate selector + quick actions + buttons
                with ui.row().classes("gap-4 q-mt-md items-center flex-wrap"):
                    rtcm_rate = (
                        ui.number(
                            "Rate",
                            value=1,
                            min=1,
                            max=10,
                            step=1,
                        )
                        .classes("w-24")
                        .props("dense")
                    )

                    ui.label("|").classes("text-grey-6")

                    # Quick-select per-port buttons
                    for qp in VISIBLE_PORTS:
                        _qp_val = qp.value

                        def _toggle_port(
                            p: str = _qp_val,
                        ) -> None:
                            """Toggle all msgs for a port."""
                            any_on = any(
                                cb.value
                                for cbs in rtcm_port_checks.values()
                                for k, cb in cbs.items()
                                if k == p
                            )
                            for cbs in rtcm_port_checks.values():
                                if p in cbs:
                                    cbs[p].value = not any_on

                        ui.button(
                            qp.value,
                            on_click=_toggle_port,
                        ).props("dense flat size=sm color=grey-5")

                    def _clear_all() -> None:
                        for cbs in rtcm_port_checks.values():
                            for cb in cbs.values():
                                cb.value = False

                    ui.button(
                        "Clear All",
                        icon="clear",
                        on_click=_clear_all,
                    ).props("dense flat size=sm color=grey-5")

                with ui.row().classes("gap-4 q-mt-sm items-center"):
                    rtcm_load_btn = ui.button(
                        "Load from Device", icon="download"
                    ).props("color=info outlined")
                    rtcm_apply_btn = ui.button("Apply RTCM Config", icon="send").props(
                        "color=primary"
                    )
                # Persistent status label beside the buttons.  The
                # ``ui.notify`` toast fires on apply but fades in
                # ~5s; operators in the e2e tour reported "no
                # feedback" because they checked the page after the
                # toast had vanished.  This label keeps the last
                # apply result visible until the next attempt.
                rtcm_apply_status = ui.label("").classes("text-caption q-mt-xs")
                rtcm_apply_status.set_visibility(False)

        # ================================================================
        # Section C: GNSS Constellations (hidden until connected + capable)
        # ================================================================
        gnss_card = ui.card().classes("w-full q-pa-md q-mt-md")
        gnss_card.set_visibility(False)

        with gnss_card:
            ui.label("GNSS Constellations").classes("text-h6 text-white")
            ui.separator()
            ui.label(
                "Enable/disable satellite systems. At least one must remain enabled."
            ).classes("text-grey-4 q-mt-xs text-caption")

            _GNSS_DISPLAY: list[tuple[str, str, str]] = [
                ("gps", "GPS", "🇺🇸 USA"),
                ("glonass", "GLONASS", "🇷🇺 Russia"),
                ("galileo", "Galileo", "🇪🇺 Europe"),
                ("beidou", "BeiDou", "🇨🇳 China"),
                ("sbas", "SBAS", "Augmentation"),
                ("qzss", "QZSS", "🇯🇵 Japan"),
            ]

            gnss_toggles: dict[str, ui.switch] = {}
            with ui.column().classes("q-mt-sm gap-2"):
                for c_val, c_name, c_region in _GNSS_DISPLAY:
                    with ui.row().classes("items-center gap-2"):
                        sw = ui.switch(c_name, value=True)
                        ui.label(c_region).classes("text-caption text-grey-5")
                        gnss_toggles[c_val] = sw

            with ui.row().classes("gap-4 q-mt-sm items-center"):
                gnss_load_btn = ui.button("Load from Device", icon="download").props(
                    "color=info outlined"
                )
                gnss_apply_btn = ui.button(
                    "Apply GNSS Config", icon="satellite_alt"
                ).props("color=primary")
            gnss_apply_status = ui.label("").classes("text-caption q-mt-xs")
            gnss_apply_status.set_visibility(False)

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
            rtcm_section.set_visibility(
                connected and DeviceCapability.RTCM_MESSAGE_SELECT in caps
            )
            gnss_card.set_visibility(connected and DeviceCapability.GNSS_SELECT in caps)
            flash_card.set_visibility(
                connected and DeviceCapability.SAVE_TO_FLASH in caps
            )
            handoff_card.set_visibility(connected)
            reload_device_btn.set_visibility(connected)

            # Error display
            status = svc.get_status()
            if status.last_error:
                error_label.text = f"Error: {status.last_error}"
                error_label.set_visibility(True)
            else:
                error_label.set_visibility(False)

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

            # Auto-load configs after connect
            if svc.is_connected:
                try:
                    await _load_rtcm_config()
                except Exception:
                    logger.warning("Failed to read RTCM config on connect")
                try:
                    await _load_gnss_config()
                except Exception:
                    logger.warning("Failed to read GNSS config on connect")

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

        async def _apply_rtcm() -> None:
            """Apply multi-port RTCM message configuration."""
            rate = int(rtcm_rate.value or 1)
            messages: dict[int, dict[str, int]] = {}

            any_enabled = False
            for msg_id, port_cbs in rtcm_port_checks.items():
                port_rates: dict[str, int] = {}
                for port_name, cb in port_cbs.items():
                    val = rate if cb.value else 0
                    port_rates[port_name] = val
                    if val > 0:
                        any_enabled = True
                port_rates.setdefault("SPI", 0)
                messages[msg_id] = port_rates

            if not any_enabled:
                ui.notify(
                    "Enable at least one message on one port",
                    type="warning",
                )
                return

            try:
                await svc.configure_rtcm_ports(RtcmPortConfig(messages=messages))
                ui.notify("RTCM config applied ✓", type="positive")
                rtcm_apply_status.text = "✓ RTCM config applied"
                rtcm_apply_status.classes(replace="text-positive text-caption q-mt-xs")
                rtcm_apply_status.set_visibility(True)
            except Exception as exc:
                ui.notify(f"RTCM config failed: {exc}", type="negative")
                rtcm_apply_status.text = f"✗ RTCM config failed: {exc}"
                rtcm_apply_status.classes(replace="text-negative text-caption q-mt-xs")
                rtcm_apply_status.set_visibility(True)

        async def _load_rtcm_config() -> None:
            """Load current RTCM multi-port config from the device."""
            try:
                config = await svc.get_rtcm_port_config()

                for msg_id, port_cbs in rtcm_port_checks.items():
                    device_ports = config.messages.get(msg_id, {})
                    for port_name, cb in port_cbs.items():
                        cb.value = device_ports.get(port_name, 0) > 0

                for port_rates in config.messages.values():
                    for r in port_rates.values():
                        if r > 0:
                            rtcm_rate.value = r
                            break
                    else:
                        continue
                    break

                ui.notify("RTCM config loaded from device", type="positive")
            except Exception as exc:
                ui.notify(f"Load RTCM failed: {exc}", type="negative")

        async def _load_gnss_config() -> None:
            """Load current GNSS constellation config from the device."""
            try:
                config = await svc.get_gnss_config()
                enabled_map: dict[str, bool] = {}
                for sys_cfg in config.systems:
                    enabled_map[sys_cfg.constellation.value] = sys_cfg.enabled
                for c_val, sw in gnss_toggles.items():
                    sw.value = enabled_map.get(c_val, False)
                ui.notify("GNSS config loaded from device", type="positive")
            except Exception as exc:
                ui.notify(f"Load GNSS failed: {exc}", type="negative")

        async def _apply_gnss_config() -> None:
            """Apply GNSS constellation configuration to the device."""
            from sp_rtk_base.models.device_models import (
                GnssConfig,
                GnssConstellation,
                GnssSystemConfig,
            )

            enabled_count = sum(1 for sw in gnss_toggles.values() if sw.value)
            if enabled_count == 0:
                ui.notify("At least one constellation must be enabled", type="warning")
                return

            systems: list[GnssSystemConfig] = []
            for c_val, sw in gnss_toggles.items():
                systems.append(
                    GnssSystemConfig(
                        constellation=GnssConstellation(c_val),
                        enabled=bool(sw.value),
                    )
                )

            try:
                await svc.configure_gnss(GnssConfig(systems=systems))
                enabled = [c_val for c_val, sw in gnss_toggles.items() if sw.value]
                ui.notify(f"GNSS config applied: {enabled}", type="positive")
                gnss_apply_status.text = f"✓ GNSS config applied: {enabled}"
                gnss_apply_status.classes(replace="text-positive text-caption q-mt-xs")
                gnss_apply_status.set_visibility(True)
            except Exception as exc:
                ui.notify(f"GNSS config failed: {exc}", type="negative")
                gnss_apply_status.text = f"✗ GNSS config failed: {exc}"
                gnss_apply_status.classes(replace="text-negative text-caption q-mt-xs")
                gnss_apply_status.set_visibility(True)

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
            """Re-read all configs from the connected device."""
            if not svc.is_connected:
                ui.notify("Not connected", type="warning")
                return
            ui.notify("Reloading device config...", type="info")
            _update_ui_state()
            try:
                await _load_rtcm_config()
            except Exception:
                logger.debug("Reload RTCM config failed")
            try:
                await _load_gnss_config()
            except Exception:
                logger.debug("Reload GNSS config failed")

        # ---- Wire up event handlers ----
        connect_btn.on_click(_connect)
        disconnect_btn.on_click(_disconnect)
        cancel_btn.on_click(lambda: _cancel_connect())
        refresh_btn.on_click(lambda: _refresh_ports())
        reload_device_btn.on_click(_reload_device_config)
        rtcm_load_btn.on_click(_load_rtcm_config)
        rtcm_apply_btn.on_click(_apply_rtcm)
        gnss_load_btn.on_click(_load_gnss_config)
        gnss_apply_btn.on_click(_apply_gnss_config)
        save_flash_btn.on_click(_save_flash)
        handoff_btn.on_click(_handoff_to_relay)

        # ---- Auto-load if already connected (navigated from another page) ----
        async def _on_page_load() -> None:
            """If device is already connected, auto-load RTCM and GNSS configs."""
            if svc.is_connected:
                _update_ui_state()
                try:
                    await _load_rtcm_config()
                except Exception:
                    logger.debug("Auto-load RTCM config failed on page load")
                try:
                    await _load_gnss_config()
                except Exception:
                    logger.debug("Auto-load GNSS config failed on page load")

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
