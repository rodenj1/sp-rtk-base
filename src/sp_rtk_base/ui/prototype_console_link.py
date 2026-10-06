"""PROTOTYPE, throw away: three ways to connect a Console link of either kind.

Question (rtk_development#35): how do the Connect panel and the REST device
API present a Console link that can be Serial or Bluetooth?

Three variants, switchable via ``/survey?variant=A|B|C`` and the floating
bar (or the arrow keys). Mounted above the real Connection card only when
``SP_RTK_BASE_PROTOTYPE=1``. Every action is a stub: nothing touches the
DeviceService, the receiver or BlueZ. The "proposed API" panel shows the
request and status each variant implies.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from nicegui import ui

VARIANTS = {
    "A": "Kind toggle",
    "B": "One link picker",
    "C": "Two connect tiles",
}

SERIAL_PORTS = ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0"]
BAUDS = [9600, 38400, 57600, 115200, 230400, 460800]
BT = {"name": "RTK_BASE_TST", "mac": "98:D3:71:FE:FC:47", "bonded": True}
STAGES = ["pair", "connect", "identify"]
STAGE_LABEL = {
    "pair": "Pair (Bond with the Input profile's PIN)",
    "connect": "Connect (RFCOMM, discovery up to 30 s, can't cancel)",
    "identify": "Identify (receiver answers, Console port)",
}


def enabled() -> bool:
    return os.environ.get("SP_RTK_BASE_PROTOTYPE") == "1"


class _Sim:
    """In-memory stand-in for the device state the card shows."""

    def __init__(self) -> None:
        self.input_is_bluetooth = True
        self.fail_at: str | None = None
        self.state = "disconnected"
        self.kind = "serial"
        self.port = "/dev/ttyUSB1"
        self.baud = 57600
        self.stages: dict[str, str] = {}
        self.error = ""

    def request(self, variant: str) -> tuple[str, dict[str, Any] | None]:
        if variant == "C" and self.kind == "bluetooth":
            return "POST /api/device/connect/bluetooth", None
        if variant == "B":
            body: dict[str, Any] = {
                "vendor": "ublox",
                "link": self.port if self.kind == "serial" else "bluetooth",
            }
            if self.kind == "serial":
                body["baud_rate"] = self.baud
            return "POST /api/device/connect", body
        link: dict[str, Any] = (
            {"kind": "serial", "port": self.port, "baud_rate": self.baud}
            if self.kind == "serial"
            else {"kind": "bluetooth"}
        )
        return "POST /api/device/connect", {"vendor": "ublox", "link": link}

    def status(self) -> dict[str, Any]:
        link: dict[str, Any] | None = None
        if self.state != "disconnected":
            link = (
                {"kind": "serial", "port": self.port, "baud_rate": self.baud}
                if self.kind == "serial"
                else {"kind": "bluetooth", "device_name": BT["name"], "mac": BT["mac"]}
            )
        return {
            "state": self.state,
            "link": link,
            "stages": self.stages or None,
            "console_port": "UART2"
            if self.state == "connected" and self.kind == "bluetooth"
            else None,
            "last_error": self.error or None,
        }


def render(variant: str) -> None:
    variant = variant if variant in VARIANTS else "A"
    sim = _Sim()
    sim.kind = "bluetooth"

    with (
        ui.card().classes("w-full q-pa-md q-mb-md").style("border: 2px dashed #e0a030")
    ):
        with ui.row().classes("items-center gap-2"):
            ui.label(f"PROTOTYPE {variant}: {VARIANTS[variant]}").classes(
                "text-h6 text-warning"
            )
            ui.label("stubbed, connects nothing").classes("text-caption text-grey-5")
        with ui.row().classes("gap-4 items-center"):
            ui.switch(
                "Input profile is Bluetooth",
                value=True,
                on_change=lambda e: (
                    setattr(sim, "input_is_bluetooth", e.value),
                    body.refresh(),
                ),
            )
            ui.select(
                {
                    None: "connect succeeds",
                    "pair": "fail at Pair",
                    "connect": "fail at Connect",
                    "identify": "fail at Identify",
                },
                value=None,
                label="simulate",
                on_change=lambda e: setattr(sim, "fail_at", e.value),
            ).classes("w-48")
        ui.separator()

        async def connect() -> None:
            sim.state, sim.error, sim.stages = "connecting", "", {}
            body.refresh()
            if sim.kind == "serial":
                await asyncio.sleep(0.8)
                sim.state = "connected"
            else:
                for st in STAGES:
                    sim.stages[st] = "running"
                    body.refresh()
                    await asyncio.sleep(0.3 if st == "pair" and BT["bonded"] else 1.2)
                    if sim.fail_at == st:
                        sim.stages[st] = "red"
                        sim.state = "disconnected"
                        sim.error = {
                            "pair": "Pair: the module refused the Input profile's PIN. Check it on the Input page.",
                            "connect": f"Connect: {BT['name']} did not answer (is it powered and in range?). Is the Relay stopped?",
                            "identify": "Identify: the link is up but the receiver does not answer UBX: a baud mismatch between the module and UART2?",
                        }[st]
                        body.refresh()
                        return
                    sim.stages[st] = (
                        "green" if st != "pair" or not BT["bonded"] else "skipped"
                    )
                sim.state = "connected"
            body.refresh()

        def disconnect() -> None:
            sim.state, sim.stages, sim.error = "disconnected", {}, ""
            body.refresh()

        def stage_list() -> None:
            if not sim.stages:
                return
            icons = {
                "running": ("hourglass_top", "info"),
                "green": ("check_circle", "positive"),
                "red": ("cancel", "negative"),
                "skipped": ("remove_circle_outline", "grey"),
            }
            with ui.column().classes("gap-0 q-mt-xs"):
                for st in STAGES:
                    if st in sim.stages:
                        icon, color = icons[sim.stages[st]]
                        with ui.row().classes("items-center gap-1"):
                            ui.icon(icon, color=color)
                            extra = (
                                " (already bonded)"
                                if sim.stages[st] == "skipped"
                                else ""
                            )
                            ui.label(STAGE_LABEL[st] + extra).classes("text-caption")

        def status_line() -> None:
            color = {"connected": "positive", "connecting": "info"}.get(
                sim.state, "grey"
            )
            what = (
                f"{sim.port} @ {sim.baud}"
                if sim.kind == "serial"
                else f"Bluetooth · {BT['name']} · console port UART2"
            )
            text = {"connected": f"Connected: {what}", "connecting": "Connecting…"}.get(
                sim.state, "Disconnected"
            )
            ui.badge(text, color=color)
            if sim.error:
                ui.label(sim.error).classes("text-negative text-caption")

        def bt_device_line() -> None:
            if sim.input_is_bluetooth:
                ui.label(
                    f"{BT['name']} · {BT['mac']} · {'Bonded' if BT['bonded'] else 'not paired yet'}"
                ).classes("text-body2")
                ui.label(
                    "From the Bluetooth Input profile. Change it on the Input page."
                ).classes("text-caption text-grey-5")
            else:
                ui.label(
                    "Bluetooth needs a Bluetooth Input profile. Set one up on the Input page."
                ).classes("text-caption text-warning")

        busy = lambda: sim.state == "connecting"  # noqa: E731

        def actions(can_connect: bool = True) -> None:
            with ui.row().classes("gap-2 items-center"):
                b = ui.button("Connect", icon="link", on_click=connect)
                b.set_enabled(can_connect and sim.state == "disconnected")
                ui.button("Disconnect", icon="link_off", on_click=disconnect).props(
                    "color=grey"
                ).set_enabled(sim.state == "connected")
                c = ui.button("Cancel", icon="cancel").props("color=negative outline")
                c.set_visibility(busy())
                c.set_enabled(
                    not (
                        sim.kind == "bluetooth"
                        and sim.stages.get("connect") == "running"
                    )
                )
                if sim.kind == "bluetooth" and sim.stages.get("connect") == "running":
                    c.tooltip("Bluetooth discovery can't be interrupted")

        def serial_fields(show_port: bool = True) -> None:
            with ui.row().classes("w-full gap-4"):
                if show_port:
                    ui.select(
                        SERIAL_PORTS,
                        value=sim.port,
                        label="Serial Port",
                        on_change=lambda e: setattr(sim, "port", e.value),
                    ).classes("col-grow")
                ui.select(
                    BAUDS,
                    value=sim.baud,
                    label="Baud Rate",
                    on_change=lambda e: setattr(sim, "baud", e.value),
                ).classes("w-40")
                ui.button("Detect", icon="search").props("outline color=info").classes(
                    "self-center"
                )

        @ui.refreshable
        def body() -> None:
            locked = sim.state != "disconnected"
            if variant == "A":
                status_line()
                tog = ui.toggle(
                    {"serial": "Serial cable", "bluetooth": "Bluetooth"},
                    value=sim.kind,
                    on_change=lambda e: (setattr(sim, "kind", e.value), body.refresh()),
                ).classes("q-mt-sm")
                tog.set_enabled(not locked)
                if sim.kind == "serial":
                    serial_fields()
                else:
                    bt_device_line()
                actions(sim.kind == "serial" or sim.input_is_bluetooth)
                stage_list()

            elif variant == "B":
                status_line()
                opts = {p: f"{p}  (serial)" for p in SERIAL_PORTS}
                if sim.input_is_bluetooth:
                    opts["bluetooth"] = f"Bluetooth · {BT['name']}  ({BT['mac']})"
                current = (
                    "bluetooth"
                    if sim.kind == "bluetooth" and sim.input_is_bluetooth
                    else sim.port
                )

                def pick(e: Any) -> None:
                    if e.value == "bluetooth":
                        sim.kind = "bluetooth"
                    else:
                        sim.kind, sim.port = "serial", e.value
                    body.refresh()

                with ui.row().classes("w-full gap-4 q-mt-sm"):
                    s = ui.select(
                        opts, value=current, label="Link", on_change=pick
                    ).classes("col-grow")
                    s.set_enabled(not locked)
                    if sim.kind == "serial":
                        ui.select(
                            BAUDS,
                            value=sim.baud,
                            label="Baud Rate",
                            on_change=lambda e: setattr(sim, "baud", e.value),
                        ).classes("w-40")
                        ui.button("Detect", icon="search").props(
                            "outline color=info"
                        ).classes("self-center")
                if sim.kind == "bluetooth":
                    ui.label(
                        "Baud and Detect don't apply: the module sets the rate."
                    ).classes("text-caption text-grey-5")
                actions()
                stage_list()

            else:  # C
                status_line()
                with ui.row().classes("w-full gap-4 q-mt-sm no-wrap"):
                    for kind, title, icon in (
                        ("serial", "By cable", "cable"),
                        ("bluetooth", "By Bluetooth", "bluetooth"),
                    ):
                        active = sim.kind == kind and locked
                        dim = locked and not active
                        with (
                            ui.card()
                            .classes("col q-pa-sm")
                            .style(f"opacity: {0.4 if dim else 1}")
                        ):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon(icon, size="md")
                                ui.label(title).classes("text-subtitle1")
                            if kind == "serial":
                                serial_fields()
                            else:
                                bt_device_line()

                            def go(k: str = kind) -> Any:
                                sim.kind = k
                                return connect()

                            with ui.row().classes("gap-2"):
                                b = ui.button("Connect", icon="link", on_click=go)
                                b.set_enabled(
                                    not locked
                                    and (kind == "serial" or sim.input_is_bluetooth)
                                )
                                if active and sim.state == "connected":
                                    ui.button(
                                        "Disconnect",
                                        icon="link_off",
                                        on_click=disconnect,
                                    ).props("color=grey")
                            if kind == "bluetooth" and sim.kind == "bluetooth":
                                stage_list()

            method, req = sim.request(variant)
            with ui.expansion("Proposed API", icon="api").classes("w-full q-mt-sm"):
                ui.label(method).classes("text-caption")
                if req is not None:
                    ui.code(json.dumps(req, indent=2), language="json").classes(
                        "w-full"
                    )
                ui.label("GET /api/device/status").classes("text-caption q-mt-sm")
                ui.code(json.dumps(sim.status(), indent=2), language="json").classes(
                    "w-full"
                )

        body()

    _switcher(variant)


def _switcher(current: str) -> None:
    keys = list(VARIANTS)

    def go(step: int) -> None:
        nxt = keys[(keys.index(current) + step) % len(keys)]
        ui.navigate.to(f"/survey?variant={nxt}")

    with ui.element("div").style(
        "position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); z-index: 9999;"
        "background: #fff; color: #000; border-radius: 999px; padding: 4px 12px;"
        "box-shadow: 0 2px 10px rgba(0,0,0,.5); display: flex; align-items: center; gap: 8px;"
    ):
        ui.button(icon="chevron_left", on_click=lambda: go(-1)).props(
            "flat round dense color=black"
        )
        ui.label(f"{current} ({VARIANTS[current]})").style("font-weight: 600")
        ui.button(icon="chevron_right", on_click=lambda: go(1)).props(
            "flat round dense color=black"
        )

    def on_key(e: Any) -> None:
        if e.action.keydown and not e.action.repeat:
            if e.key.arrow_left:
                go(-1)
            elif e.key.arrow_right:
                go(1)

    ui.keyboard(on_key=on_key, ignore=["input", "select", "button", "textarea"])
