"""Event bridge — thread-to-async relay event forwarding.

Consumes ``EventSubscription`` events from the sp-rtk-base-relay
threaded event system and pushes them into an ``asyncio.Queue``
for async consumers (WebSocket handlers, NiceGUI timers, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import asdict
from typing import Any

from sp_rtk_base_relay import EventSubscription, RelayEvent

from sp_rtk_base.services.relay_service import RelayService

logger = logging.getLogger(__name__)


class EventBridge:
    """Bridge between sp-rtk-base-relay's threaded events and async consumers.

    Runs a daemon thread that consumes ``EventSubscription.get_event()``
    (blocking) and pushes events into an ``asyncio.Queue`` that async
    handlers can ``await``.

    Usage::

        bridge = EventBridge()
        bridge.start(relay_service)

        # In async handler:
        event = await bridge.event_queue.get()

        bridge.stop()
    """

    def __init__(self, max_queue_size: int = 200) -> None:
        self._subscription: EventSubscription | None = None
        self._thread: threading.Thread | None = None
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the event bridge thread is active."""
        return self._running

    @property
    def event_queue(self) -> asyncio.Queue[dict[str, Any]]:
        """The async queue that receives serialized relay events."""
        return self._event_queue

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, relay_service: RelayService) -> None:
        """Start the event bridge daemon thread.

        Subscribes to the relay engine's event bus and begins
        forwarding events to the async queue.

        Args:
            relay_service: The relay service to subscribe to.

        Raises:
            RuntimeError: If the bridge is already running or if
                the relay service has no engine.
        """
        if self._running:
            raise RuntimeError("EventBridge is already running")

        subscription = relay_service.subscribe_events()
        if subscription is None:
            raise RuntimeError("Cannot start EventBridge: relay engine not available")

        self._subscription = subscription
        self._running = True

        # Capture the current event loop for cross-thread queue pushing
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        self._thread = threading.Thread(
            target=self._event_loop,
            name="sp-rtk-base-event-bridge",
            daemon=True,
        )
        self._thread.start()
        logger.info("EventBridge started")

    def stop(self) -> None:
        """Stop the event bridge.

        Closes the event subscription and waits for the daemon thread
        to exit. Safe to call when already stopped.
        """
        if not self._running:
            return

        self._running = False

        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
            self._thread = None

        self._loop = None
        logger.info("EventBridge stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _event_loop(
        self,
    ) -> None:
        """Daemon thread: consume events and push to async queue.

        Runs until ``self._running`` is set to False or the
        subscription is closed.
        """
        sub = self._subscription
        if sub is None:
            return

        logger.debug("EventBridge thread started")

        while self._running and not sub.is_closed:
            event = sub.get_event(timeout=1.0)
            if event is None:
                continue

            event_dict = self._serialize_event(event)
            self._push_to_queue(event_dict)

        logger.debug("EventBridge thread exiting")

    def _serialize_event(self, event: RelayEvent) -> dict[str, Any]:
        """Convert a RelayEvent to a JSON-compatible dictionary.

        Args:
            event: The relay event to serialize.

        Returns:
            Dictionary representation of the event.
        """
        return asdict(event)

    def _push_to_queue(self, event_dict: dict[str, Any]) -> None:
        """Push an event dict to the async queue (thread-safe).

        If the queue is full, the oldest event is discarded to make room.
        Uses ``call_soon_threadsafe`` when an event loop is available.

        Args:
            event_dict: Serialized event data.
        """
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._enqueue_nowait, event_dict)
        else:
            # Fallback: direct put (works in tests without event loop)
            self._enqueue_nowait(event_dict)

    def _enqueue_nowait(self, event_dict: dict[str, Any]) -> None:
        """Put event on queue, discarding if full.

        Args:
            event_dict: Serialized event data.
        """
        try:
            self._event_queue.put_nowait(event_dict)
        except asyncio.QueueFull:
            # Discard oldest to make room
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._event_queue.put_nowait(event_dict)
            except asyncio.QueueFull:
                logger.warning("Event queue still full — event dropped")
