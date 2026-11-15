"""Dashboard page — relay status overview.

Shows relay running state, input source status, throughput metrics,
start/stop controls, and a live event log with per-destination details.
Focused exclusively on relay monitoring — GPS device status is on the
Survey-In and Advanced GPS pages.
"""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# NiceGUI elements have partially unknown types.

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nicegui import ui

from sp_rtk_base import services as services_mod
from sp_rtk_base.models.bluetooth_link_models import BluetoothLinkReading
from sp_rtk_base.services import (
    get_bluetooth_link_service,
    get_config_service,
    get_network_link_service,
    get_relay_service,
    get_signal_quality_service,
)
from sp_rtk_base.services.bluetooth_link.monitor import is_bluetooth_input
from sp_rtk_base.services.relay_service import RelayStartRefusedError
from sp_rtk_base.ui.components.bluetooth_link import BluetoothLinkRow
from sp_rtk_base.ui.components.network_card import NetworkCard
from sp_rtk_base.ui.components.signal_quality import signal_quality_heading
from sp_rtk_base.ui.components.status_card import (
    StatusIndicator,
    StatusMetric,
    set_text,
    set_visible,
    swap_class,
)
from sp_rtk_base.ui.formatting import format_byte_rate as _format_byte_rate
from sp_rtk_base.ui.layout import page_layout
from sp_rtk_base.ui.start_status import needs_setup, start_failure_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelayControlState:
    """How the single Start/Stop button should render this tick.

    Pulled out as a pure data shape so the decision logic can be
    unit-tested without spinning up NiceGUI.  The page render path
    just plugs each field into the live ``ui.button`` /
    ``ui.label`` widgets.
    """

    primary_text: str
    primary_icon: str
    primary_color: str
    primary_enabled: bool
    config_message: str | None


def _compute_relay_control_state(
    *, is_running: bool, has_input: bool, enabled_destination_count: int
) -> RelayControlState:
    """Decide what the relay-control button should look like.

    When the relay is running, the button is always "Stop" (red) and
    enabled regardless of config — the operator may need to stop a
    running engine even with a momentarily-empty destination list.

    When the relay is stopped, the button is "Start" (green) but
    disabled with an explanatory message if either an input source
    isn't configured yet or zero destinations are enabled — both are
    hard preconditions for the relay engine's ``start()`` to succeed.
    """
    if is_running:
        return RelayControlState(
            primary_text="Stop",
            primary_icon="stop",
            primary_color="red",
            primary_enabled=True,
            config_message=None,
        )
    if not has_input:
        return RelayControlState(
            primary_text="Start",
            primary_icon="play_arrow",
            primary_color="green",
            primary_enabled=False,
            config_message=(
                "Configure an input source on the Input page before starting."
            ),
        )
    if enabled_destination_count == 0:
        return RelayControlState(
            primary_text="Start",
            primary_icon="play_arrow",
            primary_color="green",
            primary_enabled=False,
            config_message=(
                "Add at least one enabled destination on the Outputs page "
                "before starting."
            ),
        )
    return RelayControlState(
        primary_text="Start",
        primary_icon="play_arrow",
        primary_color="green",
        primary_enabled=True,
        config_message=None,
    )


def _format_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _format_count_rate(per_second: float, unit: str = "/s") -> str:
    """Format a count-per-second value (messages, frames, chunks)."""
    if per_second >= 100:
        return f"{per_second:.0f}{unit}"
    if per_second >= 10:
        return f"{per_second:.1f}{unit}"
    return f"{per_second:.2f}{unit}"


def _format_uptime(seconds: float | None) -> str:
    """Format uptime in days/hours/minutes/seconds.

    Examples (the relay typically runs for days at a time, so the
    leading unit is whichever is largest):

        12s
        5m 23s
        4h 23m
        12d 4h
        1y 23d
    """
    if seconds is None:
        return "--"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        m, s = divmod(total, 60)
        return f"{m}m {s:02d}s"
    if total < 86400:
        h = total // 3600
        m = (total % 3600) // 60
        return f"{h}h {m:02d}m"
    if total < 86400 * 365:
        d = total // 86400
        h = (total % 86400) // 3600
        return f"{d}d {h:02d}h"
    y = total // (86400 * 365)
    d = (total % (86400 * 365)) // 86400
    return f"{y}y {d:02d}d"


