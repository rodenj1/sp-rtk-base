"""Reusable status card component for displaying key-value metrics."""

# pyright: reportUnknownMemberType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

from nicegui import ui


def status_metric(
    label: str,
    value: str,
    icon: str = "",
    subvalue: str | None = None,
    *,
    value_color: str = "white",
    tooltip: str | None = None,
    testid: str | None = None,
) -> None:
    """Render a single metric with label and value.

    Args:
        label: Metric label text.
        value: Primary metric value (prominent).
        icon: Optional material icon name.
        subvalue: Optional secondary value rendered small + muted under
            the primary value.  Useful for showing a cumulative total
            beneath a rate, where the rate is the "is it flowing right
            now" signal and the total is just for context.
        value_color: Quasar colour of the primary value.
        tooltip: Optional tooltip over the whole metric.
        testid: Optional ``data-testid`` for the metric's row.
    """
    row = ui.row().classes("items-center gap-2")
    if testid is not None:
        row.props(f"data-testid={testid}")
    with row:
        if icon:
            ui.icon(icon).classes("text-grey-5 text-lg")
        with ui.column().classes("gap-0"):
            ui.label(label).classes("text-caption text-grey-5")
            ui.label(value).classes(f"text-body1 text-{value_color} font-bold")
            if subvalue is not None:
                ui.label(subvalue).classes("text-caption text-grey-6")
        if tooltip is not None:
            ui.tooltip(tooltip)


def status_indicator(running: bool) -> None:
    """Render a colored running/stopped indicator.

    Args:
        running: Whether the relay engine is running.
    """
    color = "green" if running else "red"
    text = "Running" if running else "Stopped"
    with ui.row().classes("items-center gap-2"):
        ui.icon("circle").classes(f"text-{color} text-xs")
        ui.label(text).classes(f"text-{color} text-body1 font-bold")
