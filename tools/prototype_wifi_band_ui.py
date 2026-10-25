#!/usr/bin/env python3
# pyright: basic
"""PROTOTYPE, throw me away: Wi-Fi band control and readout on the Network page.

Answers rtk_development#145 ("How the Wi-Fi band control and band readout
look in the console"). Three structurally different variants of the
Network page, switchable with ``?variant=A|B|C`` or the floating bar
(arrow keys work too). All data is fake and in memory; nothing touches
nmcli.

    uv run python tools/prototype_wifi_band_ui.py      # http://localhost:8090

Scenario toggles in the bar: adapter without 5 GHz, operator on the
Portal, and a pending "band change was reverted" note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode

from nicegui import ui

BANDS = ["Auto", "2.4 GHz", "5 GHz"]
VARIANTS = {"A": "Inline toggle", "B": "Band dialog + hints", "C": "Table, scan by band"}
FALLBACK_MIN = 5
AP_SSID = "SP-Base-Setup"


@dataclass
class Saved:
    name: str
    band: str
    active: bool = False
    reverted: str | None = None  # the band that was reverted from


@dataclass
class Heard:
    ssid: str
    secure: bool
    bands: dict[str, int] = field(default_factory=dict)  # band -> signal %


SAVED = [
    Saved("FarmShop", "Auto", active=True),
    Saved("Barn-AP", "2.4 GHz"),
    Saved("Home-Mesh", "Auto"),
]
SCAN = [
    Heard("FarmShop", True, {"2.4 GHz": 82, "5 GHz": 41}),
    Heard("Neighbour-5G", True, {"5 GHz": 55}),
    Heard("Home-Mesh", True, {"2.4 GHz": 35, "5 GHz": 22}),
    Heard("Barn-Guest", False, {"2.4 GHz": 30}),
]
# The live link: FarmShop, associated on 5 GHz with Marginal strength.
LINK = {"network": "FarmShop", "dbm": -74, "level": "Marginal", "mhz": 5180}


def heard_on(ssid: str) -> dict[str, int]:
    return next((h.bands for h in SCAN if h.ssid == ssid), {})


def band_of(mhz: int) -> str:
    return "2.4 GHz" if mhz < 3000 else "5 GHz"


# ---------------------------------------------------------------------------
# Shared copy and behaviour (the decisions from rtk_development#144)
# ---------------------------------------------------------------------------


def drop_warning(name: str, band: str, portal: bool) -> str:
    target = "either band" if band == "Auto" else band
    if portal:
        return (
            f"'{name}' reconnects to apply this, so the base will drop off the "
            "Portal for about a minute and this page will stop updating. If it "
            f"can't join on {target}, it puts the old band back by itself and "
            "reappears."
        )
    return (
        f"'{name}' reconnects to apply this, so this console session will drop "
        f"for about half a minute. If it can't join on {target}, it puts the old "
        "band back by itself; failing that, "
        f"'{AP_SSID}' appears in about {FALLBACK_MIN} min."
    )


def refusal(name: str, band: str) -> str:
    return (
        f"'{name}' isn't being heard on {band} right now, so the base couldn't "
        "join it there. The band was not changed."
    )


def revert_note(conn: Saved) -> str:
    return (
        f"Your change to {conn.reverted} was undone: the base couldn't join "
        f"'{conn.name}' on {conn.reverted}, so it went back to {conn.band}."
    )


def request_band(conn: Saved, band: str, portal: bool, refresh) -> None:
    """Stub of the decided flow: inactive → save; active → pre-check, confirm."""
    if band == conn.band:
        return
    if not conn.active:
        conn.band, conn.reverted = band, None
        ui.notify(f"Saved: '{conn.name}' will use {band}.", type="positive")
        refresh()
        return
    if band != "Auto" and band not in heard_on(conn.name):
        ui.notify(refusal(conn.name, band), type="warning", multi_line=True)
        refresh()
        return
    with ui.dialog() as dialog, ui.card().classes("q-pa-md").style("max-width: 440px"):
        ui.label(f"Use {band} for '{conn.name}'?").classes("text-h6 text-white")
        ui.label(drop_warning(conn.name, band, portal)).classes(
            "text-caption text-orange-4 q-mt-sm"
        )

        def confirm() -> None:
            conn.band, conn.reverted = band, None
            dialog.close()
            ui.notify(
                f"Instructed the base to rejoin '{conn.name}' on {band}.",
                type="positive",
            )
            refresh()

        with ui.row().classes("justify-end gap-2 q-mt-md"):
            ui.button("Cancel", on_click=lambda: (dialog.close(), refresh())).props(
                "flat"
            )
            ui.button("Change band", on_click=confirm).props("color=primary")
    dialog.open()


def join_dialog(ssid: str, band_picker) -> None:
    with ui.dialog() as dialog, ui.card().classes("q-pa-md").style("min-width: 400px"):
        ui.label(f"Join '{ssid}'").classes("text-h6 text-white")
        ui.input("Password", password=True, password_toggle_button=True).classes(
            "w-full"
        )
        if band_picker is not None:
            band_picker(ssid)
        ui.label(
            "This console session will drop once you connect. Rejoin on the new "
            f"network to continue — if it fails, '{AP_SSID}' will appear again "
            f"in about {FALLBACK_MIN} min."
        ).classes("text-caption text-orange-4 q-mt-sm")
        with ui.row().classes("justify-end gap-2 q-mt-md"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button(
                "Connect", on_click=lambda: (dialog.close(), ui.notify("(stub)"))
            ).props("color=primary")
    dialog.open()


def signal_icon(signal: int) -> str:
    if signal >= 75:
        return "signal_wifi_4_bar"
    if signal >= 50:
        return "network_wifi_3_bar"
    if signal >= 25:
        return "network_wifi_2_bar"
    return "network_wifi_1_bar"


LEVEL_COLOR = {"Good": "green-4", "Marginal": "orange-4", "Poor": "red-4"}


# ---------------------------------------------------------------------------
# Variant A: inline segmented toggle on every row, band chips on scan rows
# ---------------------------------------------------------------------------


def variant_a(has5: bool, portal: bool, refresh) -> None:
    with ui.card().classes("w-full q-pa-md"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("wifi").classes("text-grey-4 text-3xl")
            with ui.column().classes("gap-0"):
                ui.label("Uplink").classes("text-caption text-grey-5")
                ui.label("Wi-Fi (wlan0)").classes("text-body1 text-green-4 font-bold")
        with ui.row().classes("q-mt-sm gap-6"):
            ui.label("IP: 192.168.1.40").classes("text-caption text-grey-5")
            ui.label(f"Network: {LINK['network']}").classes("text-caption text-grey-5")
            ui.label(
                f"Wi-Fi link: {LINK['level']} (−{abs(LINK['dbm'])} dBm · "
                f"{band_of(LINK['mhz'])})"
            ).classes(f"text-caption text-{LEVEL_COLOR[LINK['level']]}")

    ui.label("Saved Networks").classes("text-h5 text-white q-mt-lg")
    for conn in SAVED:
        with ui.card().classes("w-full q-pa-sm"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("wifi").classes(
                        "text-green-4 text-xl" if conn.active else "text-grey-5 text-xl"
                    )
                    ui.label(conn.name).classes("text-body1 text-white")
                    if conn.active:
                        ui.badge("Active", color="green")
                with ui.row().classes("items-center gap-2"):
                    if has5:
                        ui.toggle(
                            BANDS,
                            value=conn.band,
                            on_change=lambda e, c=conn: request_band(
                                c, e.value, portal, refresh
                            ),
                        ).props("dense no-caps toggle-color=primary size=sm")
                    if not conn.active:
                        ui.button("Switch").props("flat dense color=primary")
                    ui.button("Forget").props("flat dense color=negative")
            if conn.reverted:
                with ui.row().classes("items-center gap-2 q-mt-xs q-ml-lg"):
                    ui.icon("undo").classes("text-orange-4")
                    ui.label(revert_note(conn)).classes("text-caption text-orange-4")

    with ui.row().classes("items-center justify-between w-full q-mt-lg"):
        ui.label("Nearby WiFi Networks").classes("text-h5 text-white")
        ui.button("Rescan", icon="refresh").props("flat color=white")
    for net in SCAN:
        bands = {b: s for b, s in net.bands.items() if has5 or b == "2.4 GHz"}
        if not bands:
            continue
        best = max(bands.values())

        def picker(ssid: str) -> None:
            ui.label("Wi-Fi band").classes("text-caption text-grey-5 q-mt-sm")
            ui.toggle(BANDS, value="Auto").props("dense no-caps")

        with (
            ui.card()
            .classes("w-full q-pa-sm cursor-pointer")
            .on("click", lambda _e, s=net.ssid: join_dialog(s, picker if has5 else None))
        ):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon(signal_icon(best)).classes("text-blue-4 text-xl")
                    ui.label(net.ssid).classes("text-body1 text-white")
                    if net.secure:
                        ui.icon("lock").classes("text-grey-5 text-sm")
                    if has5:
                        for b in bands:
                            ui.badge(b, color="grey-8").props("outline")
                ui.label(f"{best}%").classes("text-caption text-grey-5")


# ---------------------------------------------------------------------------
# Variant B: compact rows with a lock badge, a Band dialog that shows what
# each band hears now, a stronger-band hint, expandable per-band scan rows
# ---------------------------------------------------------------------------


def band_dialog(conn: Saved, portal: bool, refresh) -> None:
    heard = heard_on(conn.name)
    with ui.dialog() as dialog, ui.card().classes("q-pa-md").style("min-width: 420px"):
        ui.label(f"Wi-Fi band for '{conn.name}'").classes("text-h6 text-white")
        if conn.reverted:
            with ui.row().classes("items-start gap-2 q-mt-sm no-wrap"):
                ui.icon("undo").classes("text-orange-4")
                ui.label(revert_note(conn)).classes("text-caption text-orange-4")
        choice = {"band": conn.band}
        with ui.column().classes("gap-1 q-mt-sm w-full"):
            for band in BANDS:
                if band == "Auto":
                    hint = "Either band; the base picks."
                    blocked = False
                elif band in heard:
                    hint = f"Heard now at {heard[band]}%."
                    blocked = False
                else:
                    hint = "Not heard right now."
                    blocked = conn.active
                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                    radio = ui.radio(
                        [band],
                        value=choice["band"] if choice["band"] == band else None,
                    ).props("dense" + (" disable" if blocked else ""))
                    ui.label(hint).classes(
                        "text-caption " + ("text-grey-7" if blocked else "text-grey-5")
                    )
                    radio.on_value_change(
                        lambda e, b=band: (choice.update(band=b), dialog.close(),
                                           request_band(conn, b, portal, refresh))
                        if e.value else None
                    )
        if conn.active:
            ui.label(
                "Changing the band of the network the base is on reconnects it."
            ).classes("text-caption text-grey-6 q-mt-sm")
        with ui.row().classes("justify-end q-mt-md"):
            ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()


def variant_b(has5: bool, portal: bool, refresh) -> None:
    on_band = band_of(LINK["mhz"])
    heard = heard_on(LINK["network"])
    with ui.card().classes("w-full q-pa-md"):
        ui.label("Uplink").classes("text-caption text-grey-5")
        ui.label(f"Wi-Fi · {LINK['network']}").classes("text-h6 text-green-4")
        with ui.row().classes("gap-8 q-mt-sm"):
            for label, value, sub, color in (
                ("Wi-Fi link", LINK["level"], f"−{abs(LINK['dbm'])} dBm",
                 LEVEL_COLOR[LINK["level"]]),
                ("Band", on_band, f"{LINK['mhz']} MHz", "white"),
                ("Address", "192.168.1.40", None, "white"),
            ):
                with ui.column().classes("gap-0"):
                    ui.label(label).classes("text-caption text-grey-5")
                    ui.label(value).classes(f"text-body1 text-{color}")
                    if sub:
                        ui.label(sub).classes("text-caption text-grey-6")
        other = next((b for b in heard if b != on_band), None)
        active = next(c for c in SAVED if c.active)
        if has5 and other and heard[other] > heard[on_band] + 15:
            with ui.row().classes("items-center gap-2 q-mt-sm"):
                ui.icon("lightbulb").classes("text-amber-4")
                ui.label(
                    f"'{LINK['network']}' is heard more strongly on {other} "
                    f"({heard[other]}% vs {heard[on_band]}%)."
                ).classes("text-caption text-amber-4")
                ui.button(
                    f"Use {other}",
                    on_click=lambda: band_dialog(active, portal, refresh),
                ).props("flat dense color=amber no-caps")

    ui.label("Saved Networks").classes("text-h5 text-white q-mt-lg")
    for conn in SAVED:
        with ui.card().classes("w-full q-pa-sm"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("wifi").classes(
                        "text-green-4 text-xl" if conn.active else "text-grey-5 text-xl"
                    )
                    ui.label(conn.name).classes("text-body1 text-white")
                    if conn.active:
                        ui.badge("Active", color="green")
                    if has5 and conn.band != "Auto":
                        ui.badge(f"{conn.band} only", color="blue-grey")
                    if conn.reverted:
                        ui.icon("undo").classes("text-orange-4").tooltip(
                            revert_note(conn)
                        )
                with ui.row().classes("gap-2"):
                    if has5:
                        ui.button(
                            "Band",
                            icon="settings_input_antenna",
                            on_click=lambda _e, c=conn: band_dialog(c, portal, refresh),
                        ).props("flat dense color=white no-caps")
                    if not conn.active:
                        ui.button("Switch").props("flat dense color=primary")
                    ui.button("Forget").props("flat dense color=negative")

    ui.label("Nearby WiFi Networks").classes("text-h5 text-white q-mt-lg")
    for net in SCAN:
        bands = {b: s for b, s in net.bands.items() if has5 or b == "2.4 GHz"}
        if not bands:
            continue
        best = max(bands.values())
        summary = " + ".join(bands) if has5 else ""

        def picker(ssid: str) -> None:
            h = heard_on(ssid)
            ui.select(
                {b: (b if b == "Auto" else f"{b} ({h[b]}%)" if b in h
                     else f"{b} (not heard)") for b in BANDS},
                value="Auto",
                label="Wi-Fi band",
            ).classes("w-full")

        with ui.expansion().classes("w-full bg-grey-10 rounded-borders") as exp:
            with exp.add_slot("header"), ui.row().classes(
                "items-center justify-between w-full"
            ):
                with ui.row().classes("items-center gap-3"):
                    ui.icon(signal_icon(best)).classes("text-blue-4 text-xl")
                    ui.label(net.ssid).classes("text-body1 text-white")
                    if net.secure:
                        ui.icon("lock").classes("text-grey-5 text-sm")
                ui.label(summary).classes("text-caption text-grey-5")
            for band, sig in bands.items():
                with ui.row().classes("items-center gap-3 q-pl-xl"):
                    ui.icon(signal_icon(sig)).classes("text-blue-3")
                    ui.label(f"{band}: {sig}%").classes("text-caption text-grey-4")
            ui.button(
                "Join",
                on_click=lambda _e, s=net.ssid: join_dialog(s, picker if has5 else None),
            ).props("flat dense color=primary").classes("q-ml-xl")


# ---------------------------------------------------------------------------
# Variant C: saved networks as a table with a Band dropdown and a "heard
# on" column; scan list grouped into 2.4 GHz and 5 GHz sections
# ---------------------------------------------------------------------------


def bars(sig: int) -> str:
    return "▂▄▆█"[: max(1, round(sig / 25))]


def variant_c(has5: bool, portal: bool, refresh) -> None:
    with ui.card().classes("w-full q-pa-md"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("wifi").classes("text-grey-4 text-2xl")
            ui.label(f"Uplink: {LINK['network']}").classes("text-h6 text-green-4")
            ui.badge(band_of(LINK["mhz"]), color="primary")
        ui.label(
            f"Wi-Fi link {LINK['level']}, −{abs(LINK['dbm'])} dBm · "
            f"{LINK['mhz']} MHz · 192.168.1.40"
        ).classes(f"text-caption text-{LEVEL_COLOR[LINK['level']]}")

    ui.label("Saved Networks").classes("text-h5 text-white q-mt-lg")
    cols = "2fr 1.4fr 1.4fr auto" if has5 else "2fr 1.4fr auto"
    with ui.element("div").classes("w-full").style(
        f"display:grid; grid-template-columns:{cols}; gap:6px 16px; align-items:center"
    ):
        for h in ["Network", "Wi-Fi band", "Heard on now", ""] if has5 else [
            "Network", "Heard now", ""
        ]:
            ui.label(h).classes("text-overline text-grey-6")
        for conn in SAVED:
            tint = "background:rgba(255,152,0,.08);" if conn.reverted else ""
            with ui.row().classes("items-center gap-2").style(tint):
                ui.label(conn.name).classes(
                    "text-body1 " + ("text-green-4" if conn.active else "text-white")
                )
                if conn.active:
                    ui.badge("Active", color="green")
            if has5:
                with ui.column().classes("gap-0"):
                    ui.select(
                        BANDS,
                        value=conn.band,
                        on_change=lambda e, c=conn: request_band(
                            c, e.value, portal, refresh
                        ),
                    ).props("dense outlined options-dense").classes("w-full")
                    if conn.reverted:
                        ui.label(f"Undone: couldn't join on {conn.reverted}").classes(
                            "text-caption text-orange-4"
                        ).tooltip(revert_note(conn))
            h = heard_on(conn.name)
            ui.label(
                "  ".join(f"{b} {bars(s)}" for b, s in h.items()
                          if has5 or b == "2.4 GHz") or "Not in range"
            ).classes("text-caption text-grey-4")
            with ui.row().classes("gap-1"):
                if not conn.active:
                    ui.button("Switch").props("flat dense color=primary")
                ui.button("Forget").props("flat dense color=negative")

    with ui.row().classes("items-center justify-between w-full q-mt-lg"):
        ui.label("Nearby WiFi Networks").classes("text-h5 text-white")
        ui.button("Rescan", icon="refresh").props("flat color=white")

    def picker(ssid: str) -> None:
        with ui.expansion("Advanced").classes("w-full text-grey-5"):
            ui.select(BANDS, value="Auto", label="Wi-Fi band").classes("w-full")

    for band in ["2.4 GHz", "5 GHz"] if has5 else ["2.4 GHz"]:
        if has5:
            ui.label(band).classes("text-subtitle1 text-grey-4 q-mt-sm")
        nets = sorted(
            (n for n in SCAN if band in n.bands), key=lambda n: -n.bands[band]
        )
        with ui.row().classes("w-full gap-2"):
            for net in nets:
                with (
                    ui.card()
                    .classes("q-pa-sm cursor-pointer")
                    .style("min-width: 200px")
                    .on("click", lambda _e, s=net.ssid: join_dialog(
                        s, picker if has5 else None))
                ):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon(signal_icon(net.bands[band])).classes("text-blue-4")
                        ui.label(net.ssid).classes("text-white")
                        if net.secure:
                            ui.icon("lock").classes("text-grey-5 text-sm")
                        ui.label(f"{net.bands[band]}%").classes(
                            "text-caption text-grey-5"
                        )


# ---------------------------------------------------------------------------
# Page, shell and floating switcher
# ---------------------------------------------------------------------------


@ui.page("/network", dark=True)
@ui.page("/", dark=True)
def page(variant: str = "A", no5g: int = 0, portal: int = 0, reverted: int = 0) -> None:
    variant = variant if variant in VARIANTS else "A"
    has5 = not no5g
    farm = SAVED[0]
    farm.reverted = "5 GHz" if reverted else None

    def url(**kw) -> str:
        q = {"variant": variant, "no5g": no5g, "portal": portal, "reverted": reverted}
        q.update(kw)
        return "/network?" + urlencode(q)

    with ui.header(elevated=True).classes("items-center q-px-md").style(
        "background-color: #1a1a2e"
    ):
        ui.icon("menu").classes("text-white text-xl")
        ui.label("SP-Base").classes("text-h6 text-white q-ml-sm")
        ui.space()
        ui.label("Network").classes("text-subtitle1 text-white")

    body = ui.column().classes("w-full q-pa-md").style("padding-bottom: 120px")

    def render() -> None:
        body.clear()
        with body:
            ui.label("Network").classes("text-h4 text-white q-mb-md")
            if portal:
                ui.label("Scenario: reached through the Portal").classes(
                    "text-caption text-purple-3"
                )
            {"A": variant_a, "B": variant_b, "C": variant_c}[variant](
                has5, bool(portal), render
            )

    render()

    keys = list(VARIANTS)
    i = keys.index(variant)
    prev_v, next_v = keys[(i - 1) % len(keys)], keys[(i + 1) % len(keys)]
    with ui.element("div").style(
        "position:fixed; bottom:16px; left:50%; transform:translateX(-50%);"
        "z-index:9999; background:#fff; color:#111; border-radius:999px;"
        "box-shadow:0 4px 18px rgba(0,0,0,.5); padding:6px 14px;"
        "display:flex; gap:10px; align-items:center; font:13px sans-serif"
    ):
        ui.button(icon="chevron_left", on_click=lambda: ui.navigate.to(
            url(variant=prev_v))).props("flat round dense color=black")
        ui.label(f"PROTOTYPE {variant} ({VARIANTS[variant]})").style("font-weight:600")
        ui.button(icon="chevron_right", on_click=lambda: ui.navigate.to(
            url(variant=next_v))).props("flat round dense color=black")
        for key, label, val in (
            ("no5g", "No 5 GHz adapter", no5g),
            ("portal", "Via Portal", portal),
            ("reverted", "Revert note", reverted),
        ):
            ui.checkbox(label, value=bool(val), on_change=lambda e, k=key: ui.navigate.to(
                url(**{k: int(e.value)}))).props("dense color=black keep-color").style("color:#111")

    def on_key(e) -> None:
        if not e.action.keydown:
            return
        if e.key.arrow_left:
            ui.navigate.to(url(variant=prev_v))
        elif e.key.arrow_right:
            ui.navigate.to(url(variant=next_v))

    ui.keyboard(on_key=on_key)


ui.run(port=8090, title="PROTOTYPE Wi-Fi band", reload=False, show=False)
