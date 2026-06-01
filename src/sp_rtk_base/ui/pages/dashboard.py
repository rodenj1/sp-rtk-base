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
from typing import Any

from nicegui import ui

from sp_rtk_base import services as services_mod
from sp_rtk_base.services import get_config_service, get_relay_service
from sp_rtk_base.ui.components.status_card import status_indicator, status_metric
from sp_rtk_base.ui.layout import page_layout

logger = logging.getLogger(__name__)


def _format_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _format_byte_rate(per_second: float) -> str:
    """Format a byte-rate value as '12.3 KB/s' (or B/s, MB/s, GB/s).

    Rate is the primary 'is data flowing right now' signal — totals
    keep climbing for days and stop being informative once the
    instance has been up long enough that everything looks like a
    big number.  0 B/s reads as 'nothing flowing' immediately;
    a stuck total reads as 'don't know'.
    """
    if per_second < 1024:
        return f"{per_second:.0f} B/s"
    if per_second < 1024 * 1024:
        return f"{per_second / 1024:.1f} KB/s"
    if per_second < 1024 * 1024 * 1024:
        return f"{per_second / (1024 * 1024):.1f} MB/s"
    return f"{per_second / (1024 * 1024 * 1024):.2f} GB/s"


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


@ui.page("/", dark=True)
def dashboard_page() -> None:
    """Render the dashboard page."""
    relay = get_relay_service()
    config_svc = get_config_service()

    with page_layout("Dashboard"):
        ui.label("Dashboard").classes("text-h4 text-white q-mb-md")

        # --- Relay control bar ---
        with ui.card().classes("w-full q-pa-md"):
            with ui.row().classes("items-center justify-between w-full"):
                status_container = ui.row().classes("items-center gap-4")
                with ui.row().classes("gap-2"):
                    start_btn = ui.button(
                        "Start", icon="play_arrow", on_click=lambda: _start_relay()
                    ).props("color=green")
                    stop_btn = ui.button(
                        "Stop", icon="stop", on_click=lambda: _stop_relay()
                    ).props("color=red")
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
            input_card = ui.card().classes("flex-1 q-pa-md")
            throughput_card = ui.card().classes("flex-1 q-pa-md")
            dest_card = ui.card().classes("flex-1 q-pa-md")

        # --- Destination details ---
        dest_details_container = ui.column().classes("w-full gap-2 q-mt-md")

        # --- Auto-start banner (in_progress / failure surface) ---
        #
        # Populated by ``_refresh_auto_start_banner`` on each status
        # tick from the module-level ``auto_start_status`` snapshot
        # in :mod:`sp_rtk_base.services`.  Dismissible by the user
        # for the current browser session.
        auto_start_banner = ui.column().classes("w-full q-mt-md")
        auto_start_dismissed_states: set[str] = set()

        # --- Error banner ---
        error_banner = ui.column().classes("w-full q-mt-md")

        # --- Event log ---
        with ui.card().classes("w-full q-pa-md q-mt-md"):
            ui.label("Recent Events").classes("text-h6 text-white")
            ui.separator()
            event_log = (
                ui.column()
                .classes("w-full q-mt-sm")
                .style("max-height: 300px; overflow-y: auto")
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

            # Update control buttons
            start_btn.set_enabled(not running)
            stop_btn.set_enabled(running)

            # Status indicator
            status_container.clear()
            with status_container:
                status_indicator(running)
                if status and status.uptime_seconds is not None:
                    ui.label(
                        f"Uptime: {_format_uptime(status.uptime_seconds)}"
                    ).classes("text-grey-4")

            # Input card
            input_card.clear()
            with input_card:
                ui.label("Input Source").classes("text-subtitle2 text-grey-4")
                if status and status.input:
                    inp: Any = status.input
                    connected: bool = getattr(inp, "connected", False)
                    color = "green" if connected else "red"
                    ui.label(f"{'Connected' if connected else 'Disconnected'}").classes(
                        f"text-{color}"
                    )
                    status_metric(
                        "Source", getattr(inp, "source_type", "unknown"), "input"
                    )

                    # Per-second rate is the primary signal — 0 B/s
                    # reads as "nothing flowing" immediately, where a
                    # stuck running total just looks like a big number.
                    # Total is shown small underneath as context.
                    bytes_in = int(getattr(inp, "bytes_received", 0))
                    bytes_rate = _rate("input_bytes", bytes_in, now)
                    status_metric(
                        "Received",
                        (
                            _format_byte_rate(bytes_rate)
                            if bytes_rate is not None
                            else "—"
                        ),
                        "download",
                        subvalue=f"total: {_format_bytes(bytes_in)}",
                    )
                    msgs_in = int(getattr(inp, "messages_received", 0))
                    msgs_rate = _rate("input_msgs", msgs_in, now)
                    status_metric(
                        "Messages",
                        (
                            _format_count_rate(msgs_rate, " msg/s")
                            if msgs_rate is not None
                            else "—"
                        ),
                        "message",
                        subvalue=f"total: {msgs_in}",
                    )
                    secs = getattr(inp, "seconds_since_last_data", -1.0)
                    if secs >= 0:
                        if secs > 10:
                            ui.label(f"⚠ No data for {secs:.0f}s").classes(
                                "text-orange text-caption q-mt-xs"
                            )
                    elif not connected:
                        reconnects: int = getattr(inp, "reconnect_attempts", 0)
                        if reconnects > 0:
                            ui.label(f"Reconnect attempts: {reconnects}").classes(
                                "text-orange text-caption q-mt-xs"
                            )
                else:
                    ui.label("Not running").classes("text-grey-6")

            # Throughput card
            throughput_card.clear()
            with throughput_card:
                ui.label("Throughput").classes("text-subtitle2 text-grey-4")
                if status:
                    bytes_thru = int(status.bytes_received)
                    bytes_thru_rate = _rate("thru_bytes", bytes_thru, now)
                    status_metric(
                        "Bytes In",
                        (
                            _format_byte_rate(bytes_thru_rate)
                            if bytes_thru_rate is not None
                            else "—"
                        ),
                        "download",
                        subvalue=f"total: {_format_bytes(bytes_thru)}",
                    )
                    frames = int(status.frames_parsed)
                    frames_rate = _rate("frames", frames, now)
                    status_metric(
                        "Frames Parsed",
                        (
                            _format_count_rate(frames_rate)
                            if frames_rate is not None
                            else "—"
                        ),
                        "analytics",
                        subvalue=f"total: {frames}",
                    )
                    chunks = int(status.chunks_distributed)
                    chunks_rate = _rate("chunks", chunks, now)
                    status_metric(
                        "Chunks Out",
                        (
                            _format_count_rate(chunks_rate)
                            if chunks_rate is not None
                            else "—"
                        ),
                        "upload",
                        subvalue=f"total: {chunks}",
                    )
                    if status.no_data_warnings and status.no_data_warnings > 0:
                        ui.label(
                            f"⚠ {status.no_data_warnings} no-data warnings"
                        ).classes("text-orange text-caption q-mt-xs")
                else:
                    ui.label("Not running").classes("text-grey-6")

            # Destinations summary card
            dest_card.clear()
            with dest_card:
                ui.label("Destinations").classes("text-subtitle2 text-grey-4")
                if status:
                    status_metric(
                        "Active",
                        f"{status.active_destination_count}/{status.total_destination_count}",
                        "output",
                    )
                    total_errors = sum(
                        getattr(d, "errors", 0) for d in status.destinations
                    )
                    if total_errors > 0:
                        ui.label(f"⚠ {total_errors} total errors").classes(
                            "text-orange text-caption q-mt-xs"
                        )
                    total_dropped = sum(
                        getattr(d, "messages_dropped", 0) for d in status.destinations
                    )
                    if total_dropped > 0:
                        ui.label(f"⚠ {total_dropped} messages dropped").classes(
                            "text-orange text-caption q-mt-xs"
                        )
                else:
                    ui.label("Not running").classes("text-grey-6")

            # Per-destination details
            dest_details_container.clear()
            if status and status.destinations:
                with dest_details_container:
                    with ui.card().classes("w-full q-pa-md"):
                        ui.label("Destination Details").classes(
                            "text-subtitle2 text-grey-4 q-mb-sm"
                        )
                        for dest in status.destinations:
                            _render_dest_row(dest)

            # Auto-start lifecycle banner (re-rendered every tick so
            # transitions in_progress → succeeded / failed are visible).
            _refresh_auto_start_banner()

            # Error banner for critical issues
            error_banner.clear()
            if status:
                errors: list[str] = []
                if status.input and not getattr(status.input, "connected", True):
                    errors.append("Input source is disconnected")
                for dest in status.destinations:
                    last_err = getattr(dest, "last_error", None)
                    if last_err:
                        errors.append(f"{dest.name}: {last_err}")

                if errors:
                    with error_banner:
                        with (
                            ui.card()
                            .classes("w-full q-pa-sm")
                            .style(
                                "background-color: #3d1515; border-left: 4px solid #ff4444"
                            )
                        ):
                            ui.label("⚠ Issues Detected").classes(
                                "text-subtitle2 text-red-3"
                            )
                            for err in errors:
                                ui.label(f"• {err}").classes(
                                    "text-caption text-red-4 q-ml-sm"
                                )

        def _refresh_auto_start_banner() -> None:
            """Render the auto-start lifecycle banner.

            Surfaces transient retry progress (yellow) and terminal
            failure (red) for the post-power-cycle path where the
            relay engine couldn't reach its input source on the first
            attempt.  Dismissible per-state so an operator can ack
            a stale "failed" banner without it reappearing every
            polling tick.
            """
            auto_start_banner.clear()
            snapshot = services_mod.auto_start_status
            state = snapshot.state
            if state in ("idle", "succeeded", "succeeded_user", "skipped_no_input"):
                return
            if state in auto_start_dismissed_states:
                return

            total = len(services_mod.AUTO_START_BACKOFF_SECONDS)

            def _dismiss(current_state: str = state) -> None:
                auto_start_dismissed_states.add(current_state)
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

        def _render_dest_row(dest: Any) -> None:
            """Render a single destination status row."""
            name: str = getattr(dest, "name", "unknown")
            connected: bool = getattr(dest, "connected", False)
            bytes_sent: int = getattr(dest, "bytes_sent", 0)
            errors: int = getattr(dest, "errors", 0)
            dropped: int = getattr(dest, "messages_dropped", 0)
            queue: int = getattr(dest, "queue_depth", 0)

            color = "green" if connected else "red"
            with ui.row().classes("items-center gap-4 q-py-xs w-full"):
                ui.icon("circle").classes(f"text-{color}").style("font-size: 10px")
                ui.label(name).classes("text-grey-2").style("min-width: 120px")
                ui.label(_format_bytes(bytes_sent)).classes("text-grey-4 text-caption")
                if errors > 0:
                    ui.badge(f"{errors} err", color="red").props("outline")
                if dropped > 0:
                    ui.badge(f"{dropped} drop", color="orange").props("outline")
                if queue > 0:
                    ui.label(f"Q:{queue}").classes("text-grey-5 text-caption")

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
                        ui.label("No events yet").classes("text-grey-6")
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
                const wsUrl = proto + '//' + window.location.host + '/api/events/ws';
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

                            const row = document.createElement('div');
                            row.className = 'row items-center gap-2 q-py-xs';
                            row.innerHTML =
                                '<span class="q-badge q-badge--outline" ' +
                                'style="border-color:' + badgeColor + ';color:' + badgeColor + '">' +
                                etype + '</span>' +
                                '<span class="text-grey-3 text-caption">' + msg + '</span>';

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
                config = config_svc.get_config()
                if config.input is None:
                    ui.notify(
                        "No input source configured — go to Input first", type="warning"
                    )
                    return
                enabled = [d for d in config.destinations if d.enabled]
                if not enabled:
                    ui.notify(
                        "No enabled destinations — add one in Outputs first",
                        type="warning",
                    )
                    return
                input_cfg = config.input.to_relay_config()
                dest_cfgs = [d.to_relay_config() for d in enabled]
                await relay.start_relay(input_cfg, dest_cfgs)
                ui.notify("Relay started", type="positive")
                await _refresh_status()
            except Exception as exc:
                logger.exception("Failed to start relay")
                # Map common ConfigurationError patterns to friendly
                # messages.  Without this the operator sees the raw
                # exception path (e.g. "input.config.port must be an
                # integer between 1 and 65535 | Key: input.config.port")
                # which leaks the internal config tree shape.
                exc_text = str(exc)
                if (
                    "port must be an integer" in exc_text
                    or "input.config.port" in exc_text
                ):
                    friendly = (
                        "Failed to start: TCP input port is not a number. "
                        "Re-save the Input config (Input page) and try again."
                    )
                elif "input.config" in exc_text or "destinations" in exc_text:
                    friendly = (
                        "Failed to start: configuration error.  Check the "
                        "Input and Outputs pages for fields that need "
                        "valid values, then re-save."
                    )
                else:
                    friendly = f"Failed to start relay: {exc}"
                # Persistent banner stays visible until the next
                # Start click — the toast still fires for
                # consistency but the banner is the primary signal.
                start_error_label.text = friendly
                start_error_label.set_visibility(True)
                ui.notify(friendly, type="negative")

        async def _stop_relay() -> None:
            """Stop the relay engine."""
            try:
                await relay.stop_relay()
                ui.notify("Relay stopped", type="info")
                await _refresh_status()
            except Exception as exc:
                logger.exception("Failed to stop relay")
                ui.notify(f"Failed to stop relay: {exc}", type="negative")

        # Initial refresh + periodic status timer
        ui.timer(
            interval=config_svc.get_settings().status_poll_interval,
            callback=_refresh_status,
        )

        # Event log: backfill recent events, then stream via WebSocket
        ui.timer(interval=0, callback=_backfill_events, once=True)
        ui.timer(interval=0.5, callback=_setup_event_websocket, once=True)
