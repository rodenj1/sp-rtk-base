"""The dashboard's full-width Network card: one column per Network link.

Lays out what ``ui/network_link_status.py`` decides.  Redrawn from the
Network link snapshot on each status refresh; it never samples.

Test ids: ``network-card``, ``network-uplink`` (the Uplink's value), ``network-freshness``,
and per link ``network-link-{name}`` with ``-state``, ``-network``,
``-band``, ``-bitrate`` (Wi-Fi), ``-speed`` (Ethernet),
``-address``, ``-traffic`` and ``-counters``.
"""

# pyright: reportUnknownMemberType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

from nicegui import ui

from sp_rtk_base.models.network_link_models import NetworkLink, NetworkLinkSnapshot
from sp_rtk_base.ui import network_link_status as shown
from sp_rtk_base.ui.components.status_card import status_metric


def _metric(label: str, value: shown.Shown, testid: str) -> None:
    status_metric(
        label,
        value.text,
        subvalue=value.sub,
        value_color=value.color,
        testid=testid,
    )


def _link_column(link: NetworkLink) -> None:
    testid = f"network-link-{link.name}"
    column = ui.column().classes("flex-1 gap-2").style("min-width: 260px")
    column.props(f"data-testid={testid}")
    with column:
        with ui.row().classes("items-center gap-2"):
            ui.icon(shown.KIND_ICON[link.kind]).classes("text-grey-4")
            ui.label(shown.heading(link)).classes("text-subtitle1 text-white")
            if link.uplink:
                ui.badge("Uplink", color="primary").props(
                    f"data-testid={testid}-uplink"
                )
        state = shown.state(link)
        with (
            ui.row().classes("items-center gap-1").props(f"data-testid={testid}-state")
        ):
            ui.label(state.text).classes(f"text-{state.color} font-bold")
            if state.sub:
                ui.label(f"— {state.sub}").classes("text-caption text-grey-6")
        with ui.grid(columns=2).classes("gap-x-6 gap-y-2"):
            for label, value, suffix in (
                ("Network", shown.wifi_network(link), "network"),
                ("Band", shown.wifi_band(link), "band"),
                ("Bitrate", shown.wifi_bitrate(link), "bitrate"),
            ):
                if value is not None:
                    _metric(label, value, f"{testid}-{suffix}")
            if (speed := shown.speed(link)) is not None:
                _metric("Speed", speed, f"{testid}-speed")
            _metric("Address", shown.address(link), f"{testid}-address")
            _metric("Traffic", shown.traffic(link), f"{testid}-traffic")
            _metric("Errors / drops", shown.counters(link), f"{testid}-counters")


def network_card(snapshot: NetworkLinkSnapshot) -> None:
    """Render the card's content for *snapshot* inside the current card."""
    with ui.row().classes("items-center justify-between w-full"):
        ui.label("Network").classes("text-h6 text-white")
        fresh = shown.freshness(snapshot)
        ui.label(fresh.text).classes(f"text-caption text-{fresh.color}").props(
            "data-testid=network-freshness"
        )
    uplink = shown.uplink(snapshot)
    with ui.row().classes("items-center gap-2"):
        ui.icon("public").classes("text-grey-5")
        ui.label("Uplink:").classes("text-grey-5")
        ui.label(uplink.text).classes(f"text-{uplink.color} font-bold").props(
            "data-testid=network-uplink"
        )
    ui.label(shown.REACHABILITY_NOTE).classes("text-caption text-grey-6")
    ui.separator()
    with ui.row().classes("w-full gap-6 items-start"):
        for link in snapshot.links:
            _link_column(link)
