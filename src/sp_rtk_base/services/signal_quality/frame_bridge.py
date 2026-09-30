"""FrameBridge: hands the Relay's Frames from its thread to the event loop.

The Relay's Frame subscription is synchronous; this reads it on a daemon
thread and schedules each Frame onto the asyncio loop, the same pattern
``EventBridge`` uses for relay events.
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
        """Close the subscription and wait for the thread to exit."""
        self._subscription.close()
        if self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        for frame in self._subscription:  # ends when the subscription closes
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._deliver, frame)
            elif loop is None:
                self._deliver(frame)  # no event loop (e.g. tests)
        logger.debug("FrameBridge thread exiting")