# --- Dashboard sections, built once and updated in place ---
#
# Every element NiceGUI creates and later deletes leaves a little memory
# behind in the browser, so a dashboard that cleared and rebuilt its
# cards on every status refresh grew without bound while it stayed on
# screen, until the Pi ran short of memory and the page reloaded itself
# (rtk_development#177).  Each section below is built once when the page
# loads; a refresh changes text, colours and visibility, and creates or
# deletes elements only when the page's structure changes.


def _rate_text(rate: float | None, fmt: Callable[[float], str]) -> str:
    """A per-second rate, or "—" before there is a baseline."""
    return fmt(rate) if rate is not None else "—"


def _caption(text: str = "") -> ui.label:
    """An orange warning caption under a card's metrics, hidden at first."""
    label = ui.label(text).classes("text-orange text-caption q-mt-xs")
    label.set_visibility(False)
    return label


def _show_caption(label: ui.label, text: str | None) -> None:
    """Show *text* in *label*, or hide (and empty) it for ``None``."""
    set_text(label, text or "")
    set_visible(label, text is not None)


class _RunningCard:
    """A metrics card that says "Not running" when there is nothing to show."""

    def __init__(self, title: str) -> None:
        ui.label(title).classes("text-subtitle2 text-grey-4")
        self._not_running = ui.label("Not running").classes("text-grey-6")
        self._running_parts: list[ui.element | StatusMetric] = []

    def _set_running(self, running: bool) -> None:
        set_visible(self._not_running, not running)
        for part in self._running_parts:
            part.set_visibility(running)


class _InputCard(_RunningCard):
    """The Input Source card."""

    def __init__(self) -> None:
        super().__init__("Input Source")
        self._connected = ui.label("").classes("text-red")
        self._connected_color = "red"
        self._source = StatusMetric("Source", "", "input")
        self._bluetooth = BluetoothLinkRow(None)
        # Per-second rate is the primary signal — 0 B/s reads as "nothing
        # flowing" immediately, where a stuck running total just looks like
        # a big number.  Total is shown small underneath as context.
        self._received = StatusMetric("Received", "—", "download", subvalue="")
        self._messages = StatusMetric("Messages", "—", "message", subvalue="")
        self._no_data = _caption()
        self._reconnects = _caption()
        self._running_parts = [
            self._connected,
            self._source,
            self._received,
            self._messages,
        ]
        self.update(None)

    def update(
        self,
        inp: Any | None,
        *,
        bluetooth: BluetoothLinkReading | None = None,
        bytes_rate: float | None = None,
        msgs_rate: float | None = None,
    ) -> None:
        """Show the Relay's input status (``None``: not running)."""
        self._set_running(inp is not None)
        if inp is None:
            self._bluetooth.set_visibility(False)
            _show_caption(self._no_data, None)
            _show_caption(self._reconnects, None)
            return
        connected: bool = getattr(inp, "connected", False)
        color = "green" if connected else "red"
        set_text(self._connected, "Connected" if connected else "Disconnected")
        swap_class(self._connected, f"text-{self._connected_color}", f"text-{color}")
        self._connected_color = color
        self._source.update(getattr(inp, "source_type", "unknown"))
        if is_bluetooth_input(inp):
            self._bluetooth.update(bluetooth)
            self._bluetooth.set_visibility(True)
        else:
            self._bluetooth.set_visibility(False)
        bytes_in = int(getattr(inp, "bytes_received", 0))
        self._received.update(
            _rate_text(bytes_rate, _format_byte_rate),
            f"total: {_format_bytes(bytes_in)}",
        )
        msgs_in = int(getattr(inp, "messages_received", 0))
        self._messages.update(
            _rate_text(msgs_rate, lambda r: _format_count_rate(r, " msg/s")),
            f"total: {msgs_in}",
        )
        secs = getattr(inp, "seconds_since_last_data", -1.0)
        no_data = f"⚠ No data for {secs:.0f}s" if secs > 10 else None
        reconnects: int = getattr(inp, "reconnect_attempts", 0)
        reconnect_text = (
            f"Reconnect attempts: {reconnects}"
            if secs < 0 and not connected and reconnects > 0
            else None
        )
        _show_caption(self._no_data, no_data)
        _show_caption(self._reconnects, reconnect_text)


