"""Relay engine service — async wrapper around sp-rtk-base-relay RelayEngine.

Provides lifecycle management (start/stop), status queries, and
dynamic destination management with async bridging via
``asyncio.to_thread()`` for blocking RelayEngine methods.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Literal, NamedTuple, Protocol

from pydantic import ValidationError
from sp_rtk_base_relay import (
    FrameSubscription,
    RelayEngine,
    RelayEvent,
    RelayStatus,
)
from sp_rtk_base_relay.config import DestinationConfig, InputConfig
from sp_rtk_base_relay.exceptions import ConfigurationError, ServiceError

from sp_rtk_base.models.config_models import InputProfile
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.relay_events import EventStream, RelayEvents

logger = logging.getLogger(__name__)

# Labels passed by callers so journal/Loki readers can see who/what
# initiated each lifecycle transition.  Free-form string accepted at
# runtime, but these are the conventional values:
#
#   "auto-start" — services.init_services on app boot
#   "api"        — POST /api/relay/start or /stop (manual UI or curl)
#   "shutdown"   — app.shutdown_services on graceful exit
#   "handoff"    — device → relay handoff flow (api/device.py)
#   "unknown"    — fallback for callers that don't pass a value
RelayTrigger = Literal["auto-start", "api", "shutdown", "handoff", "unknown"]


def _format_bytes(n: int) -> str:
    """Format a byte count for log readability (B / KB / MB / GB)."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _format_uptime(seconds: float) -> str:
    """Format uptime as a human-readable duration for log output."""
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
    d = total // 86400
    h = (total % 86400) // 3600
    return f"{d}d {h:02d}h"


def _summarise_input(input_config: InputConfig) -> str:
    """Render an input source into a one-token-ish summary for logs.

    Examples:
        bluetooth(28:cd:c1:…)   for Bluetooth input
        tcp(127.0.0.1:5015)     for TCP input
        serial(/dev/ttyACM0)    for serial input

    Keeps the log line scannable without dumping the whole config tree.
    """
    src = input_config.source
    cfg: dict[str, Any] = input_config.config or {}
    if src == "bluetooth":
        addr = cfg.get("mac_address") or cfg.get("address") or "?"
        return f"bluetooth({addr})"
    if src == "tcp":
        host = cfg.get("host", "?")
        port = cfg.get("port", "?")
        return f"tcp({host}:{port})"
    if src == "serial":
        port = cfg.get("port", "?")
        return f"serial({port})"
    return str(src)


CONSOLE_CONNECTED_MESSAGE = (
    "The console is connected to the receiver. Disconnect the console or "
    "use Hand off to start the relay."
)


