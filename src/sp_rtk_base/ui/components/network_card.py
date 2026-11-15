"""The dashboard's full-width Network card: one column per Network link.

Lays out what ``ui/network_link_status.py`` decides.  Built once and
updated in place from the Network link snapshot on each status refresh
(rtk_development#177); it never samples.  The link columns are re-created
only when the set of links changes (a link appears or goes), and a value
the snapshot doesn't support is hidden rather than left out.

Test ids: ``network-card``, ``network-uplink`` (the Uplink's value), ``network-freshness``,
and per link ``network-link-{name}`` with ``-state``, ``-strength``, ``-network``,
``-band``, ``-bitrate`` (Wi-Fi), ``-speed`` (Ethernet),
``-address``, ``-traffic`` and ``-counters``.
"""

# pyright: reportUnknownMemberType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

from collections.abc import Callable

from nicegui import ui

from sp_rtk_base.models.network_link_models import NetworkLink, NetworkLinkSnapshot
from sp_rtk_base.ui import network_link_status as shown
from sp_rtk_base.ui.components.status_card import (
    StatusMetric,
    set_text,
    set_visible,
    swap_class,
)

_OPTIONAL: tuple[tuple[str, Callable[[NetworkLink], shown.Shown | None], str], ...] = (
    ("Wi-Fi link", shown.wifi_strength, "strength"),
    ("Network", shown.wifi_network, "network"),
    ("Band", shown.wifi_band, "band"),
    ("Bitrate", shown.wifi_bitrate, "bitrate"),
    ("Speed", shown.speed, "speed"),
)
_ALWAYS: tuple[tuple[str, Callable[[NetworkLink], shown.Shown], str], ...] = (
    ("Address", shown.address, "address"),
    ("Traffic", shown.traffic, "traffic"),
    ("Errors / drops", shown.counters, "counters"),
)


def _metric(label: str, value: shown.Shown | None, testid: str) -> StatusMetric:
    metric = StatusMetric(
        label,
        value.text if value else "",
        subvalue=value.sub if value else None,
        value_color=value.color if value else "white",
        tooltip=value.tooltip if value else None,
        testid=testid,
        with_tooltip=True,
    )
    metric.set_visibility(value is not None)
    return metric


def _update_metric(metric: StatusMetric, value: shown.Shown | None) -> None:
    if value is not None:
        metric.update(
            value.text, value.sub, value_color=value.color, tooltip=value.tooltip
        )
    else:
        metric.update("")  # hidden, and emptied so no stale value lingers
    metric.set_visibility(value is not None)


class _LinkColumn:
    """One Network link's column."""

    def __init__(self, link: NetworkLink) -> None:
        testid = f"network-link-{link.name}"
        column = ui.column().classes("flex-1 gap-2").style("min-width: 260px")
        column.props(f"data-testid={testid}")
        state = shown.state(link)
        self._state_color = state.color
        with column:
            with ui.row().classes("items-center gap-2"):
                ui.icon(shown.KIND_ICON[link.kind]).classes("text-grey-4")
                self._heading = ui.label(shown.heading(link)).classes(
                    "text-subtitle1 text-white"
                )
                self._uplink = ui.badge("Uplink", color="primary").props(
                    f"data-testid={testid}-uplink"
                )
                self._uplink.set_visibility(link.uplink)
            with (
                ui.row()
                .classes("items-center gap-1")
                .props(f"data-testid={testid}-state")
            ):
                self._state = ui.label(state.text).classes(
                    f"text-{state.color} font-bold"
                )
                self._state_sub = ui.label(f"— {state.sub}" if state.sub else "")
                self._state_sub.classes("text-caption text-grey-6")
                self._state_sub.set_visibility(bool(state.sub))
            with ui.grid(columns=2).classes("gap-x-6 gap-y-2"):
                self._optional = [
                    (_metric(label, fn(link), f"{testid}-{suffix}"), fn)
                    for label, fn, suffix in _OPTIONAL
                ]
                self._always = [
                    (_metric(label, fn(link), f"{testid}-{suffix}"), fn)
                    for label, fn, suffix in _ALWAYS
                ]

    def update(self, link: NetworkLink) -> None:
        set_text(self._heading, shown.heading(link))
        set_visible(self._uplink, link.uplink)
        state = shown.state(link)
        set_text(self._state, state.text)
        swap_class(self._state, f"text-{self._state_color}", f"text-{state.color}")
        self._state_color = state.color
        set_text(self._state_sub, f"— {state.sub}" if state.sub else "")
        set_visible(self._state_sub, bool(state.sub))
        for metric, fn in self._optional:
            _update_metric(metric, fn(link))
        for metric, always_fn in self._always:
            _update_metric(metric, always_fn(link))


def _layout(snapshot: NetworkLinkSnapshot) -> tuple[tuple[str, str], ...]:
    """Which links the columns are for: a change re-creates them."""
    return tuple((link.name, link.kind) for link in snapshot.links)


class NetworkCard:
    """The card's content, built inside the current card."""

    def __init__(self, snapshot: NetworkLinkSnapshot) -> None:
        fresh = shown.freshness(snapshot)
        uplink = shown.uplink(snapshot)
        self._fresh_color = fresh.color
        self._uplink_color = uplink.color
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Network").classes("text-h6 text-white")
            self._fresh = (
                ui.label(fresh.text)
                .classes(f"text-caption text-{fresh.color}")
                .props("data-testid=network-freshness")
            )
        with ui.row().classes("items-center gap-2"):
            ui.icon("public").classes("text-grey-5")
            ui.label("Uplink:").classes("text-grey-5")
            self._uplink = (
                ui.label(uplink.text)
                .classes(f"text-{uplink.color} font-bold")
                .props("data-testid=network-uplink")
            )
        ui.label(shown.REACHABILITY_NOTE).classes("text-caption text-grey-6")
        ui.separator()
        self._links_row = ui.row().classes("w-full gap-6 items-start")
        self._layout: tuple[tuple[str, str], ...] = ()
        self._columns: list[_LinkColumn] = []
        self._update_links(snapshot)

    def _update_links(self, snapshot: NetworkLinkSnapshot) -> None:
        layout = _layout(snapshot)
        if layout != self._layout:
            self._links_row.clear()
            with self._links_row:
                self._columns = [_LinkColumn(link) for link in snapshot.links]
            self._layout = layout
            return
        for column, link in zip(self._columns, snapshot.links, strict=True):
            column.update(link)

    def update(self, snapshot: NetworkLinkSnapshot) -> None:
        """Show *snapshot*."""
        fresh = shown.freshness(snapshot)
        set_text(self._fresh, fresh.text)
        swap_class(self._fresh, f"text-{self._fresh_color}", f"text-{fresh.color}")
        self._fresh_color = fresh.color
        uplink = shown.uplink(snapshot)
        set_text(self._uplink, uplink.text)
        swap_class(self._uplink, f"text-{self._uplink_color}", f"text-{uplink.color}")
        self._uplink_color = uplink.color
        self._update_links(snapshot)


def network_card(snapshot: NetworkLinkSnapshot) -> NetworkCard:
    """Render the card's content for *snapshot* inside the current card."""
    return NetworkCard(snapshot)
