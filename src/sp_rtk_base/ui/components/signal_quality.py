"""Signal Quality on a page: a detailed card, or a compact chip.

Reads ``SignalQualityService.current(view)`` every :data:`REFRESH_SECONDS`.

- **Card** (detailed): the verdict and its cause, and one meter per
  measure (L1 Band strength, L2 Band strength, Usable satellites) over its
  Poor / Marginal / Good bands; with no data, a grey "No data" and why.
- **Chip** (compact): a coloured pill beside the page title, e.g.
  "Signal Marginal · L2 weak", with the three measures in its tooltip;
  with no data, a grey "Signal: no data" with the reason in its tooltip.

Each page chooses with its own display setting.  Both are built once and
updated in place on each refresh, never cleared and re-created
(rtk_development#177).
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

from nicegui import ui

from sp_rtk_base.models.config_models import SignalDisplay
from sp_rtk_base.models.signal_quality_models import (
    SignalLevel,
    SignalQualityNoData,
    SignalQualityReading,
)
from sp_rtk_base.services.signal_quality.monitor import (
    L1_GOOD_DBHZ,
    L1_MARGINAL_DBHZ,
    L2_GOOD_DBHZ,
    L2_MARGINAL_DBHZ,
    SATELLITES_GOOD,
    SATELLITES_MARGINAL,
)
from sp_rtk_base.services.signal_quality.service import SignalQualityService, View
from sp_rtk_base.ui.components.level import LEVEL_COLOR
from sp_rtk_base.ui.components.status_card import (
    Tooltip,
    set_text,
    set_visible,
    swap_class,
)

REFRESH_SECONDS = 2.0

# Meter scales: wide enough to show clear sky and a failing antenna.
_DBHZ_SCALE = (30.0, 56.0)
_SATELLITE_SCALE = (0.0, 32.0)


def signal_quality_heading(
    title: str, service: SignalQualityService, view: View, display: SignalDisplay
) -> None:
    """The page title, with Signal Quality as a chip beside it or a card below it."""
    with ui.row().classes("items-center gap-4 q-mb-md"):
        ui.label(title).classes("text-h4 text-white")
        if display == "compact":
            signal_quality_chip(service, view)
    if display == "detailed":
        signal_quality_card(service, view)


def _chip_look(reading: SignalQualityReading) -> tuple[str, str, str]:
    """The chip's colour, text and tooltip for *reading*."""
    if isinstance(reading, SignalQualityNoData):
        return "grey-7", "Signal: no data", reading.reason
    text = f"Signal {reading.level}" + (f" · {reading.cause}" if reading.cause else "")
    tooltip = " · ".join(
        (
            f"L1 {_dbhz(reading.l1_strength_dbhz)}",
            f"L2 {_dbhz(reading.l2_strength_dbhz)}",
            f"{reading.usable_satellites} satellites",
        )
    )
    return LEVEL_COLOR[reading.level], text, tooltip


def signal_quality_chip(service: SignalQualityService, view: View) -> None:
    """Render the compact Signal Quality chip for *view* and keep it current."""
    color, text, tooltip = _chip_look(service.current(view))
    state = {"color": color}
    with ui.element("div").props("data-testid=signal-quality-chip"):
        pill = ui.element("div").classes(f"rounded-borders q-px-md q-py-xs bg-{color}")
        with pill:
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("satellite_alt").classes("text-white")
                label = ui.label(text).classes("text-white text-body2 text-weight-bold")
            tip = Tooltip(tooltip)

    def refresh() -> None:
        new_color, new_text, new_tooltip = _chip_look(service.current(view))
        swap_class(pill, f"bg-{state['color']}", f"bg-{new_color}")
        state["color"] = new_color
        set_text(label, new_text)
        tip.update(new_tooltip)

    ui.timer(REFRESH_SECONDS, refresh)


def _dbhz(value: float | None) -> str:
    return "no signal" if value is None else f"{value:.1f} dB-Hz"


class _Header:
    """The card's verdict badge and its cause."""

    def __init__(self) -> None:
        self._no_data = ui.badge("No data", color="grey-7").classes(
            "text-body2 q-px-sm"
        )
        self._badge = ui.badge("", color="grey-7").classes("text-body1 q-px-md q-py-xs")
        self._cause = ui.label("")
        self._color = "grey-7"
        self._cause.classes(f"text-{self._color}")

    def update(self, reading: SignalQualityReading) -> None:
        no_data = isinstance(reading, SignalQualityNoData)
        set_visible(self._no_data, no_data)
        set_visible(self._badge, not no_data)
        if isinstance(reading, SignalQualityNoData):
            set_text(self._cause, "")
            set_visible(self._cause, False)
            return
        color = LEVEL_COLOR[reading.level]
        if self._badge.text != reading.level:
            self._badge.text = reading.level
        if color != self._color:
            self._badge.props(f"color={color}")
            swap_class(self._cause, f"text-{self._color}", f"text-{color}")
            self._color = color
        set_text(self._cause, reading.cause or "")
        set_visible(self._cause, bool(reading.cause))


