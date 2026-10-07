"""FrameBridge: hands the Relay's Frames from its thread to the event loop.

The Relay's Frame subscription is synchronous; this reads it on a daemon
thread and schedules each Frame onto the asyncio loop, the same pattern
``EventFanout`` uses for relay events.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from sp_rtk_base_relay import Frame, FrameSubscription

logger = logging.getLogger(__name__)


class FrameBridge:
    """Delivers every Frame of one subscription to *deliver*, on the loop."""

    def __init__(
        self,
        subscription: FrameSubscription,
        deliver: Callable[[Frame], None],
        loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        self._subscription = subscription
        self._deliver = deliver
        self._loop = loop
        self._thread = threading.Thread(
            target=self._run, name="sp-rtk-base-frame-bridge", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Close the subscription and let the thread exit.

        Closing wakes the thread at once, so the join is near-instant; it
        is bounded so it can never hold up the event loop for long.
        """
        self._subscription.close()
        if self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def _run(self) -> None:
        # Frames are only ever delivered on the event loop, never on this
        # thread; with no running loop there is nobody to deliver to.
        for frame in self._subscription:  # ends when the subscription closes
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._deliver, frame)
        logger.debug("FrameBridge thread exiting")
