"""Live relay events for async consumers, one stream per client.

``RelayService.stream_events()`` hands each consumer (each Dashboard's
``/api/events/ws`` connection) its own :class:`EventStream`.  Behind them
an :class:`EventFanout` holds the only subscription to the running
engine's event bus and copies every event to every open stream.

The fan-out is attached by ``RelayService`` only while the Relay runs
and a stream is open, so no caller starts or stops anything, a replaced
engine is picked up on the next start, and nothing is buffered for a
client that hasn't connected yet (issue #49).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import asdict
from types import TracebackType
from typing import Any

from sp_rtk_base_relay import EventSubscription

logger = logging.getLogger(__name__)

# Events a client may fall behind by before its oldest are dropped.
STREAM_QUEUE_SIZE = 200

# How often the fan-out thread checks whether it has been told to stop.
_POLL_SECONDS = 0.2


class EventStream:
    """One client's live relay events, as JSON-ready dicts.

    Use as a context manager, or call :meth:`close` when the client goes
    away.  The buffer is bounded: a client that stops reading loses its
    oldest events and never holds up the Relay or other clients.
    """

    def __init__(
        self,
        on_close: Callable[[EventStream], None],
        max_queue_size: int = STREAM_QUEUE_SIZE,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._on_close = on_close
        self._closed = False

    async def get(self) -> dict[str, Any]:
        """Wait for the next event."""
        return await self._queue.get()

    def close(self) -> None:
        """Stop receiving events.  Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._on_close(self)

    def offer(self, event: dict[str, Any]) -> None:
        """Queue an event from any thread."""
        try:
            self._loop.call_soon_threadsafe(self._put, event)
        except RuntimeError:
            pass  # The client's event loop has closed.

    def _put(self, event: dict[str, Any]) -> None:
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(event)

    def __enter__(self) -> EventStream:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class EventFanout:
    """Copies one engine's events to every open :class:`EventStream`."""

    def __init__(
        self,
        subscription: EventSubscription,
        streams: Callable[[], list[EventStream]],
    ) -> None:
        self._subscription = subscription
        self._streams: Callable[[], list[EventStream]] = streams
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="sp-rtk-base-event-fanout", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Deliver what the engine has already emitted, then unsubscribe.

        Blocks for up to a poll interval while the thread exits.
        """
        self._stopping.set()
        self._thread.join(timeout=2 * _POLL_SECONDS + 1.0)
        for event in self._subscription.drain():
            self._deliver(asdict(event))
        self._subscription.close()

    def abandon(self) -> None:
        """Unsubscribe without waiting; for when no stream is left.

        The thread may still be holding an event for up to a poll
        interval; it delivers to no one rather than to a stream opened
        since, which a newer fan-out already serves.
        """
        self._streams = lambda: []
        self._stopping.set()
        self._subscription.close()

    def _run(self) -> None:
        while not self._stopping.is_set():
            event = self._subscription.get_event(timeout=_POLL_SECONDS)
            if event is not None:
                self._deliver(asdict(event))

    def _deliver(self, event: dict[str, Any]) -> None:
        for stream in self._streams():
            stream.offer(event)
