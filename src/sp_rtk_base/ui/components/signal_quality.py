"""Signal Quality card: the verdict, its cause and one meter per measure.

Reads ``SignalQualityService.current()`` every
:data:`REFRESH_SECONDS` and renders either the verdict (with L1 Band
strength, L2 Band strength and Usable satellites meters over their
Poor / Marginal / Good bands) or a grey "No data" with the reason.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

from nicegui import ui

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
from sp_rtk_base.services.signal_quality.service import SignalQualityService

REFRESH_SECONDS = 2.0

LEVEL_COLOR: dict[SignalLevel, str] = {
    "Good": "positive",
    "Marginal": "warning",
    "Poor": "negative",
}

# Meter scales: wide enough to show clear sky and a failing antenna.
_DBHZ_SCALE = (30.0, 56.0)
_SATELLITE_SCALE = (0.0, 32.0)


def signal_quality_card(service: SignalQualityService) -> None:
    """Render the Signal Quality card and keep it current."""
    with (
        ui.card()
        .classes("w-full q-pa-md q-mb-md")
        .props("data-testid=signal-quality-card")
    ):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Signal Quality").classes("text-h6 text-white")
            header = ui.row().classes("items-center gap-2")
        ui.separator()
        body = ui.row().classes("w-full gap-6 q-mt-sm")

    def refresh() -> None:
        reading = service.current()
        header.clear()
        body.clear()
        with header:
            _render_header(reading)
        with body:
            _render_body(reading)

    refresh()
    ui.timer(REFRESH_SECONDS, refresh)


def _render_header(reading: SignalQualityReading) -> None:
    if isinstance(reading, SignalQualityNoData):
        ui.badge("No data", color="grey-7").classes("text-body2 q-px-sm")
        return
    color = LEVEL_COLOR[reading.level]
    ui.badge(reading.level, color=color).classes("text-body1 q-px-md q-py-xs")
    if reading.cause:
        ui.label(reading.cause).classes(f"text-{color}")


def _render_body(reading: SignalQualityReading) -> None:
    if isinstance(reading, SignalQualityNoData):
        with ui.row().classes("items-center gap-2"):
            ui.icon("info").classes("text-grey-5")
            ui.label(reading.reason).classes("text-grey-4")
        return
    _meter(
        "L1 Band strength",
        reading.l1_strength_dbhz,
        reading.l1_level,
        _DBHZ_SCALE,
        (L1_MARGINAL_DBHZ, L1_GOOD_DBHZ),
        "dB-Hz",
    )
    _meter(
        "L2 Band strength",
        reading.l2_strength_dbhz,
        reading.l2_level,
        _DBHZ_SCALE,
        (L2_MARGINAL_DBHZ, L2_GOOD_DBHZ),
        "dB-Hz",
    )
    _meter(
        "Usable satellites",
        float(reading.usable_satellites),
        reading.satellites_level,
        _SATELLITE_SCALE,
        (float(SATELLITES_MARGINAL), float(SATELLITES_GOOD)),
        "",
        decimals=0,
    )


def _meter(
    label: str,
    value: float | None,
    level: SignalLevel,
    scale: tuple[float, float],
    bands: tuple[float, float],
    unit: str,
    decimals: int = 1,
) -> None:
    """One measure: its value, over Poor / Marginal / Good bands with a marker."""
    lo, hi = scale

    def pct(x: float) -> float:
        return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100))

    marginal, good = pct(bands[0]), pct(bands[1])
    text = "no signal" if value is None else f"{value:.{decimals}f} {unit}".strip()
    with ui.column().classes("col-grow gap-1").style("min-width: 180px"):
        with ui.row().classes("items-baseline justify-between w-full"):
            ui.label(label).classes("text-caption text-grey-5")
            ui.label(text).classes(f"text-{LEVEL_COLOR[level]} text-weight-bold")
        with (
            ui.element("div").classes("relative-position w-full").style("height: 10px")
        ):
            ui.element("div").classes("absolute-full rounded-borders").style(
                "opacity: .5; background: linear-gradient(to right, "
                f"#c10015 0 {marginal}%, #f2c037 {marginal}% {good}%, "
                f"#21ba45 {good}% 100%)"
            )
            if value is not None:
                ui.element("div").classes("absolute bg-white").style(
                    f"left: calc({pct(value)}% - 2px); top: -3px; width: 4px; "
                    "height: 16px; border-radius: 2px"
                )
