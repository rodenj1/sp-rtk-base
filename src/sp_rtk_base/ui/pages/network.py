"""Network page — status, scan, join, switch, and forget (issues #22-24).

Shows the Uplink from the Network link snapshot (for a Network link
Uplink: Wi-Fi link strength, network, Wi-Fi band with its MHz, and
address as separate readings, the same as the dashboard's Network
card), a list of saved WiFi profiles with switch/forget actions, and a scan list of
nearby networks the operator can join (or a hidden SSID typed
manually). Each nearby network shows the Wi-Fi bands it's heard on and
expands to the signal on each, when the adapter has 5 GHz.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

import logging

from nicegui import ui

from sp_rtk_base.models.net_provision_models import (
    SavedWifiConnection,
    WifiNetwork,
)
from sp_rtk_base.models.network_link_models import NetworkLinkSnapshot
from sp_rtk_base.services import get_network_link_service, get_network_service
from sp_rtk_base.services.network_service import NetworkNotConfiguredError
from sp_rtk_base.ui import network_link_status as shown
from sp_rtk_base.ui import wifi_bands
from sp_rtk_base.ui.components.status_card import status_metric
from sp_rtk_base.ui.layout import page_layout
from sp_rtk_base.ui.validators import is_non_empty

logger = logging.getLogger(__name__)

# The Uplink block redraws from the Network link sampler's snapshot,
# which is in memory and refreshed every 2 s, so it runs no nmcli at all
# (in particular no Wi-Fi scan).  The saved list is a cheap
# `nmcli connection show` read.  The scan list is NOT auto-polled — a
# WiFi scan takes real radio time, so it's loaded once on page entry and
# otherwise left to the manual Rescan button.
_STATUS_POLL_INTERVAL_SECONDS = 2.0
_SAVED_POLL_INTERVAL_SECONDS = 5.0


def _signal_icon(signal: int) -> str:
    """Material icon matching a 0-100 signal strength (the Wi-Fi picker's
    scan list)."""
    if signal >= 75:
        return "signal_wifi_4_bar"
    if signal >= 50:
        return "network_wifi_3_bar"
    if signal >= 25:
        return "network_wifi_2_bar"
    return "network_wifi_1_bar"


@ui.page("/network", dark=True)
def network_page() -> None:
    """Render the network status, WiFi scan, and join dialog."""
    svc = get_network_service()
    network_link = get_network_link_service()

    with page_layout("Network"):
        ui.label("Network").classes("text-h4 text-white q-mb-md")
        status_container = ui.column().classes("w-full gap-3")

        ui.label("Saved Networks").classes("text-h5 text-white q-mt-lg")
        saved_status_label = ui.label("").classes("text-caption text-grey-6 q-mb-sm")
        saved_list = ui.column().classes("w-full gap-2")

        with ui.row().classes("items-center justify-between w-full q-mt-lg"):
            ui.label("Nearby WiFi Networks").classes("text-h5 text-white")
            with ui.row().classes("gap-2"):
                ui.button(
                    "Add Hidden Network",
                    icon="add",
                    on_click=lambda: _open_connect_dialog(None, hidden=True),
                ).props("flat color=white")
                ui.button(
                    "Rescan", icon="refresh", on_click=lambda: _refresh_scan()
                ).props("flat color=white")
        scan_status_label = ui.label("").classes("text-caption text-grey-6 q-mb-sm")
        scan_list = ui.column().classes("w-full gap-2")

        def _render_status_card(
            configured: bool, snapshot: NetworkLinkSnapshot
        ) -> None:
            uplink = shown.uplink(snapshot)
            link = snapshot.uplink_link()
            if link is not None:
                icon = shown.KIND_ICON[link.kind]
            elif snapshot.uplink is not None:
                icon = "vpn_lock"
            else:
                icon = "wifi_off"
            with ui.card().classes("w-full q-pa-md"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon(icon).classes("text-grey-4 text-3xl")
                    with ui.column().classes("gap-0"):
                        ui.label("Uplink").classes("text-caption text-grey-5")
                        ui.label(uplink.text).classes(
                            f"text-body1 text-{uplink.color} font-bold"
                        ).props("data-testid=network-page-uplink")
                if link is not None:
                    # Separate readings, as on the dashboard's Network card.
                    with ui.row().classes("q-mt-sm gap-x-8 gap-y-2"):
                        for label, value, suffix in (
                            ("Wi-Fi link", shown.wifi_strength(link), "strength"),
                            ("Network", shown.wifi_network(link), "network"),
                            ("Band", shown.wifi_band(link), "band"),
                            ("Address", shown.address(link), "address"),
                        ):
                            if value is not None:
                                status_metric(
                                    label,
                                    value.text,
                                    subvalue=value.sub,
                                    value_color=value.color,
                                    tooltip=value.tooltip,
                                    testid=f"network-page-{suffix}",
                                )
                if not configured:
                    ui.label(
                        "Network provisioning is not configured on this device."
                    ).classes("text-caption text-grey-6 q-mt-sm")

        async def _do_refresh_status() -> None:
            configured = await svc.is_configured()
            status_container.clear()
            with status_container:
                _render_status_card(configured, network_link.current())

        async def _refresh_status() -> None:
            try:
                await _do_refresh_status()
            except RuntimeError:
                return  # Elements deleted — user navigated away

        async def _session_drop_warning_text(verb: str) -> str:
            """Honest session-drop copy shared by the join/switch/forget
            dialogs (issues #23, #24) — read the AP fallback values fresh
            each time rather than hard-coding them (per #23's spec)."""
            ap_ssid = "the setup hotspot"
            fallback_minutes = "a few"
            try:
                info = await svc.get_ap_fallback_info()
                ap_ssid = info.ap_ssid
                fallback_minutes = str(max(1, round(info.fallback_window_seconds / 60)))
            except Exception:
                logger.exception("Failed to load AP fallback info for the warning")
            return (
                f"This console session will drop once you {verb}. Rejoin "
                f"on the new network to continue — if it fails, '{ap_ssid}' "
                f"will appear again in about {fallback_minutes} min."
            )

        def _render_saved_row(conn: SavedWifiConnection) -> None:
            with ui.card().classes("w-full q-pa-sm"):
                with ui.row().classes("items-center justify-between w-full"):
                    with ui.row().classes("items-center gap-3"):
                        ui.icon("wifi").classes(
                            "text-green-4 text-xl"
                            if conn.active
                            else "text-grey-5 text-xl"
                        )
                        ui.label(conn.name).classes("text-body1 text-white")
                        if conn.active:
                            ui.badge("Active", color="green")
                    with ui.row().classes("gap-2"):
                        if not conn.active:
                            ui.button(
                                "Switch",
                                on_click=lambda _e, name=conn.name: _open_switch_dialog(
                                    name
                                ),
                            ).props("flat dense color=primary")
                        ui.button(
                            "Forget",
                            on_click=lambda _e, name=conn.name, active=conn.active: (
                                _open_forget_dialog(name, active)
                            ),
                        ).props("flat dense color=negative")

        async def _open_switch_dialog(name: str) -> None:
            """Confirm switching to an already-saved network (issue #24)."""
            warning = await _session_drop_warning_text("switch")
            with (
                ui.dialog() as dialog,
                ui.card().classes("q-pa-md").style("min-width: 400px"),
            ):
                ui.label(f"Switch to '{name}'").classes("text-h6 text-white")
                ui.label(warning).classes("text-caption text-orange-4 q-mt-sm")
                with ui.row().classes("justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button(
                        "Switch",
                        on_click=lambda: _submit_switch(name, dialog),
                    ).props("color=primary")
            dialog.open()

        async def _submit_switch(name: str, dialog: ui.dialog) -> None:
            try:
                await svc.switch_to_network(name)
            except NetworkNotConfiguredError:
                ui.notify(
                    "Network provisioning is not configured on this device.",
                    type="negative",
                )
                return
            except Exception as exc:
                logger.exception("Failed to start WiFi switch attempt")
                ui.notify(f"Could not start switch attempt: {exc}", type="negative")
                return
            dialog.close()
            ui.notify(
                f"Instructed the device to switch to '{name}'. Rejoin on the "
                "new network to continue.",
                type="positive",
            )
            await _refresh_saved()

        async def _open_forget_dialog(name: str, active: bool) -> None:
            """Confirm forgetting a saved network (issue #24).

            Only the active network's forget carries the session-drop
            warning — forgetting an inactive saved profile doesn't touch
            the operator's current connection.
            """
            with (
                ui.dialog() as dialog,
                ui.card().classes("q-pa-md").style("min-width: 400px"),
            ):
                ui.label(f"Forget '{name}'?").classes("text-h6 text-white")
                if active:
                    warning = await _session_drop_warning_text("forget this network")
                    ui.label(warning).classes("text-caption text-orange-4 q-mt-sm")
                else:
                    ui.label(
                        "This saved network's credentials will be removed."
                    ).classes("text-caption text-grey-5 q-mt-sm")
                with ui.row().classes("justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button(
                        "Forget",
                        on_click=lambda: _submit_forget(name, dialog),
                    ).props("color=negative")
            dialog.open()

        async def _submit_forget(name: str, dialog: ui.dialog) -> None:
            try:
                await svc.forget_network(name)
            except NetworkNotConfiguredError:
                ui.notify(
                    "Network provisioning is not configured on this device.",
                    type="negative",
                )
                return
            except Exception as exc:
                logger.exception("Failed to start WiFi forget attempt")
                ui.notify(f"Could not start forget attempt: {exc}", type="negative")
                return
            dialog.close()
            ui.notify(f"Instructed the device to forget '{name}'.", type="positive")
            await _refresh_saved()

        async def _do_refresh_saved() -> None:
            connections: list[SavedWifiConnection] = []
            error: str | None = None
            try:
                connections = await svc.list_saved_networks()
            except NetworkNotConfiguredError:
                error = "Network provisioning is not configured on this device."
            except Exception as exc:
                logger.exception("Failed to list saved networks")
                error = f"Could not load saved networks: {exc}"

            saved_list.clear()
            if error is not None:
                saved_status_label.text = error
                return

            saved_status_label.text = ""
            with saved_list:
                if not connections:
                    ui.label("No saved WiFi networks.").classes("text-grey-6 q-pa-md")
                    return
                for conn in connections:
                    _render_saved_row(conn)

        async def _refresh_saved() -> None:
            try:
                await _do_refresh_saved()
            except RuntimeError:
                return  # Elements deleted — user navigated away

        def _render_network_row(network: WifiNetwork, has_5ghz: bool | None) -> None:
            """One nearby network: its band summary in the header, and when
            expanded the signal on each band and the Join button."""
            summary = wifi_bands.band_summary(network, has_5ghz=has_5ghz)
            row = ui.expansion().classes("w-full bg-grey-10 rounded-borders")
            row.props('data-testid="network-scan-row"')
            with row:
                with row.add_slot("header"):
                    with ui.row().classes("items-center justify-between w-full"):
                        with ui.row().classes("items-center gap-3"):
                            ui.icon(_signal_icon(network.signal)).classes(
                                "text-blue-4 text-xl"
                            )
                            ui.label(network.ssid).classes("text-body1 text-white")
                            if network.security:
                                ui.icon("lock").classes("text-grey-5 text-sm")
                        if summary is not None:
                            ui.label(summary).classes("text-caption text-grey-5").props(
                                'data-testid="network-scan-bands"'
                            )
                        else:
                            ui.label(f"{network.signal}%").classes(
                                "text-caption text-grey-5"
                            )
                with ui.row().classes("items-center justify-between w-full q-px-md"):
                    with ui.column().classes("gap-1"):
                        for heard in wifi_bands.bands_heard(network, has_5ghz=has_5ghz):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon(_signal_icon(heard.signal)).classes(
                                    "text-blue-4"
                                )
                                ui.label(heard.text).classes("text-caption text-grey-4")
                    ui.button(
                        "Join",
                        on_click=lambda _e, ssid=network.ssid: _open_connect_dialog(
                            ssid, hidden=False
                        ),
                    ).props("flat dense color=primary")

        async def _open_connect_dialog(ssid: str | None, *, hidden: bool) -> None:
            """Show the join dialog: SSID (fixed or typed), password, and
            the honest session-drop warning (issue #23)."""
            warning = await _session_drop_warning_text("connect")

            with (
                ui.dialog() as dialog,
                ui.card().classes("q-pa-md").style("min-width: 400px"),
            ):
                ui.label("Add Hidden Network" if hidden else f"Join '{ssid}'").classes(
                    "text-h6 text-white"
                )
                ssid_input = (
                    ui.input(
                        "SSID", validation={"SSID is required": is_non_empty}
                    ).classes("w-full")
                    if hidden
                    else None
                )
                password_input = ui.input(
                    "Password", password=True, password_toggle_button=True
                ).classes("w-full")
                ui.label(warning).classes("text-caption text-orange-4 q-mt-sm")
                with ui.row().classes("justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button(
                        "Connect",
                        on_click=lambda: _submit_connect(
                            ssid_input.value if ssid_input is not None else ssid,
                            password_input.value or "",
                            hidden,
                            dialog,
                        ),
                    ).props("color=primary")
            dialog.open()

        async def _submit_connect(
            ssid: str | None, password: str, hidden: bool, dialog: ui.dialog
        ) -> None:
            if not ssid or not ssid.strip():
                ui.notify("SSID is required", type="warning")
                return
            try:
                await svc.connect_to_network(ssid, password, hidden=hidden)
            except NetworkNotConfiguredError:
                ui.notify(
                    "Network provisioning is not configured on this device.",
                    type="negative",
                )
                return
            except Exception as exc:
                logger.exception("Failed to start WiFi connect attempt")
                ui.notify(f"Could not start connect attempt: {exc}", type="negative")
                return
            dialog.close()
            ui.notify(
                f"Instructed the device to join '{ssid}'. Rejoin on the new "
                "network to continue.",
                type="positive",
            )
            await _refresh_saved()

        async def _read_has_5ghz() -> bool | None:
            """Whether the adapter has 5 GHz; unknown hides the band summaries
            rather than failing the scan list."""
            try:
                return await svc.has_5ghz()
            except Exception:
                logger.exception("Could not read the Wi-Fi adapter's 5 GHz support")
                return None

        async def _do_refresh_scan() -> None:
            scan_status_label.text = "Scanning…"
            networks: list[WifiNetwork] = []
            error: str | None = None
            has_5ghz: bool | None = None
            try:
                networks = await svc.scan_networks()
                has_5ghz = await _read_has_5ghz()
            except NetworkNotConfiguredError:
                error = "Network provisioning is not configured on this device."
            except Exception as exc:
                logger.exception("WiFi scan failed")
                error = f"Scan failed: {exc}"

            scan_list.clear()
            if error is not None:
                scan_status_label.text = error
                return

            scan_status_label.text = ""
            with scan_list:
                if not networks:
                    ui.label("No WiFi networks found.").classes("text-grey-6 q-pa-md")
                    return
                for network in networks:
                    _render_network_row(network, has_5ghz)

        async def _refresh_scan() -> None:
            try:
                await _do_refresh_scan()
            except RuntimeError:
                return  # Elements deleted — user navigated away

        ui.timer(_STATUS_POLL_INTERVAL_SECONDS, _refresh_status)
        ui.timer(_SAVED_POLL_INTERVAL_SECONDS, _refresh_saved)
        ui.timer(0.1, _refresh_status, once=True)
        ui.timer(0.1, _refresh_saved, once=True)
        ui.timer(0.1, _refresh_scan, once=True)