class RelayStartRefusedError(ServiceError):
    """Start did not run, and nothing was touched.

    ``code`` is what a client branches on: 409 is shared with unrelated
    refusals (ADR 0002).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


ALREADY_RUNNING_MESSAGE = "Relay engine is already running"
NO_INPUT_MESSAGE = "No input source configured. Configure an input source first."
NO_DESTINATIONS_MESSAGE = "No enabled destinations configured."


class SavedStart(NamedTuple):
    """The saved configuration, translated into what the Relay runs."""

    input: InputConfig
    destinations: list[DestinationConfig]


class FrameSubscriber(Protocol):
    """A Frame subscriber: reads the Relay's input Frames while it runs.

    Told about every start (with a fresh, unfiltered Frame subscription for
    that engine run) and every stop.  Signal Quality is the one subscriber.
    """

    def relay_started(self, subscription: FrameSubscription) -> None: ...

    def relay_stopped(self) -> None: ...


class RelayService:
    """Async adapter for sp-rtk-base-relay's threaded RelayEngine.

    All blocking ``RelayEngine`` methods are wrapped with
    ``asyncio.to_thread()`` so they can be called safely from
    async FastAPI handlers.

    The engine is created lazily on the first ``start_relay()`` call
    and recreated if the input configuration changes.

    Args:
        config_service: Where :meth:`start_saved` reads the saved
            Input profile and outputs from.
    """

    def __init__(self, config_service: ConfigService | None = None) -> None:
        self._config_service = config_service
        self._engine: RelayEngine | None = None
        self._input_config: InputConfig | None = None
        # Captured at each successful start so the stop log can include
        # uptime + final throughput totals (the engine's own metrics
        # are gone after stop()).  Reset in start_relay; consumed in
        # stop_relay.
        self._start_monotonic: float | None = None
        self._start_trigger: str | None = None
        self._frame_subscriber: FrameSubscriber | None = None
        self._console_connected_check: Callable[[], bool] | None = None
        self._events = RelayEvents()

    def set_console_check(self, check: Callable[[], bool]) -> None:
        """Set a callback that says whether the console is connected.

        The other half of the console/Relay mutual exclusion: Connect
        refuses while the Relay runs, and Start refuses while the
        console is connected, for every Console link kind.
        """
        self._console_connected_check = check

    def set_frame_subscriber(self, subscriber: FrameSubscriber) -> None:
        """Register the Frame subscriber told about every relay start and stop.

        Subscribing here, rather than in each caller, means every way of
        starting the relay (auto-start, API, dashboard, input page,
        handoff) feeds it.
        """
        self._frame_subscriber = subscriber

    def _notify_subscriber_started(self, engine: RelayEngine) -> None:
        if self._frame_subscriber is None:
            return
        subscription = engine.subscribe_frames()
        try:
            self._frame_subscriber.relay_started(subscription)
        except Exception:
            # Never let a Frame subscriber stop the relay from relaying.
            subscription.close()
            logger.exception("Frame subscriber failed to attach")

    def _notify_subscriber_stopped(self) -> None:
        if self._frame_subscriber is None:
            return
        try:
            self._frame_subscriber.relay_stopped()
        except Exception:
            logger.exception("Frame subscriber failed to detach")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the relay engine is currently running."""
        if self._engine is None:
            return False
        return self._engine.is_running

    @property
    def engine(self) -> RelayEngine | None:
        """Direct access to the underlying RelayEngine (or None)."""
        return self._engine

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_relay(
        self,
        input_config: InputConfig,
        destinations: list[DestinationConfig] | None = None,
        trigger: str = "unknown",
        *,
        refuse_while_console_connected: bool = True,
    ) -> None:
        """Start the relay engine.

        Creates a new ``RelayEngine`` if one does not exist or if the
        input configuration has changed since the last start.

        Args:
            input_config: Input source configuration.
            destinations: Optional initial destinations.
            trigger: Free-form label describing what initiated the
                start (``"auto-start"``, ``"api"``, ``"handoff"``,
                etc.) — surfaced on the journal/Loki log line so
                operators can tell apart who/what kicked off the run.
            refuse_while_console_connected: Refuse while the console is
                connected.  Only auto-start at boot passes ``False``.

        Raises:
            RelayStartRefusedError: ``console_connected`` while the
                console is connected.  Nothing was touched.
            ServiceError: If the engine is already running.
            ConfigurationError: If the configuration is invalid.
        """
        if self._engine is not None and self._engine.is_running:
            raise ServiceError("Relay engine is already running")

        # Before the stale-handle release: over Bluetooth that release
        # cuts a live console link (ConnectionAbortedError 103 on the
        # bench), so a refused Start must touch nothing.
        if (
            refuse_while_console_connected
            and self._console_connected_check is not None
            and self._console_connected_check()
        ):
            raise RelayStartRefusedError("console_connected", CONSOLE_CONNECTED_MESSAGE)

        # Best-effort stale-handle release, on *every* path into the
        # relay.  This used to live in ``init_services`` behind the
        # auto-start check, so the four manual Start paths never got it
        # — which would have made the Verification more forgiving than
        # the Start it predicts, handing out a Green for a Start that
        # then failed on a handle BlueZ was still holding.
        if input_config.source == "bluetooth":
            from sp_rtk_base.services import bluetooth_service

            mac = bluetooth_service.mac_from_input_config(input_config.config)
            if mac is not None:
                try:
                    await bluetooth_service.release_stale_bluetooth_handle(mac)
                except Exception as exc:
                    logger.debug(
                        "Stale-handle release for %s raised (%s); starting anyway",
                        mac,
                        exc,
                    )

        # Recreate engine if input config changed or first start
        if self._engine is None or self._input_config != input_config:
            self._engine = RelayEngine(input_config)
            self._input_config = input_config
            logger.info("Created new RelayEngine with source=%s", input_config.source)

        # Follow before starting so open streams see the start's own events.
        self._events.follow(self._engine)
        try:
            await asyncio.to_thread(self._engine.start, destinations)
        except Exception:
            # Not on cancellation: the start carries on in its thread, so
            # keep following the engine it may yet bring up.
            await asyncio.to_thread(self._events.unfollow)
            raise
        self._notify_subscriber_started(self._engine)

        # Record start state so stop_relay can compose the uptime/totals
        # log line.  Monotonic clock so a wall-clock jump can't poison it.
        self._start_monotonic = time.monotonic()
        self._start_trigger = trigger

        dest_names = [d.name for d in (destinations or [])]
        logger.info(
            "Relay engine started — trigger=%s input=%s destinations=%s",
            trigger,
            _summarise_input(input_config),
            dest_names if dest_names else "[]",
        )

    def check_saved(self, input_profile: InputProfile | None = None) -> SavedStart:
        """Check the saved configuration can start, without starting it.

        Args:
            input_profile: Check this Input profile with the saved outputs
                instead of the saved one: Hand off checks the profile it
                is about to save before it lets go of the console.

        Returns:
            The Relay input and enabled outputs to start with.

        Raises:
            RelayStartRefusedError: ``already_running``, ``no_input``,
                ``no_destinations``, or ``config_invalid`` when a profile
                can't be turned into Relay config (its message is the
                reason).  Nothing was touched.
        """
        if self.is_running:
            raise RelayStartRefusedError("already_running", ALREADY_RUNNING_MESSAGE)
        if self._config_service is None:
            raise ServiceError("RelayService has no ConfigService to read from")
        config = self._config_service.get_config()
        profile = input_profile or config.input
        if profile is None:
            raise RelayStartRefusedError("no_input", NO_INPUT_MESSAGE)
        enabled = [d for d in config.destinations if d.enabled]
        if not enabled:
            raise RelayStartRefusedError("no_destinations", NO_DESTINATIONS_MESSAGE)
        # Running pydantic on each profile is where a saved config that
        # can't run shows up (e.g. an NTRIP v2 output without a username,
        # issue #198).
        try:
            return SavedStart(
                profile.to_relay_config(), [d.to_relay_config() for d in enabled]
            )
        except (ConfigurationError, ValidationError, ValueError) as exc:
            raise RelayStartRefusedError("config_invalid", str(exc)) from exc

    async def start_saved(
        self, trigger: str = "unknown", *, refuse_while_console_connected: bool = True
    ) -> None:
        """Start the Relay from the saved Input profile and enabled outputs.

        The one way every start path starts from saved config.

        Args:
            trigger: Who started it, for the log (see :data:`RelayTrigger`).
            refuse_while_console_connected: As for :meth:`start_relay`.

        Raises:
            RelayStartRefusedError: As for :meth:`check_saved`, or
                ``console_connected``.  Nothing was touched.
            Exception: Whatever bringing the Relay up raised.
        """
        saved = self.check_saved()
        await self.start_relay(
            saved.input,
            saved.destinations,
            trigger,
            refuse_while_console_connected=refuse_while_console_connected,
        )

    async def stop_relay(self, trigger: str = "unknown") -> None:
        """Stop the relay engine.

        Safe to call when already stopped (no-op if engine is None
        or not running).

        Args:
            trigger: Free-form label describing what initiated the
                stop (``"api"``, ``"shutdown"``, ``"handoff"``).
                Logged so operators can tell apart user-initiated
                stops from shutdown-time cleanup.
        """
        if self._engine is None:
            return
        if not self._engine.is_running:
            return

        # Snapshot final throughput totals BEFORE stopping the engine
        # so the log line can include them.  After ``stop()`` returns,
        # ``get_status()`` is None and the counters are gone.
        bytes_in = 0
        chunks_out = 0
        try:
            status = await asyncio.to_thread(self._engine.get_status)
            bytes_in = int(getattr(status, "bytes_received", 0) or 0)
            chunks_out = int(getattr(status, "chunks_distributed", 0) or 0)
        except Exception:
            # Status query is best-effort — never block the stop on it.
            pass

        self._notify_subscriber_stopped()
        try:
            await asyncio.to_thread(self._engine.stop)
        finally:
            await asyncio.to_thread(self._events.unfollow)

        # Compute uptime from start_monotonic if we recorded one.
        uptime_str = "—"
        if self._start_monotonic is not None:
            uptime_str = _format_uptime(time.monotonic() - self._start_monotonic)
        start_trigger = self._start_trigger or "unknown"
        self._start_monotonic = None
        self._start_trigger = None

        logger.info(
            "Relay engine stopped — trigger=%s uptime=%s bytes_in=%s chunks_out=%d "
            "(started by %s)",
            trigger,
            uptime_str,
            _format_bytes(bytes_in),
            chunks_out,
            start_trigger,
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_status(self) -> RelayStatus | None:
        """Get the current relay status snapshot.

        Returns:
            Frozen ``RelayStatus`` snapshot, or None if engine is
            not running.
        """
        if self._engine is None or not self._engine.is_running:
            return None

        try:
            return await asyncio.to_thread(self._engine.get_status)
        except ServiceError:
            # Engine may have stopped between the check and the call
            return None

    # ------------------------------------------------------------------
    # Destination management
    # ------------------------------------------------------------------

    async def add_destination(self, config: DestinationConfig) -> str:
        """Hot-add a destination to the running engine.

        Args:
            config: Destination configuration.

        Returns:
            The destination name.

        Raises:
            ServiceError: If the engine is not running.
            ConfigurationError: If the config is invalid or name is duplicate.
        """
        self._require_running()
        assert self._engine is not None  # guarded by _require_running
        return await asyncio.to_thread(self._engine.add_destination, config)

    async def remove_destination(self, name: str) -> None:
        """Remove a destination from the running engine.

        Args:
            name: Destination name to remove.

        Raises:
            ServiceError: If the engine is not running.
            KeyError: If the destination is not found.
        """
        self._require_running()
        assert self._engine is not None
        await asyncio.to_thread(self._engine.remove_destination, name)

    async def start_destination(self, name: str) -> None:
        """Resume a paused destination.

        Args:
            name: Destination name to start.

        Raises:
            ServiceError: If the engine is not running.
            KeyError: If the destination is not found.
        """
        self._require_running()
        assert self._engine is not None
        await asyncio.to_thread(self._engine.start_destination, name)

    async def stop_destination(self, name: str) -> None:
        """Pause a destination (keeps it registered).

        Args:
            name: Destination name to stop.

        Raises:
            ServiceError: If the engine is not running.
            KeyError: If the destination is not found.
        """
        self._require_running()
        assert self._engine is not None
        await asyncio.to_thread(self._engine.stop_destination, name)

    def get_destination_names(self) -> list[str]:
        """Get names of all registered destinations.

        Returns:
            List of destination names, or empty list if not running.
        """
        if self._engine is None:
            return []
        return self._engine.get_destination_names()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def stream_events(self) -> EventStream:
        """Open a live stream of relay events for one client.

        Call from the event loop the client reads on.  The stream receives
        every event emitted after it was opened, across stops, starts and
        engine replacement, until it is closed.  History is
        :meth:`get_recent_events`.
        """
        return self._events.open()

    def get_recent_events(self, count: int = 50) -> list[dict[str, Any]]:
        """Get recent events from the ring buffer as dicts.

        Args:
            count: Maximum number of events to return.

        Returns:
            List of event dictionaries (oldest first).
        """
        if self._engine is None:
            return []
        events: list[RelayEvent] = self._engine.get_recent_events(count)
        return [asdict(e) for e in events]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_running(self) -> None:
        """Raise ServiceError if the engine is not running.

        Raises:
            ServiceError: If engine is None or not running.
        """
        if self._engine is None or not self._engine.is_running:
            raise ServiceError("Relay engine is not running")
