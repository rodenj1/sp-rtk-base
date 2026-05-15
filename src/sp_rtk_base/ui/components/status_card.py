"""Reusable status card component for displaying key-value metrics."""

# pyright: reportUnknownMemberType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

from nicegui import ui


def status_metric(label: str, value: str, icon: str = "") -> None:
    """Render a single metric with label and value.

    Args:
        label: Metric label text.
        value: Metric value to display.
        icon: Optional material icon name.
    """
    with ui.row().classes("items-center gap-2"):
        if icon:
            ui.icon(icon).classes("text-grey-5 text-lg")
        with ui.column().classes("gap-0"):
            ui.label(label).classes("text-caption text-grey-5")
            ui.label(value).classes("text-body1 text-white font-bold")


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