class _ThroughputCard(_RunningCard):
    """The Throughput card."""

    def __init__(self) -> None:
        super().__init__("Throughput")
        self._bytes = StatusMetric("Bytes In", "—", "download", subvalue="")
        self._chunks = StatusMetric("Chunks Out", "—", "upload", subvalue="")
        self._warnings = _caption()
        self._running_parts = [self._bytes, self._chunks]
        self.update(None)

    def update(
        self,
        status: Any | None,
        *,
        bytes_rate: float | None = None,
        chunks_rate: float | None = None,
    ) -> None:
        """Show the Relay's throughput (``None``: not running)."""
        self._set_running(status is not None)
        if status is None:
            _show_caption(self._warnings, None)
            return
        self._bytes.update(
            _rate_text(bytes_rate, _format_byte_rate),
            f"total: {_format_bytes(int(status.bytes_received))}",
        )
        self._chunks.update(
            _rate_text(chunks_rate, _format_count_rate),
            f"total: {int(status.chunks_distributed)}",
        )
        warnings = status.no_data_warnings
        _show_caption(
            self._warnings,
            f"⚠ {warnings} no-data warnings" if warnings and warnings > 0 else None,
        )


class _DestinationsCard(_RunningCard):
    """The Destinations summary card."""

    def __init__(self) -> None:
        super().__init__("Destinations")
        self._active = StatusMetric("Active", "", "output")
        self._errors = _caption()
        self._dropped = _caption()
        self._running_parts = [self._active]
        self.update(None)

    def update(self, status: Any | None) -> None:
        """Show the destinations' summary (``None``: not running)."""
        self._set_running(status is not None)
        if status is None:
            _show_caption(self._errors, None)
            _show_caption(self._dropped, None)
            return
        self._active.update(
            f"{status.active_destination_count}/{status.total_destination_count}"
        )
        total_errors = sum(getattr(d, "errors", 0) for d in status.destinations)
        total_dropped = sum(
            getattr(d, "messages_dropped", 0) for d in status.destinations
        )
        _show_caption(
            self._errors,
            f"⚠ {total_errors} total errors" if total_errors > 0 else None,
        )
        _show_caption(
            self._dropped,
            f"⚠ {total_dropped} messages dropped" if total_dropped > 0 else None,
        )


class _DestinationRow:
    """One destination's row in Destination details."""

    def __init__(self, name: str) -> None:
        self.row = (
            ui.row()
            .classes("items-center gap-4 q-py-xs w-full")
            .props(f"data-testid=destination-row-{name}")
        )
        self._color = "red"
        with self.row:
            self._dot = ui.icon("circle").classes("text-red").style("font-size: 10px")
            ui.label(name).classes("text-grey-2").style("min-width: 120px")
            self._bytes = ui.label("").classes("text-grey-4 text-caption")
            self._errors = ui.badge("", color="red").props("outline")
            self._dropped = ui.badge("", color="orange").props("outline")
            self._queue = ui.label("").classes("text-grey-5 text-caption")
        for hidden in (self._errors, self._dropped, self._queue):
            hidden.set_visibility(False)

    def update(self, dest: Any) -> None:
        connected: bool = getattr(dest, "connected", False)
        errors: int = getattr(dest, "errors", 0)
        dropped: int = getattr(dest, "messages_dropped", 0)
        queue: int = getattr(dest, "queue_depth", 0)
        color = "green" if connected else "red"
        swap_class(self._dot, f"text-{self._color}", f"text-{color}")
        self._color = color
        set_text(self._bytes, _format_bytes(getattr(dest, "bytes_sent", 0)))
        for badge, count, text in (
            (self._errors, errors, f"{errors} err"),
            (self._dropped, dropped, f"{dropped} drop"),
        ):
            text = text if count > 0 else ""
            if badge.text != text:
                badge.text = text
            set_visible(badge, count > 0)
        _show_caption(self._queue, f"Q:{queue}" if queue > 0 else None)