class _Meter:
    """One measure: its value, over Poor / Marginal / Good bands with a marker."""

    def __init__(
        self,
        label: str,
        scale: tuple[float, float],
        bands: tuple[float, float],
        unit: str,
        decimals: int = 1,
    ) -> None:
        self._scale = scale
        self._unit = unit
        self._decimals = decimals
        self._color = "grey-5"
        self._left: str | None = None
        marginal, good = self._pct(bands[0]), self._pct(bands[1])
        self._column = ui.column().classes("col-grow gap-1").style("min-width: 180px")
        with self._column:
            with ui.row().classes("items-baseline justify-between w-full"):
                ui.label(label).classes("text-caption text-grey-5")
                self._value = ui.label("").classes(
                    f"text-{self._color} text-weight-bold"
                )
            with (
                ui.element("div")
                .classes("relative-position w-full")
                .style("height: 10px")
            ):
                ui.element("div").classes("absolute-full rounded-borders").style(
                    "opacity: .5; background: linear-gradient(to right, "
                    f"#c10015 0 {marginal}%, #f2c037 {marginal}% {good}%, "
                    f"#21ba45 {good}% 100%)"
                )
                self._marker = ui.element("div").classes("absolute bg-white")
                self._marker.set_visibility(False)

    def set_visibility(self, visible: bool) -> None:
        set_visible(self._column, visible)

    def _pct(self, x: float) -> float:
        lo, hi = self._scale
        return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100))

    def update(self, value: float | None, level: SignalLevel) -> None:
        text = (
            "no signal"
            if value is None
            else f"{value:.{self._decimals}f} {self._unit}".strip()
        )
        set_text(self._value, text)
        color = LEVEL_COLOR[level]
        swap_class(self._value, f"text-{self._color}", f"text-{color}")
        self._color = color
        if value is not None:
            left = f"calc({self._pct(value)}% - 2px)"
            if left != self._left:
                self._marker.style(
                    replace=f"left: {left}; top: -3px; width: 4px; "
                    "height: 16px; border-radius: 2px"
                )
                self._left = left
        set_visible(self._marker, value is not None)


class _Body:
    """The card's body: three meters, or why there is no data."""

    def __init__(self) -> None:
        self._no_data = ui.row().classes("items-center gap-2")
        with self._no_data:
            ui.icon("info").classes("text-grey-5")
            self._reason = ui.label("").classes("text-grey-4")
        self._l1 = _Meter(
            "L1 Band strength",
            _DBHZ_SCALE,
            (L1_MARGINAL_DBHZ, L1_GOOD_DBHZ),
            "dB-Hz",
        )
        self._l2 = _Meter(
            "L2 Band strength",
            _DBHZ_SCALE,
            (L2_MARGINAL_DBHZ, L2_GOOD_DBHZ),
            "dB-Hz",
        )
        self._satellites = _Meter(
            "Usable satellites",
            _SATELLITE_SCALE,
            (float(SATELLITES_MARGINAL), float(SATELLITES_GOOD)),
            "",
            decimals=0,
        )
        self._meters_shown: bool | None = None

    def _show_meters(self, shown: bool) -> None:
        if shown == self._meters_shown:
            return
        set_visible(self._no_data, not shown)
        for meter in (self._l1, self._l2, self._satellites):
            meter.set_visibility(shown)
        self._meters_shown = shown

    def update(self, reading: SignalQualityReading) -> None:
        if isinstance(reading, SignalQualityNoData):
            set_text(self._reason, reading.reason)
            self._show_meters(False)
            return
        self._l1.update(reading.l1_strength_dbhz, reading.l1_level)
        self._l2.update(reading.l2_strength_dbhz, reading.l2_level)
        self._satellites.update(
            float(reading.usable_satellites), reading.satellites_level
        )
        self._show_meters(True)


def signal_quality_card(service: SignalQualityService, view: View) -> None:
    """Render the Signal Quality card for *view* and keep it current."""
    with (
        ui.card()
        .classes("w-full q-pa-md q-mb-md")
        .props("data-testid=signal-quality-card")
    ):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Signal Quality").classes("text-h6 text-white")
            with ui.row().classes("items-center gap-2"):
                header = _Header()
        ui.separator()
        with ui.row().classes("w-full gap-6 q-mt-sm"):
            body = _Body()

    def refresh() -> None:
        reading = service.current(view)
        header.update(reading)
        body.update(reading)

    refresh()
    ui.timer(REFRESH_SECONDS, refresh)
