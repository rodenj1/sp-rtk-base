"""Reusable status card components for displaying key-value metrics.

Each component is built once and then updated in place: a page that
refreshes on a timer calls ``update(...)`` rather than clearing and
re-creating it.  Every element NiceGUI creates and later deletes leaves a
little memory behind in the browser, so a page left on screen that
rebuilt its metrics every refresh grew without bound (rtk_development#177).
"""

# pyright: reportUnknownMemberType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

from nicegui import ui


def set_text(label: ui.label, text: str) -> None:
    """Set *label*'s text, sending nothing when it hasn't changed."""
    if label.text != text:
        label.text = text


def swap_class(element: ui.element, old: str, new: str) -> None:
    """Replace class *old* with *new* on *element* (no-op if equal)."""
    if old != new:
        element.classes(remove=old, add=new)


def set_visible(element: ui.element, visible: bool) -> None:
    """Show or hide *element*, sending nothing when it hasn't changed."""
    if element.visible != visible:
        element.set_visibility(visible)


class Tooltip:
    """A tooltip whose text can change, and that can be switched off."""

    def __init__(self, text: str | None) -> None:
        self._tooltip = ui.tooltip(text or "")
        self._text = text
        if text is None:
            self._tooltip.props("disable")

    def update(self, text: str | None) -> None:
        """Show *text* on hover, or no tooltip at all for ``None``."""
        if text == self._text:
            return
        if text is None:
            self._tooltip.props("disable")
        else:
            if self._text is None:
                self._tooltip.props(remove="disable")
            self._tooltip.text = text
        self._text = text


class StatusMetric:
    """A single metric with label and value, updatable in place.

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
        with_tooltip: Create the tooltip even while *tooltip* is ``None``,
            so a later ``update`` can give it one.
    """

    def __init__(
        self,
        label: str,
        value: str,
        icon: str = "",
        subvalue: str | None = None,
        *,
        value_color: str = "white",
        tooltip: str | None = None,
        testid: str | None = None,
        with_tooltip: bool = False,
    ) -> None:
        self.row = ui.row().classes("items-center gap-2")
        if testid is not None:
            self.row.props(f"data-testid={testid}")
        self._value_color = value_color
        self._tooltip: Tooltip | None = None
        with self.row:
            if icon:
                ui.icon(icon).classes("text-grey-5 text-lg")
            with ui.column().classes("gap-0"):
                self.label = ui.label(label).classes("text-caption text-grey-5")
                self._value = ui.label(value).classes(
                    f"text-body1 text-{value_color} font-bold"
                )
                self._sub = ui.label(subvalue or "").classes("text-caption text-grey-6")
                if subvalue is None:
                    self._sub.set_visibility(False)
            if tooltip is not None or with_tooltip:
                self._tooltip = Tooltip(tooltip)

    def update(
        self,
        value: str,
        subvalue: str | None = None,
        *,
        value_color: str = "white",
        tooltip: str | None = None,
    ) -> None:
        """Show new values; elements are changed, never re-created."""
        set_text(self._value, value)
        swap_class(self._value, f"text-{self._value_color}", f"text-{value_color}")
        self._value_color = value_color
        # Emptied when hidden, so no stale value lingers in the page.
        set_text(self._sub, subvalue or "")
        set_visible(self._sub, subvalue is not None)
        if self._tooltip is not None:
            self._tooltip.update(tooltip)

    def set_visibility(self, visible: bool) -> None:
        """Show or hide the whole metric."""
        set_visible(self.row, visible)


def status_metric(
    label: str,
    value: str,
    icon: str = "",
    subvalue: str | None = None,
    *,
    value_color: str = "white",
    tooltip: str | None = None,
    testid: str | None = None,
) -> StatusMetric:
    """Render a single metric with label and value (see :class:`StatusMetric`)."""
    return StatusMetric(
        label,
        value,
        icon,
        subvalue,
        value_color=value_color,
        tooltip=tooltip,
        testid=testid,
    )


class StatusIndicator:
    """A coloured running/stopped indicator, updatable in place."""

    def __init__(self, running: bool) -> None:
        self._running = running
        color, text = self._look(running)
        with ui.row().classes("items-center gap-2"):
            self._icon = ui.icon("circle").classes(f"text-{color} text-xs")
            self._label = ui.label(text).classes(f"text-{color} text-body1 font-bold")

    @staticmethod
    def _look(running: bool) -> tuple[str, str]:
        return ("green", "Running") if running else ("red", "Stopped")

    def update(self, running: bool) -> None:
        """Show *running*."""
        if running == self._running:
            return
        old, _ = self._look(self._running)
        new, text = self._look(running)
        swap_class(self._icon, f"text-{old}", f"text-{new}")
        swap_class(self._label, f"text-{old}", f"text-{new}")
        self._label.text = text
        self._running = running


def status_indicator(running: bool) -> StatusIndicator:
    """Render a coloured running/stopped indicator.

    Args:
        running: Whether the relay engine is running.
    """
    return StatusIndicator(running)
