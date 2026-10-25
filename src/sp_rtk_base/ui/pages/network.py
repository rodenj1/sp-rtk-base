"""Network page — status, scan, join, switch, and forget (issues #22-24).

Shows the Uplink from the Network link snapshot (for a Network link
Uplink: Wi-Fi link strength, network, Wi-Fi band with its MHz, and
address as separate readings, the same as the dashboard's Network
card), a list of saved WiFi profiles with Wi-Fi band/switch/forget
actions, and a scan list of nearby networks the operator can join (or
a hidden SSID typed manually). Each nearby network shows the Wi-Fi
bands it's heard on and expands to the signal on each, when the
adapter has 5 GHz.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

import logging

from nicegui import ui

from sp_rtk_base.models.net_provision_models import (
    BandChangePending,
    BandChangeRefused,
    SavedWifiConnection,
    WifiBand,
    WifiNetwork,
)
from sp_rtk_base.models.network_link_models import NetworkLink, NetworkLinkSnapshot
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
    # The Portal proxies the console under its own path and says so in this
    # header, so its presence means the operator reaches the base through
    # the Portal rather than on the local network.
    on_portal = bool(ui.context.client.request.headers.get("X-Forwarded-Prefix"))

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
                    on_click=lambda: _open_connect_dialog(None),
                ).props("flat color=white")
                ui.button(
                    "Rescan", icon="refresh", on_click=lambda: _refresh_scan()
                ).props("flat color=white")
        scan_status_label = ui.label("").classes("text-caption text-grey-6 q-mb-sm")
        scan_list = ui.column().classes("w-full gap-2")
        # The page's last scan, which the Band dialog's hints read from.
        # Filled on page entry and by Rescan, never polled.
        last_scan: list[WifiNetwork] = []
        # The latest saved list and 5 GHz capability, from the saved-list
        # poll, so the status card's stronger-band suggestion can name the
        # active network and open its Band dialog without asking again.
        last_saved: list[SavedWifiConnection] = []
        last_has_5ghz: list[bool | None] = [None]

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
                    _render_stronger_band(link)
                if not configured:
                    ui.label(
                        "Network provisioning is not configured on this device."
                    ).classes("text-caption text-grey-6 q-mt-sm")

        def _render_stronger_band(link: NetworkLink) -> None:
            """Suggest the band the page's last scan hears the active network
            on more strongly; never scans by itself."""
            active = next((c for c in last_saved if c.active), None)
            if active is None or link.wifi is None:
                return  # Not on Wi-Fi: an Ethernet uplink has no band
            mhz = None
            if link.wifi.frequency_mhz is not None:
                mhz = link.wifi.frequency_mhz.value
            suggestion = wifi_bands.stronger_band(
                active, mhz, last_scan, has_5ghz=last_has_5ghz[0]
            )
            if suggestion is None:
                return
            with ui.row().classes("items-center gap-2 q-mt-sm no-wrap"):
                ui.icon("lightbulb").classes("text-amber-4")
                ui.label(suggestion.text).classes("text-caption text-amber-4").props(
                    'data-testid="network-page-stronger-band"'
                )
                ui.button(
                    suggestion.button, on_click=lambda: _open_band_dialog(active)
                ).props(
                    "flat dense color=amber no-caps "
                    'data-testid="network-page-stronger-band-use"'
                )

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

        async def _ap_fallback() -> tuple[str, str]:
            """The setup hotspot's SSID and fallback window in minutes, read
            fresh each time rather than hard-coded (per #23's spec)."""
            ap_ssid = "the setup hotspot"
            fallback_minutes = "a few"
            try:
                info = await svc.get_ap_fallback_info()
                ap_ssid = info.ap_ssid
                fallback_minutes = str(max(1, round(info.fallback_window_seconds / 60)))
            except Exception:
                logger.exception("Failed to load AP fallback info for the warning")
            return ap_ssid, fallback_minutes

        async def _session_drop_warning_text(verb: str) -> str:
            """Honest session-drop copy shared by the join/switch/forget
            dialogs (issues #23, #24)."""
            ap_ssid, fallback_minutes = await _ap_fallback()
            return (
                f"This console session will drop once you {verb}. Rejoin "
                f"on the new network to continue — if it fails, '{ap_ssid}' "
                f"will appear again in about {fallback_minutes} min."
            )

        def _render_saved_row(conn: SavedWifiConnection, has_5ghz: bool | None) -> None:
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
                        badge = wifi_bands.band_badge(conn, has_5ghz=has_5ghz)
                        if badge is not None:
                            ui.badge(badge, color="blue-grey").props(
                                'data-testid="network-saved-band"'
                            )
                        note = wifi_bands.reverted_note(conn)
                        if note is not None:
                            ui.icon("undo").classes("text-orange-4").props(
                                'data-testid="network-saved-band-undone"'
                            ).tooltip(note)
                    with ui.row().classes("gap-2"):
                        if wifi_bands.shows_band_control(has_5ghz=has_5ghz):
                            ui.button(
                                "Band",
                                icon="settings_input_antenna",
                                on_click=lambda _e, c=conn: _open_band_dialog(c),
                            ).props("flat dense color=white no-caps")
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

        def _open_band_dialog(conn: SavedWifiConnection) -> None:
            """Offer Auto, 2.4 GHz and 5 GHz, each with what the page's scan
            hears now; on the active network a band it doesn't hear is
            disabled. Choosing one closes the dialog and saves it, or, on the
            active network, asks to confirm the reconnect first."""
            with (
                ui.dialog() as dialog,
                ui.card().classes("q-pa-md").style("min-width: 400px"),
            ):
                ui.label(f"Wi-Fi band for '{conn.ssid}'").classes("text-h6 text-white")
                note = wifi_bands.reverted_note(conn)
                if note is not None:
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.icon("undo").classes("text-orange-4")
                        ui.label(note).classes("text-caption text-orange-4").props(
                            'data-testid="network-band-dialog-undone"'
                        )
                for option in wifi_bands.band_options(conn, last_scan):
                    current = option.band is conn.band
                    with ui.row().classes("items-center gap-3 w-full no-wrap"):
                        choice = ui.button(
                            option.label,
                            icon="radio_button_checked"
                            if current
                            else "radio_button_unchecked",
                            on_click=lambda _e, band=option.band: _choose_band(
                                conn, band, dialog
                            ),
                        ).props("flat dense color=white no-caps")
                        if option.disabled:
                            choice.disable()
                        ui.label(option.hint).classes(
                            "text-caption "
                            + ("text-grey-7" if option.disabled else "text-grey-5")
                        )
                if conn.active:
                    ui.label(
                        "Changing the band of the network the base is on reconnects it."
                    ).classes("text-caption text-grey-6 q-mt-sm")
                with ui.row().classes("justify-end w-full q-mt-md"):
                    ui.button("Close", on_click=dialog.close).props("flat")
            dialog.open()

        async def _choose_band(
            conn: SavedWifiConnection, band: WifiBand, dialog: ui.dialog
        ) -> None:
            dialog.close()
            if band is conn.band:
                return
            if conn.active:
                await _open_band_confirm_dialog(conn, band)
                return
            await _submit_band(conn, band)

        async def _open_band_confirm_dialog(
            conn: SavedWifiConnection, band: WifiBand
        ) -> None:
            """Warn that the reconnect drops the session before going on."""
            ap_ssid, fallback_minutes = await _ap_fallback()
            warning = wifi_bands.band_change_warning(
                conn.ssid,
                band,
                on_portal=on_portal,
                ap_ssid=ap_ssid,
                fallback_minutes=fallback_minutes,
            )
            with (
                ui.dialog() as dialog,
                ui.card().classes("q-pa-md").style("max-width: 440px"),
            ):
                ui.label(f"Use {band.label} for '{conn.ssid}'?").classes(
                    "text-h6 text-white"
                )
                ui.label(warning).classes("text-caption text-orange-4 q-mt-sm")
                with ui.row().classes("justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button(
                        "Change band",
                        on_click=lambda: _submit_band(conn, band, dialog),
                    ).props("color=primary")
            dialog.open()

        async def _submit_band(
            conn: SavedWifiConnection,
            band: WifiBand,
            dialog: ui.dialog | None = None,
        ) -> None:
            try:
                reply = await svc.change_band(conn.name, band)
            except NetworkNotConfiguredError:
                ui.notify(
                    "Network provisioning is not configured on this device.",
                    type="negative",
                )
                return
            except Exception as exc:
                logger.exception("Failed to change a saved network's Wi-Fi band")
                ui.notify(f"Could not change the band: {exc}", type="negative")
                return
            finally:
                if dialog is not None:
                    dialog.close()
            if isinstance(reply, BandChangeRefused):
                ui.notify(reply.reason, type="warning", multi_line=True)
                return
            if isinstance(reply, BandChangePending):
                ui.notify(
                    f"Instructed the base to rejoin '{conn.ssid}' on {band.label}.",
                    type="positive",
                )
            else:
                ui.notify(
                    f"Saved: '{conn.ssid}' will use {band.label}.", type="positive"
                )
            await _refresh_saved()

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
            has_5ghz: bool | None = None
            try:
                connections = await svc.list_saved_networks()
                has_5ghz = await _read_has_5ghz()
            except NetworkNotConfiguredError:
                error = "Network provisioning is not configured on this device."
            except Exception as exc:
                logger.exception("Failed to list saved networks")
                error = f"Could not load saved networks: {exc}"

            last_saved[:] = connections
            last_has_5ghz[0] = has_5ghz
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
                    _render_saved_row(conn, has_5ghz)

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
                        on_click=lambda _e, network=network: _open_connect_dialog(
                            network
                        ),
                    ).props("flat dense color=primary")

        async def _open_connect_dialog(network: WifiNetwork | None) -> None:
            """Show the join dialog for a scanned ``network``, or for a
            hidden one when ``None``: SSID (fixed or typed), password, the
            Wi-Fi band select (issue #153) and the honest session-drop
            warning (issue #23)."""
            hidden = network is None
            ssid = network.ssid if network is not None else None
            warning = await _session_drop_warning_text("connect")
            band_choices = wifi_bands.join_band_choices(
                network, has_5ghz=await _read_has_5ghz()
            )

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
                band_select = (
                    ui.select(band_choices, value=WifiBand.AUTO, label="Wi-Fi band")
                    .classes("w-full")
                    .props('data-testid="network-join-band"')
                    if band_choices
                    else None
                )
                ui.label(warning).classes("text-caption text-orange-4 q-mt-sm")
                with ui.row().classes("justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button(
                        "Connect",
                        on_click=lambda: _submit_connect(
                            ssid_input.value if ssid_input is not None else ssid,
                            password_input.value or "",
                            hidden,
                            band_select.value
                            if band_select is not None
                            else WifiBand.AUTO,
                            dialog,
                        ),
                    ).props("color=primary")
            dialog.open()

        async def _submit_connect(
            ssid: str | None,
            password: str,
            hidden: bool,
            band: WifiBand,
            dialog: ui.dialog,
        ) -> None:
            if not ssid or not ssid.strip():
                ui.notify("SSID is required", type="warning")
                return
            try:
                await svc.connect_to_network(ssid, password, hidden=hidden, band=band)
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

            last_scan[:] = networks
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
