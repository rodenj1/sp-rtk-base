"""PROTOTYPE, throwaway: Signal Quality indicator layouts.

Question (rodenj1/rtk_development#7): where and how do the Signal Quality
verdict and its key numbers appear on the survey-in page and the dashboard,
including the no-data state?

Three structurally different variants, mounted on the real ``/survey`` and
``/`` pages, switched via ``?variant=A|B|C`` and ``?scenario=<key>``:

- A "Title chip": a compact pill beside the page title; numbers on hover/tap.
- B "Signal card": a full card with the verdict and one meter per measure,
  showing where each value sits against its Good/Marginal/Poor bands.
- C "Plain sentence": a traffic light and one plain-language sentence, with
  the numbers as small print underneath.

Data is a synthetic Signal Snapshot per scenario, jittered every 2 s so it
feels live. The verdict logic is the real rule from #6. Only rendered when
``SP_RTK_BASE_PROTOTYPE=1``. Nothing here is production code.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import os
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from nicegui import context, ui

Level = Literal["Good", "Marginal", "Poor"]
Page = Literal["survey", "dashboard"]

ENABLED = os.environ.get("SP_RTK_BASE_PROTOTYPE") == "1"

# --- Thresholds and verdict: the rule decided in #6 -------------------------

L1_GOOD, L1_MARGINAL = 44.0, 40.0
L2_GOOD, L2_MARGINAL = 41.0, 37.0
SATS_GOOD, SATS_MARGINAL = 15, 10
RANK: dict[Level, int] = {"Good": 0, "Marginal": 1, "Poor": 2}
COLOR: dict[Level, str] = {
    "Good": "positive",
    "Marginal": "warning",
    "Poor": "negative",
}


def _level(value: float | None, good: float, marginal: float) -> Level:
    if value is None:
        return "Poor"
    if value >= good:
        return "Good"
    return "Marginal" if value >= marginal else "Poor"


@dataclass(frozen=True)
class Measures:
    """What a Signal Snapshot reduces to: two Band strengths and a count."""

    l1: float | None  # L1 Band strength, dB-Hz; None = no L1 signals
    l2: float | None
    sats: int  # Usable satellites


@dataclass(frozen=True)
class Verdict:
    level: Level
    l1: Level
    l2: Level
    sats: Level
    reason: str  # which measure caused a non-Good verdict


def judge(m: Measures) -> Verdict:
    l1 = _level(m.l1, L1_GOOD, L1_MARGINAL)
    l2 = _level(m.l2, L2_GOOD, L2_MARGINAL)
    sats = _level(float(m.sats), SATS_GOOD, SATS_MARGINAL)
    worst = max((l1, l2, sats), key=lambda v: RANK[v])
    causes = []
    if l1 == worst and worst != "Good":
        causes.append("no L1 signal" if m.l1 is None else "L1 weak")
    if l2 == worst and worst != "Good":
        causes.append("no L2 signal" if m.l2 is None else "L2 weak")
    if sats == worst and worst != "Good":
        causes.append("few satellites")
    return Verdict(worst, l1, l2, sats, ", ".join(causes))


# --- Scenarios ---------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    label: str
    measures: Measures | None  # None = no Signal Snapshot available
    no_data: dict[Page, str] | None = None  # why there is no data, per page


SCENARIOS: dict[str, Scenario] = {
    "good": Scenario("Good: test-base clear sky", Measures(52.2, 51.0, 28)),
    "marginal": Scenario("Marginal: L2 degraded", Measures(47.5, 39.0, 22)),
    "poor": Scenario("Poor: heavy obstruction", Measures(41.0, 36.0, 8)),
    "l1only": Scenario("Poor: L1-only antenna", Measures(50.0, None, 24)),
    "nodata": Scenario(
        "No data: source unavailable",
        None,
        {
            "survey": "Connect the receiver to see Signal Quality.",
            "dashboard": "Relay is stopped. Signal Quality shows while it runs.",
        },
    ),
    "nomsm": Scenario(
        "No data: no MSM in stream",
        None,
        {
            "survey": "The receiver did not answer the signal poll.",
            "dashboard": "No MSM messages in the RTCM stream. Enable MSM4 or MSM7 "
            "output on the receiver port the Relay reads.",
        },
    ),
}


def _jitter(m: Measures) -> Measures:
    def j(v: float | None) -> float | None:
        return None if v is None else round(v + random.uniform(-0.6, 0.6), 1)

    return Measures(j(m.l1), j(m.l2), max(0, m.sats + random.choice((-1, 0, 0, 0, 1))))


# --- Shared bits -------------------------------------------------------------


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f} dB-Hz"


def _numbers_line(m: Measures) -> str:
    return f"L1 {_fmt(m.l1)} · L2 {_fmt(m.l2)} · {m.sats} satellites"


def _render_live(
    container: ui.element, render: Callable[[Measures | None], None], sc: Scenario
) -> None:
    def tick() -> None:
        container.clear()
        with container:
            render(None if sc.measures is None else _jitter(sc.measures))

    tick()
    ui.timer(2.0, tick)


# --- Variant A: title chip ---------------------------------------------------


def _variant_a(title_slot: ui.element, page: Page, sc: Scenario) -> None:
    box = ui.row().classes("items-center")
    box.move(title_slot)

    def render(m: Measures | None) -> None:
        if m is None:
            with ui.element("div").classes(
                "rounded-borders q-px-md q-py-xs bg-grey-8 cursor-pointer"
            ):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.icon("satellite_alt").classes("text-grey-5")
                    ui.label("Signal: no data").classes("text-grey-4 text-body2")
                ui.tooltip((sc.no_data or {}).get(page, ""))
            return
        v = judge(m)
        with ui.element("div").classes(
            f"rounded-borders q-px-md q-py-xs bg-{COLOR[v.level]} cursor-pointer"
        ):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("satellite_alt").classes("text-white")
                ui.label(f"Signal {v.level}").classes(
                    "text-white text-body2 text-weight-bold"
                )
                if v.reason:
                    ui.label(f"· {v.reason}").classes("text-white text-body2")
            ui.tooltip(_numbers_line(m))

    _render_live(box, render, sc)


# --- Variant B: signal card with meters --------------------------------------


def _meter(
    label: str,
    value: float | None,
    lo: float,
    hi: float,
    good: float,
    marginal: float,
    unit: str,
) -> None:
    lvl = _level(value, good, marginal)
    span = hi - lo

    def pct(x: float) -> float:
        return max(0.0, min(100.0, (x - lo) / span * 100))

    with ui.column().classes("col-grow gap-1").style("min-width: 180px"):
        with ui.row().classes("items-baseline justify-between w-full"):
            ui.label(label).classes("text-caption text-grey-5")
            txt = "no signal" if value is None else f"{value:.0f} {unit}"
            ui.label(txt).classes(f"text-{COLOR[lvl]} text-weight-bold")
        # Poor / Marginal / Good zones, with a marker at the value
        with (
            ui.element("div").classes("relative-position w-full").style("height: 10px")
        ):
            m, g = pct(marginal), pct(good)
            ui.element("div").classes("absolute-full rounded-borders").style(
                "opacity:.5; background: linear-gradient(to right, "
                f"#c10015 0 {m}%, #f2c037 {m}% {g}%, #21ba45 {g}% 100%)"
            )
            if value is not None:
                ui.element("div").classes("absolute bg-white").style(
                    f"left:calc({pct(value)}% - 2px); top:-3px; width:4px; height:16px; border-radius:2px"
                )


def _variant_b(below_slot: ui.element, page: Page, sc: Scenario) -> None:
    card = ui.card().classes("w-full q-pa-md q-mb-md")
    card.move(below_slot)
    with card:
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Signal Quality").classes("text-h6 text-white")
            head = ui.row().classes("items-center")
        ui.separator()
        body = ui.row().classes("w-full gap-6 q-mt-sm")

    def render_head(m: Measures | None) -> None:
        if m is None:
            ui.badge("No data", color="grey-7").classes("text-body2 q-px-sm")
            return
        v = judge(m)
        ui.badge(v.level, color=COLOR[v.level]).classes("text-body1 q-px-md q-py-xs")
        if v.reason:
            ui.label(v.reason).classes(f"text-{COLOR[v.level]} q-ml-sm")

    def render_body(m: Measures | None) -> None:
        if m is None:
            with ui.row().classes("items-center gap-2"):
                ui.icon("info").classes("text-grey-5")
                ui.label((sc.no_data or {}).get(page, "")).classes("text-grey-4")
            return
        _meter("L1 Band strength", m.l1, 30, 56, L1_GOOD, L1_MARGINAL, "dB-Hz")
        _meter("L2 Band strength", m.l2, 30, 56, L2_GOOD, L2_MARGINAL, "dB-Hz")
        _meter("Usable satellites", float(m.sats), 0, 32, SATS_GOOD, SATS_MARGINAL, "")

    def render(m: Measures | None) -> None:
        head.clear()
        with head:
            render_head(m)
        body.clear()
        with body:
            render_body(m)

    holder = ui.element("div").classes("hidden")
    holder.move(card)
    _render_live(holder, render, sc)


# --- Variant C: traffic light and a sentence ----------------------------------

SENTENCE: dict[Level, str] = {
    "Good": "The antenna is hearing the satellites well.",
    "Marginal": "The antenna is working, but reception is weaker than it should be",
    "Poor": "The antenna is not hearing the satellites well enough for reliable corrections",
}


def _variant_c(below_slot: ui.element, page: Page, sc: Scenario) -> None:
    row = (
        ui.row()
        .classes("w-full items-center no-wrap gap-4 q-pa-md q-mb-md rounded-borders")
        .style(
            "background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08)"
        )
    )
    row.move(below_slot)

    def render(m: Measures | None) -> None:
        lit: Level | None = None if m is None else judge(m).level
        with ui.column().classes("gap-1 items-center q-pa-xs rounded-borders bg-black"):
            for lvl in ("Poor", "Marginal", "Good"):
                on = lit == lvl
                ui.element("div").classes(f"bg-{COLOR[lvl]}").style(
                    f"width:18px; height:18px; border-radius:50%; opacity:{1 if on else 0.15}"
                )
        with ui.column().classes("gap-0"):
            if m is None:
                ui.label("Signal quality unknown").classes("text-h6 text-grey-4")
                ui.label((sc.no_data or {}).get(page, "")).classes("text-grey-5")
                return
            v = judge(m)
            text = SENTENCE[v.level]
            if v.reason:
                text += f": {v.reason}."
            ui.label(text).classes(f"text-h6 text-{COLOR[v.level]}")
            ui.label(_numbers_line(m)).classes("text-caption text-grey-5")

    _render_live(row, render, sc)


VARIANTS: dict[
    str, tuple[str, Callable[[ui.element, ui.element, Page, Scenario], None]]
] = {
    "A": ("Title chip", lambda t, b, p, s: _variant_a(t, p, s)),
    "B": ("Signal card", lambda t, b, p, s: _variant_b(b, p, s)),
    "C": ("Plain sentence", lambda t, b, p, s: _variant_c(b, p, s)),
}


# --- Mount and floating switcher ---------------------------------------------


def mount(page: Page, title_slot: ui.element, below_slot: ui.element) -> None:
    """Render the chosen variant into the host page's slots, plus the switcher."""
    if not ENABLED:
        return
    q = context.client.request.query_params
    variant = q.get("variant", "A") if q.get("variant") in VARIANTS else "A"
    scenario = q.get("scenario", "good") if q.get("scenario") in SCENARIOS else "good"
    VARIANTS[variant][1](title_slot, below_slot, page, SCENARIOS[scenario])
    _switcher(variant, scenario)


