# pyright: reportUnknownMemberType=false
"""PROTOTYPE, throwaway: Network links on the dashboard (rtk_development#127).

Three variants of where and how Network links appear on the dashboard,
switchable via ``?variant=A|B|C`` on the real ``/`` route, fed by fake
readings picked with ``?scenario=``.  Nothing here reads the host: every
value comes from SCENARIOS.  Never merge this module.

- A "Network card": a full-width card under the metric row, one column
  per Network link with every detail.
- B "Status strip": one line in the dashboard naming the Uplink and its
  health, expanding to a per-link table; detail is the Network page's job.
- C "Fourth metric card": a Network card in the metric row beside Input
  Source / Throughput / Destinations, Uplink summary only, other links as
  one-liners.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from nicegui import ui

VARIANTS = {"A": "Network card", "B": "Status strip", "C": "Fourth metric card"}

State = Literal["up", "Disconnected", "No reading"]
Level = Literal["Good", "Marginal", "Poor"]
LEVEL_COLOR = {"Good": "positive", "Marginal": "warning", "Poor": "negative"}
REACHABILITY_NOTE = (
    "Link health only: whether the caster and Portal are reachable shows "
    "under Destinations."
)


@dataclass
class Link:
    name: str
    kind: Literal["ethernet", "wifi"]
    state: State
    reason: str = ""
    ipv4: str | None = None
    ipv6: str | None = None
    # Ethernet
    speed_mbps: int | None = None
    full_duplex: bool | None = None
    # Wi-Fi
    level: Level | None = None
    dbm: int | None = None
    ssid: str | None = None
    bssid: str | None = None
    freq_mhz: int | None = None
    tx_mbps: float | None = None
    rx_mbps: float | None = None
    tx_failed: int | None = None
    wifi_reason: str = ""  # Wi-Fi fields No reading while counters are current
    # Shared
    rx_bps: float = 0.0
    tx_bps: float = 0.0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_drops: int = 0
    tx_drops: int = 0
    link_changes: int = 0
    age_s: float = 1.0


@dataclass
class Scenario:
    title: str
    links: list[Link]
    uplink: str | None  # interface name, or None for no Uplink
    uplink_other: bool = False  # Uplink isn't a Network link (VPN)
    sampler_age_s: float = 1.0


def _wifi(**kw: object) -> Link:
    base: dict[str, object] = dict(
        name="wlan0",
        kind="wifi",
        state="up",
        ipv4="192.168.0.120/24",
        ipv6="fe80::ba27:ebff:feec:891c/64",
        level="Good",
        dbm=-54,
        ssid="Cats",
        bssid="f8:bb:bf:02:d9:47",
        freq_mhz=5745,
        tx_mbps=32.5,
        rx_mbps=43.3,
        tx_failed=145,
        rx_bps=4_100,
        tx_bps=2_300,
        rx_drops=319_437,
        tx_drops=40,
        tx_errors=1,
        link_changes=0,
    )
    base.update(kw)
    return Link(**base)  # type: ignore[arg-type]


def _eth(**kw: object) -> Link:
    base: dict[str, object] = dict(
        name="eth0",
        kind="ethernet",
        state="up",
        ipv4="192.168.0.121/24",
        speed_mbps=100,
        full_duplex=True,
        rx_bps=3_900,
        tx_bps=2_200,
    )
    base.update(kw)
    return Link(**base)  # type: ignore[arg-type]


_ETH_UNPLUGGED = dict(
    state="Disconnected",
    reason="No cable.",
    ipv4=None,
    speed_mbps=None,
    full_duplex=None,
    rx_bps=0,
    tx_bps=0,
)

SCENARIOS: dict[str, Scenario] = {
    "wifi_only": Scenario(
        "Wi-Fi only (cable out)",
        [_wifi(), _eth(**_ETH_UNPLUGGED)],
        uplink="wlan0",
    ),
    "both": Scenario(
        "Both up, Ethernet is the Uplink",
        [_eth(link_changes=2), _wifi(rx_bps=120, tx_bps=80)],
        uplink="eth0",
    ),
    "eth_only": Scenario(
        "Ethernet only (no Wi-Fi configured)",
        [
            _eth(),
            _wifi(
                state="Disconnected",
                reason="Wi-Fi is disconnected.",
                level=None,
                dbm=None,
                ssid=None,
                bssid=None,
                freq_mhz=None,
                tx_mbps=None,
                rx_mbps=None,
                tx_failed=None,
                ipv4=None,
                ipv6=None,
                rx_bps=0,
                tx_bps=0,
            ),
        ],
        uplink="eth0",
    ),
    "weak_wifi": Scenario(
        "Weak Wi-Fi, recent drops",
        [
            _wifi(
                level="Poor",
                dbm=-81,
                tx_mbps=6.5,
                rx_mbps=6.0,
                tx_failed=1_212,
                link_changes=7,
            ),
            _eth(**_ETH_UNPLUGGED),
        ],
        uplink="wlan0",
    ),
    "no_uplink": Scenario(
        "Wi-Fi associated, no Uplink (no default route)",
        [_wifi(ipv4=None, level="Marginal", dbm=-71), _eth(**_ETH_UNPLUGGED)],
        uplink=None,
    ),
    "vpn": Scenario(
        "Uplink is a VPN (wg0)",
        [_wifi(), _eth(**_ETH_UNPLUGGED)],
        uplink="wg0",
        uplink_other=True,
    ),
    "setup_ap": Scenario(
        "Setup access point on",
        [
            _wifi(
                state="Disconnected",
                reason="The setup access point is on.",
                level=None,
                dbm=None,
                ssid=None,
                bssid=None,
                freq_mhz=None,
                tx_mbps=None,
                rx_mbps=None,
                tx_failed=None,
                ipv4="10.42.0.1/24",
                ipv6=None,
            ),
            _eth(**_ETH_UNPLUGGED),
        ],
        uplink=None,
    ),
    "wifi_no_reading": Scenario(
        "Wi-Fi readings failing (nl80211), counters fine",
        [
            _wifi(
                level=None,
                dbm=None,
                freq_mhz=None,
                tx_mbps=None,
                rx_mbps=None,
                tx_failed=None,
                wifi_reason="The Wi-Fi adapter gave no reading.",
            ),
            _eth(**_ETH_UNPLUGGED),
        ],
        uplink="wlan0",
    ),
    "stale": Scenario(
        "Sampler stuck: everything No reading for 45 s",
        [
            _wifi(state="No reading", reason="No reading for 45 s.", age_s=45),
            _eth(state="No reading", reason="No reading for 45 s.", age_s=45),
        ],
        uplink=None,
        sampler_age_s=45,
    ),
}


# ── Formatting ────────────────────────────────────────────────────────


def _rate(bps: float) -> str:
    for unit, div in (("MB/s", 1e6), ("kB/s", 1e3)):
        if bps >= div:
            return f"{bps / div:.1f} {unit}"
    return f"{bps:.0f} B/s"


def _kind(link: Link) -> str:
    return "Wi-Fi" if link.kind == "wifi" else "Ethernet"


def _icon(link: Link) -> str:
    return "wifi" if link.kind == "wifi" else "settings_ethernet"


def _band(freq: int | None) -> str:
    if freq is None:
        return "—"
    band = "5 GHz" if freq > 4000 else "2.4 GHz"
    return f"{band} · {freq} MHz"


def _state_text(link: Link) -> tuple[str, str]:
    if link.state == "up":
        return ("Connected", "positive")
    if link.state == "Disconnected":
        return ("Disconnected", "grey-5")
    return ("No reading", "warning")


def _strength(link: Link) -> tuple[str, str, str | None]:
    """(value, colour, subvalue) for the Wi-Fi link row."""
    if link.state != "up" or link.level is None:
        return ("—", "grey-5", None)
    return (link.level, LEVEL_COLOR[link.level], f"{link.dbm} dBm")


def _uplink_text(sc: Scenario) -> tuple[str, str]:
    if sc.sampler_age_s > 6:
        return ("No reading", "warning")
    if sc.uplink is None:
        return ("No Uplink", "negative")
    if sc.uplink_other:
        return (f"{sc.uplink} (not a Network link)", "white")
    link = next(lk for lk in sc.links if lk.name == sc.uplink)
    return (f"{_kind(link)} ({link.name})", "white")


def _ordered(sc: Scenario) -> list[Link]:
    return sorted(
        sc.links, key=lambda lk: (lk.name != sc.uplink, lk.kind != "ethernet", lk.name)
    )


def _freshness(sc: Scenario) -> tuple[str, str]:
    if sc.sampler_age_s > 6:
        return (f"No reading for {sc.sampler_age_s:.0f} s", "warning")
    return (f"Updated {sc.sampler_age_s:.0f} s ago", "grey-6")


def _metric(
    label: str,
    value: str,
    color: str = "white",
    sub: str | None = None,
    tip: str | None = None,
) -> None:
    with ui.column().classes("gap-0"):
        ui.label(label).classes("text-caption text-grey-5")
        ui.label(value).classes(f"text-body1 text-{color} font-bold")
        if sub:
            ui.label(sub).classes("text-caption text-grey-6")
        if tip:
            ui.tooltip(tip)


# ── Variant A: full-width Network card, a column per link ─────────────


def variant_a(sc: Scenario) -> None:
    with ui.card().classes("w-full q-pa-md q-mt-md"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Network").classes("text-h6 text-white")
            text, color = _freshness(sc)
            ui.label(text).classes(f"text-caption text-{color}")
        up_text, up_color = _uplink_text(sc)
        with ui.row().classes("items-center gap-2"):
            ui.icon("public").classes("text-grey-5")
            ui.label("Uplink:").classes("text-grey-5")
            ui.label(up_text).classes(f"text-{up_color} font-bold")
        ui.label(REACHABILITY_NOTE).classes("text-caption text-grey-6")
        ui.separator()
        with ui.row().classes("w-full gap-6 items-start"):
            for link in _ordered(sc):
                with ui.column().classes("flex-1 gap-2").style("min-width: 260px"):
                    st, st_color = _state_text(link)
                    with ui.row().classes("items-center gap-2"):
                        ui.icon(_icon(link)).classes("text-grey-4")
                        ui.label(f"{_kind(link)} · {link.name}").classes(
                            "text-subtitle1 text-white"
                        )
                        if link.name == sc.uplink:
                            ui.badge("Uplink", color="primary")
                    with ui.row().classes("items-center gap-1"):
                        ui.label(st).classes(f"text-{st_color} font-bold")
                        if link.reason:
                            ui.label(f"— {link.reason}").classes(
                                "text-caption text-grey-6"
                            )
                    if link.state == "No reading":
                        # Never an old value as current: only counter totals survive.
                        link = Link(
                            name=link.name,
                            kind=link.kind,
                            state=link.state,
                            reason=link.reason,
                            rx_errors=link.rx_errors,
                            tx_errors=link.tx_errors,
                            rx_drops=link.rx_drops,
                            tx_drops=link.tx_drops,
                            link_changes=link.link_changes,
                        )
                    with ui.grid(columns=2).classes("gap-x-6 gap-y-2"):
                        if link.kind == "wifi":
                            v, c, s = _strength(link)
                            _metric(
                                "Wi-Fi link",
                                v,
                                c,
                                s,
                                link.wifi_reason or (link.reason if v == "—" else None),
                            )
                            _metric("Network", link.ssid or "—", sub=link.bssid)
                            _metric(
                                "Band",
                                _band(link.freq_mhz if link.state == "up" else None),
                            )
                            _metric(
                                "Bitrate",
                                "—"
                                if link.tx_mbps is None
                                else f"↑{link.tx_mbps:g} ↓{link.rx_mbps:g} Mbit/s",
                            )
                        else:
                            _metric(
                                "Speed",
                                "—"
                                if link.speed_mbps is None
                                else f"{link.speed_mbps} Mbit/s",
                                sub=None
                                if link.full_duplex is None
                                else (
                                    "Full duplex" if link.full_duplex else "Half duplex"
                                ),
                            )
                        _metric("Address", link.ipv4 or "—", sub=link.ipv6)
                        _metric(
                            "Traffic",
                            "—"
                            if link.state == "No reading"
                            else f"↓{_rate(link.rx_bps)} ↑{_rate(link.tx_bps)}",
                        )
                        _metric(
                            "Errors / drops",
                            f"{link.rx_errors + link.tx_errors} / {link.rx_drops + link.tx_drops:,}",
                            sub=f"{link.link_changes} link changes"
                            + (
                                f" · {link.tx_failed:,} failed sends"
                                if link.tx_failed is not None
                                else ""
                            ),
                        )


# ── Variant B: one status strip, expanding to a table ─────────────────


def variant_b(sc: Scenario) -> None:
    up_text, up_color = _uplink_text(sc)
    uplink = next((lk for lk in sc.links if lk.name == sc.uplink), None)
    with ui.card().classes("w-full q-pa-sm q-mt-md"):
        with ui.row().classes("items-center gap-4 w-full no-wrap"):
            ui.icon(
                "lan" if uplink is None or uplink.kind == "ethernet" else "wifi"
            ).classes("text-grey-4 text-h6")
            ui.label("Uplink").classes("text-grey-5")
            ui.label(up_text).classes(f"text-{up_color} font-bold")
            if uplink is not None and uplink.kind == "wifi":
                v, c, s = _strength(uplink)
                ui.label(f"{v}" + (f" · {s}" if s else "")).classes(
                    f"text-{c} font-bold"
                )
            elif uplink is not None and uplink.speed_mbps:
                ui.label(f"{uplink.speed_mbps} Mbit/s").classes("text-white")
            if uplink is not None:
                ui.label(f"↓{_rate(uplink.rx_bps)} ↑{_rate(uplink.tx_bps)}").classes(
                    "text-grey-4"
                )
            ui.space()
            text, color = _freshness(sc)
            ui.label(text).classes(f"text-caption text-{color}")
        with ui.expansion("All Network links").classes("w-full text-grey-4"):
            rows = []
            for link in _ordered(sc):
                st, _ = _state_text(link)
                v, _, s = _strength(link) if link.kind == "wifi" else ("", "", None)
                rows.append(
                    {
                        "link": f"{_kind(link)} {link.name}"
                        + (" (Uplink)" if link.name == sc.uplink else ""),
                        "state": st + (f": {link.reason}" if link.reason else ""),
                        "detail": (
                            f"{v} {s or ''} {link.ssid or ''}".strip()
                            if link.kind == "wifi"
                            else (
                                f"{link.speed_mbps} Mbit/s "
                                f"{'full' if link.full_duplex else 'half'} duplex"
                                if link.speed_mbps
                                else "—"
                            )
                        ),
                        "addr": link.ipv4 or "—",
                        "traffic": f"↓{_rate(link.rx_bps)} ↑{_rate(link.tx_bps)}",
                        "errs": f"{link.rx_errors + link.tx_errors} err · "
                        f"{link.rx_drops + link.tx_drops:,} drop · {link.link_changes} chg",
                    }
                )
            ui.table(
                columns=[
                    {"name": k, "label": lbl, "field": k, "align": "left"}
                    for k, lbl in (
                        ("link", "Link"),
                        ("state", "State"),
                        ("detail", "Detail"),
                        ("addr", "Address"),
                        ("traffic", "Traffic"),
                        ("errs", "Errors"),
                    )
                ],
                rows=rows,
            ).props("flat dense dark").classes("w-full")
            ui.label(
                REACHABILITY_NOTE + " Full detail is on the Network page."
            ).classes("text-caption text-grey-6")


# ── Variant C: fourth metric card in the metric row ──────────────────


def variant_c_card(sc: Scenario) -> None:
    """Rendered inside a card that sits in the metric row."""
    ui.label("Network").classes("text-subtitle2 text-grey-4")
    up_text, up_color = _uplink_text(sc)
    uplink = next((lk for lk in sc.links if lk.name == sc.uplink), None)
    ui.label(up_text if sc.uplink else "No Uplink").classes(f"text-{up_color}")
    if uplink is not None and uplink.kind == "wifi":
        v, c, s = _strength(uplink)
        _metric("Wi-Fi link", v, c, s, uplink.wifi_reason or None)
    elif uplink is not None and uplink.kind == "ethernet":
        _metric(
            "Ethernet",
            f"{uplink.speed_mbps} Mbit/s",
            sub="Full duplex" if uplink.full_duplex else "Half duplex",
        )
    if uplink is not None:
        _metric(
            "Traffic",
            f"↓{_rate(uplink.rx_bps)} ↑{_rate(uplink.tx_bps)}",
            sub=f"{uplink.link_changes} link changes",
        )
    others = [lk for lk in _ordered(sc) if lk.name != sc.uplink]
    if others:
        ui.separator().classes("q-my-xs")
        for link in others:
            st, st_color = _state_text(link)
            extra = ""
            if link.kind == "wifi" and link.state == "up" and link.level:
                extra = f" · {link.level} {link.dbm} dBm"
            with ui.row().classes("items-center gap-1"):
                ui.icon(_icon(link)).classes("text-grey-6 text-sm")
                ui.label(f"{link.name}: ").classes("text-caption text-grey-5")
                ui.label(st + extra).classes(f"text-caption text-{st_color}")
                if link.reason:
                    ui.tooltip(link.reason)
    text, color = _freshness(sc)
    ui.label(text).classes(f"text-caption text-{color} q-mt-sm")
    ui.link("Details on the Network page →", "/network").classes("text-caption")


# ── Switcher bar ──────────────────────────────────────────────────────


def enabled() -> bool:
    return os.environ.get("SP_RTK_BASE_PROTOTYPE") == "1"


def resolve(variant: str | None, scenario: str | None) -> tuple[str, str]:
    v = (variant or "A").upper()
    return (
        v if v in VARIANTS else "A",
        scenario if scenario in SCENARIOS else "wifi_only",
    )


def switcher(variant: str, scenario: str) -> None:
    keys = list(VARIANTS)
    scen = list(SCENARIOS)

    def go(v: str, s: str) -> None:
        ui.navigate.to(f"/?variant={v}&scenario={s}")

    def step(d: int) -> None:
        go(keys[(keys.index(variant) + d) % len(keys)], scenario)

    with (
        ui.element("div").style(
            "position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);"
            "z-index: 9999; background: #fff; color: #000; border-radius: 999px;"
            "box-shadow: 0 4px 16px rgba(0,0,0,.5); padding: 4px 12px;"
        ),
        ui.row().classes("items-center gap-2 no-wrap"),
    ):
        ui.button(icon="chevron_left", on_click=lambda: step(-1)).props(
            "flat round dense color=black"
        )
        ui.label(f"PROTOTYPE {variant} ({VARIANTS[variant]})").classes(
            "text-weight-bold"
        )
        ui.button(icon="chevron_right", on_click=lambda: step(1)).props(
            "flat round dense color=black"
        )
        ui.select(
            {k: SCENARIOS[k].title for k in scen},
            value=scenario,
            on_change=lambda e: go(variant, e.value),
        ).props(
            'dense options-dense outlined :dark="false" color=black label-color=black'
        ).classes("text-black").style("min-width: 300px")

    def on_key(e: object) -> None:
        key = getattr(e, "key", None)
        action = getattr(e, "action", None)
        if action is not None and getattr(action, "keydown", False):
            if str(key) == "ArrowLeft":
                step(-1)
            elif str(key) == "ArrowRight":
                step(1)

    ui.keyboard(on_key=on_key, ignore=["input", "select", "button", "textarea"])