class _DestinationDetails:
    """The per-destination rows, keyed by destination name.

    A destination's row is created when it first appears in the Relay
    status and deleted when it goes; the rest are updated in place.
    """

    def __init__(self) -> None:
        self._card = ui.card().classes("w-full q-pa-md")
        with self._card:
            ui.label("Destination Details").classes(
                "text-subtitle2 text-grey-4 q-mb-sm"
            )
            self._rows_box = ui.column().classes("w-full gap-0")
        self._card.set_visibility(False)
        self._rows: dict[str, _DestinationRow] = {}

    def update(self, destinations: list[Any]) -> None:
        """Show *destinations* (empty: hide the card)."""
        names = [str(getattr(d, "name", "unknown")) for d in destinations]
        for gone in [n for n in self._rows if n not in names]:
            self._rows.pop(gone).row.delete()
        for index, (name, dest) in enumerate(zip(names, destinations, strict=True)):
            row = self._rows.get(name)
            if row is None:
                with self._rows_box:
                    row = self._rows[name] = _DestinationRow(name)
            if list(self._rows_box.default_slot.children).index(row.row) != index:
                row.row.move(target_index=index)
            row.update(dest)
        set_visible(self._card, bool(names))


class _ErrorBanner:
    """The "Issues Detected" banner; its lines change only with the issues."""

    def __init__(self) -> None:
        self._card = (
            ui.card()
            .classes("w-full q-pa-sm")
            .style("background-color: #3d1515; border-left: 4px solid #ff4444")
        )
        with self._card:
            ui.label("⚠ Issues Detected").classes("text-subtitle2 text-red-3")
            self._lines = ui.column().classes("w-full gap-0")
        self._card.set_visibility(False)
        self._errors: list[str] = []

    def update(self, errors: list[str]) -> None:
        """Show *errors* (none: hide the banner)."""
        if errors != self._errors:
            self._lines.clear()
            with self._lines:
                for err in errors:
                    ui.label(f"• {err}").classes("text-caption text-red-4 q-ml-sm")
            self._errors = list(errors)
        set_visible(self._card, bool(errors))