def _switcher(variant: str, scenario: str) -> None:
    keys = list(VARIANTS)
    i = keys.index(variant)

    def go(v: str, s: str) -> None:
        ui.navigate.to(f"?variant={v}&scenario={s}")

    prev_v, next_v = keys[(i - 1) % len(keys)], keys[(i + 1) % len(keys)]
    with (
        ui.row()
        .classes("fixed-bottom q-mb-md items-center no-wrap gap-2 q-px-md q-py-xs")
        .style(
            "left:50%; transform:translateX(-50%); width:max-content; z-index:9999; "
            "background:#fde047; color:#111; border-radius:999px; box-shadow:0 4px 18px rgba(0,0,0,.5)"
        )
    ):
        ui.label("PROTOTYPE").classes("text-weight-bold text-caption")
        ui.button(icon="chevron_left", on_click=lambda: go(prev_v, scenario)).props(
            "flat round dense color=black"
        )
        ui.label(f"{variant} ({VARIANTS[variant][0]})").classes("text-weight-bold")
        ui.button(icon="chevron_right", on_click=lambda: go(next_v, scenario)).props(
            "flat round dense color=black"
        )
        ui.select(
            {k: s.label for k, s in SCENARIOS.items()},
            value=scenario,
            on_change=lambda e: go(variant, e.value),
        ).props("dense options-dense borderless").classes(
            "q-ml-sm sq-proto-select"
        ).style("min-width: 230px")
    ui.add_css(
        ".sq-proto-select .q-field__native, .sq-proto-select .q-field__append .q-icon"
        " { color: #111 !important; }"
    )
    ui.keyboard(
        on_key=lambda e: (
            go(prev_v, scenario)
            if e.key.arrow_left and e.action.keydown and not e.action.repeat
            else go(next_v, scenario)
            if e.key.arrow_right and e.action.keydown and not e.action.repeat
            else None
        ),
        ignore=["input", "select", "button", "textarea"],
    )