@ui.page("/", dark=True)
def dashboard_page() -> None:
    """Render the dashboard page."""
    relay = get_relay_service()
    config_svc = get_config_service()
    bluetooth_link = get_bluetooth_link_service()
    network_link = get_network_link_service()

    with page_layout("Dashboard"):
        signal_quality_heading(
            "Dashboard",
            get_signal_quality_service(),
            "dashboard",
            config_svc.get_settings().dashboard_signal_display,
        )

        # --- Relay control bar ---
        #
        # A single toggle button whose text/icon/color swap based on
        # ``relay.is_running``.  When the relay is stopped AND a hard
        # precondition (input source, at least one enabled
        # destination) isn't satisfied, the button is disabled and
        # ``config_status_label`` explains which page to visit.
        with ui.card().classes("w-full q-pa-md"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.row().classes("items-center gap-4"):
                    indicator = StatusIndicator(relay.is_running)
                    uptime_label = ui.label("").classes("text-grey-4")
                    uptime_label.set_visibility(False)
                primary_btn = ui.button(
                    "Start", icon="play_arrow", on_click=lambda: _toggle_relay()
                ).props("color=green")
            # Shown only when the Start button is greyed out because
            # the saved config is incomplete (no input source / no
            # enabled destinations).  Hidden once the precondition is
            # met or whenever the relay is already running.
            config_status_label = ui.label("").classes(
                "text-warning text-caption q-mt-sm"
            )
            config_status_label.set_visibility(False)
            # Persistent error banner for Start failures.  The toast
            # alone fades in ~3 s; operators reported missing the
            # error context and ending up confused about why the
            # relay wasn't running.  Hidden by default, populated by
            # _start_relay's except branch, cleared on next click.
            start_error_label = ui.label("").classes(
                "text-negative text-caption q-mt-sm"
            )
            start_error_label.set_visibility(False)

        # --- Metrics cards ---
        with ui.row().classes("w-full gap-4 q-mt-md sp-metric-row"):
            with ui.card().classes("flex-1 q-pa-md"):
                input_view = _InputCard()
            with ui.card().classes("flex-1 q-pa-md"):
                throughput_view = _ThroughputCard()
            with ui.card().classes("flex-1 q-pa-md"):
                destinations_view = _DestinationsCard()

        # --- Network card: the Network links and the Uplink ---
        # The sampler's snapshot, so every viewer sees the same throughput
        # (never this page's own rate tracking).
        with (
            ui.card()
            .classes("w-full q-pa-md q-mt-md")
            .props("data-testid=network-card")
        ):
            network_view = NetworkCard(network_link.current())

        # --- Destination details ---
        with ui.column().classes("w-full gap-2 q-mt-md"):
            details_view = _DestinationDetails()

        # --- Auto-start banner (in_progress / failure surface) ---
        #
        # Populated by ``_refresh_auto_start_banner`` on each status
        # tick from the module-level ``auto_start_status`` snapshot
        # in :mod:`sp_rtk_base.services`.  Dismissible by the user
        # for the current browser session.
        auto_start_banner = ui.column().classes("w-full q-mt-md")
        auto_start_dismissed_states: set[str] = set()
        auto_start_shown: dict[str, tuple[str, int, str | None] | None] = {
            "value": None
        }

        # --- Error banner ---
        with ui.column().classes("w-full q-mt-md"):
            error_view = _ErrorBanner()

        # --- Event log ---
        with ui.card().classes("w-full q-pa-md q-mt-md"):
            ui.label("Recent Events").classes("text-h6 text-white")
            ui.separator()
            event_log = (
                ui.column()
                .classes("w-full q-mt-sm")
                .style("max-height: 300px; overflow-y: auto")
                .props("data-testid=event-log")
            )

        # --- Rate-tracking state (closure-scoped, per browser session) ---
        #
        # Stores the previous (counter, timestamp) for each running
        # total we want to convert into a per-second rate.  Mutated
        # by _do_refresh_status on each tick.  Reset when the relay
        # stops or uptime regresses (engine restart) so we don't
        # report nonsense after a counter discontinuity.
        rate_prev: dict[str, tuple[float, float]] = {}
        last_uptime: dict[str, float | None] = {"value": None}

        def _rate(key: str, current: float, now: float) -> float | None:
            """Compute per-second rate vs the previous sample.

            Returns None on the very first sample (no baseline yet).
            Caller is responsible for treating None as "0 B/s / no
            data" — but we don't substitute 0 here because the very
            first poll genuinely has no baseline and showing 0 would
            be a lie.
            """
            prev = rate_prev.get(key)
            rate_prev[key] = (current, now)
            if prev is None:
                return None
            prev_value, prev_ts = prev
            elapsed = now - prev_ts
            if elapsed <= 0:
                return None
            delta = current - prev_value
            if delta < 0:
                # Counter went backwards — engine restarted between
                # ticks.  Drop this delta; next tick re-baselines.
                return None
            return delta / elapsed

        # --- State update functions ---
        async def _refresh_status() -> None:
            """Poll relay status and update UI."""
            try:
                return await _do_refresh_status()
            except RuntimeError:
                return  # Elements deleted — user navigated away

        async def _do_refresh_status() -> None:
            """Inner status refresh (may raise RuntimeError if page left)."""
            try:
                status = await relay.get_status()
            except Exception:
                logger.exception("Failed to get relay status")
                status = None

            running = relay.is_running
            now = time.monotonic()

            # If the engine isn't running, or uptime went backwards
            # (engine restarted between polls), drop all rate
            # baselines so we don't show nonsense once it comes back.
            current_uptime = status.uptime_seconds if status else None
            prev_uptime = last_uptime["value"]
            if not running or (
                prev_uptime is not None
                and current_uptime is not None
                and current_uptime < prev_uptime
            ):
                rate_prev.clear()
            last_uptime["value"] = current_uptime

            # Update the single toggle button + config-precondition label.
            try:
                cfg = config_svc.get_config()
                has_input = cfg.input is not None
                enabled_dest_count = sum(1 for d in cfg.destinations if d.enabled)
            except Exception:
                logger.exception("Failed to read config for dashboard button state")
                has_input = False
                enabled_dest_count = 0
            control = _compute_relay_control_state(
                is_running=running,
                has_input=has_input,
                enabled_destination_count=enabled_dest_count,
            )
            primary_btn.text = control.primary_text
            primary_btn.props(
                f"color={control.primary_color} icon={control.primary_icon}"
            )
            primary_btn.set_enabled(control.primary_enabled)
            if control.config_message is not None:
                config_status_label.text = f"⚠ {control.config_message}"
                config_status_label.set_visibility(True)
            else:
                config_status_label.set_visibility(False)

            # Each section is updated in place: see the note above
            # ``_RunningCard``.
            indicator.update(running)
            uptime = status.uptime_seconds if status else None
            if uptime is not None:
                set_text(uptime_label, f"Uptime: {_format_uptime(uptime)}")
            set_visible(uptime_label, uptime is not None)

            network_view.update(network_link.current())

            inp: Any = status.input if status else None
            if inp:
                input_view.update(
                    inp,
                    bluetooth=bluetooth_link.current(),
                    bytes_rate=_rate(
                        "input_bytes", int(getattr(inp, "bytes_received", 0)), now
                    ),
                    msgs_rate=_rate(
                        "input_msgs", int(getattr(inp, "messages_received", 0)), now
                    ),
                )
            else:
                input_view.update(None)

            if status:
                throughput_view.update(
                    status,
                    bytes_rate=_rate("thru_bytes", int(status.bytes_received), now),
                    chunks_rate=_rate("chunks", int(status.chunks_distributed), now),
                )
            else:
                throughput_view.update(None)
            destinations_view.update(status)
            details_view.update(list(status.destinations) if status else [])

            # Auto-start lifecycle banner (checked every tick so
            # transitions in_progress → succeeded / failed are visible).
            _refresh_auto_start_banner()

            # Error banner for critical issues
            errors: list[str] = []
            if status:
                if status.input and not getattr(status.input, "connected", True):
                    errors.append("Input source is disconnected")
                for dest in status.destinations:
                    last_err = getattr(dest, "last_error", None)
                    if last_err:
                        errors.append(f"{dest.name}: {last_err}")
            error_view.update(errors)

        def _refresh_auto_start_banner() -> None:
            """Render the auto-start lifecycle banner.

            Surfaces transient retry progress (yellow) and terminal
            failure (red) for the post-power-cycle path where the
            relay engine couldn't reach its input source on the first
            attempt.  Dismissible per-state so an operator can ack
            a stale "failed" banner without it reappearing every
            polling tick.
            """
            snapshot = services_mod.auto_start_status
            state = snapshot.state
            # Skipped starts need no banner: the disabled Start button
            # already says what to configure.
            hidden = state in (
                "idle",
                "succeeded",
                "succeeded_user",
                "skipped_no_input",
                "skipped_no_destinations",
            ) or (state in auto_start_dismissed_states)
            # Redrawn only when what it shows changes (see ``_RunningCard``).
            shown = None if hidden else (state, snapshot.attempts, snapshot.last_error)
            if shown == auto_start_shown["value"]:
                return
            auto_start_shown["value"] = shown
            auto_start_banner.clear()
            if shown is None:
                return

            total = len(services_mod.AUTO_START_BACKOFF_SECONDS)

            def _dismiss(current_state: str = state) -> None:
                auto_start_dismissed_states.add(current_state)
                auto_start_shown["value"] = None
                auto_start_banner.clear()

            with auto_start_banner:
                if state == "in_progress":
                    with (
                        ui.card()
                        .classes("w-full q-pa-sm")
                        .style(
                            "background-color: #3d3815; border-left: 4px solid #f5b800"
                        )
                    ):
                        with ui.row().classes("items-center justify-between w-full"):
                            with ui.row().classes("items-center gap-2"):
                                ui.spinner(size="sm", color="yellow-7")
                                ui.label(
                                    f"Auto-starting relay… "
                                    f"(attempt {snapshot.attempts}/{total})"
                                ).classes("text-subtitle2 text-yellow-3")
                            ui.button(icon="close", on_click=_dismiss).props(
                                "flat dense round"
                            )
                        if snapshot.last_error:
                            ui.label(f"Last error: {snapshot.last_error}").classes(
                                "text-caption text-yellow-4 q-ml-md"
                            )
                elif state in ("failed_after_retries", "failed_config"):
                    title = (
                        f"Auto-start failed after {snapshot.attempts} attempts"
                        if state == "failed_after_retries"
                        else "Auto-start blocked by config error"
                    )
                    hint = (
                        "Click Start to retry, or check the Input and Outputs "
                        "pages for misconfiguration."
                        if state == "failed_after_retries"
                        else "Fix the config (Input or Outputs page) and restart "
                        "the service."
                    )
                    with (
                        ui.card()
                        .classes("w-full q-pa-sm")
                        .style(
                            "background-color: #3d1515; border-left: 4px solid #ff4444"
                        )
                    ):
                        with ui.row().classes("items-center justify-between w-full"):
                            ui.label(f"⚠ {title}").classes("text-subtitle2 text-red-3")
                            ui.button(icon="close", on_click=_dismiss).props(
                                "flat dense round"
                            )
                        if snapshot.last_error:
                            ui.label(snapshot.last_error).classes(
                                "text-caption text-red-4 q-ml-md"
                            )
                        ui.label(hint).classes(
                            "text-caption text-grey-4 q-ml-md q-mt-xs"
                        )

        def _render_event(evt: dict[str, object]) -> None:
            """Render a single event entry in the log."""
            etype: str = str(evt.get("event_type", ""))
            msg: str = str(evt.get("message", ""))
            with ui.row().classes("items-center gap-2 q-py-xs"):
                badge_color = "blue-grey"
                if "error" in etype.lower():
                    badge_color = "red"
                elif "warning" in etype.lower():
                    badge_color = "orange"
                elif "connect" in etype.lower():
                    badge_color = "green"
                ui.badge(etype).props(f"outline color={badge_color}")
                ui.label(msg).classes("text-grey-3 text-caption")

        async def _backfill_events() -> None:
            """Load recent events on page load for initial context."""
            try:
                events = relay.get_recent_events(20)
                event_log.clear()
                with event_log:
                    if not events:
                        # The live script removes this by its class when
                        # the first live event arrives.
                        ui.label("No events yet").classes(
                            "text-grey-6 no-events-placeholder"
                        )
                    else:
                        for evt in reversed(events):
                            _render_event(evt)
            except RuntimeError:
                pass

        def _setup_event_websocket() -> None:
            """Set up a client-side WebSocket for real-time event streaming."""
            js_code = """
            (function() {
                const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                const wsUrl = proto + '//' + window.location.host + (window.path_prefix || '') + '/api/events/ws';
                let ws = null;
                let reconnectTimer = null;

                function connect() {
                    ws = new WebSocket(wsUrl);

                    ws.onmessage = function(event) {
                        try {
                            const data = JSON.parse(event.data);
                            if (data.type === 'ping') return;

                            const etype = data.event_type || '';
                            const msg = data.message || '';

                            let badgeColor = '#607D8B';
                            if (etype.toLowerCase().includes('error')) badgeColor = '#F44336';
                            else if (etype.toLowerCase().includes('warning')) badgeColor = '#FF9800';
                            else if (etype.toLowerCase().includes('connect')) badgeColor = '#4CAF50';

                            const CONTAINER_ID = '%CONTAINER_ID%';
                            const container = document.getElementById(CONTAINER_ID);
                            if (!container) return;

                            // Remove "No events yet" placeholder
                            const placeholder = container.querySelector('.no-events-placeholder');
                            if (placeholder) placeholder.remove();

                            // Event text can come from outside (a Bluetooth
                            // device name, a caster's reply): set it as text,
                            // never as markup (issue #51).
                            const row = document.createElement('div');
                            row.className = 'row items-center gap-2 q-py-xs';
                            const badge = document.createElement('span');
                            badge.className = 'q-badge q-badge--outline';
                            badge.style.borderColor = badgeColor;
                            badge.style.color = badgeColor;
                            badge.textContent = etype;
                            const text = document.createElement('span');
                            text.className = 'text-grey-3 text-caption';
                            text.textContent = msg;
                            row.append(badge, text);

                            container.insertBefore(row, container.firstChild);

                            // Keep max 50 entries
                            while (container.children.length > 50) {
                                container.removeChild(container.lastChild);
                            }
                        } catch(e) {}
                    };

                    ws.onclose = function() {
                        if (!reconnectTimer) {
                            reconnectTimer = setTimeout(function() {
                                reconnectTimer = null;
                                connect();
                            }, 3000);
                        }
                    };

                    ws.onerror = function() { ws.close(); };
                }

                connect();

                // Cleanup when page unloads
                window.addEventListener('beforeunload', function() {
                    if (ws) ws.close();
                    if (reconnectTimer) clearTimeout(reconnectTimer);
                });
            })();
            """
            container_id = f"c{event_log.id}"
            event_log._props["id"] = container_id  # type: ignore[attr-defined]
            ui.run_javascript(js_code.replace("%CONTAINER_ID%", container_id))

        async def _start_relay() -> None:
            """Start the relay engine using saved config."""
            # Clear any previous start-failure banner before retrying.
            start_error_label.text = ""
            start_error_label.set_visibility(False)
            try:
                await relay.start_saved(trigger="ui")
            except RelayStartRefusedError as exc:
                if needs_setup(exc):
                    ui.notify(start_failure_text(exc), type="warning")
                    return
                friendly = start_failure_text(exc)
            except Exception as exc:
                logger.exception("Failed to start relay")
                friendly = start_failure_text(exc)
            else:
                ui.notify("Relay started", type="positive")
                await _refresh_status()
                return
            # Persistent banner stays visible until the next Start click —
            # the toast still fires for consistency but the banner is the
            # primary signal.
            start_error_label.text = friendly
            start_error_label.set_visibility(True)
            ui.notify(friendly, type="negative")

        async def _stop_relay() -> None:
            """Stop the relay engine."""
            try:
                await relay.stop_relay(trigger="ui")
                ui.notify("Relay stopped", type="info")
                await _refresh_status()
            except Exception as exc:
                logger.exception("Failed to stop relay")
                ui.notify(f"Failed to stop relay: {exc}", type="negative")

        async def _toggle_relay() -> None:
            """Dispatch to start/stop based on current running state."""
            if relay.is_running:
                await _stop_relay()
            else:
                await _start_relay()

        # Initial refresh + periodic status timer
        ui.timer(
            interval=config_svc.get_settings().status_poll_interval,
            callback=_refresh_status,
        )

        # Event log: backfill recent events, then stream via WebSocket
        ui.timer(interval=0, callback=_backfill_events, once=True)
        ui.timer(interval=0.5, callback=_setup_event_websocket, once=True)
