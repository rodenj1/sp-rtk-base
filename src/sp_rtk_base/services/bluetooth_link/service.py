"""BluetoothLinkService: feeds the Bluetooth link strength monitor from the Relay.

A background poller reads the Relay status every
:data:`POLL_INTERVAL_SECONDS` and feeds its Input status (``None`` while
the Relay is stopped) to one :class:`BluetoothLinkMonitor`.  Polling in
the background, rather than per page, means every tab shows the same
verdict, already warm when a page opens.  ``current()`` is what the
dashboard (and metrics) read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from sp_rtk_base.models.bluetooth_link_models import BluetoothLinkReading
from sp_rtk_base.services.bluetooth_link.monitor import (
    BluetoothLinkMonitor,
    InputStatusLike,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0


class RelayStatusLike(Protocol):
    """The part of the Relay status the service reads."""

    @property
    def input(self) -> InputStatusLike | None: ...


StatusSource = Callable[[], Awaitable[RelayStatusLike | None]]


class BluetoothLinkService:
    """Bluetooth link strength for the UI: one monitor, fed by the Relay status."""

    def __init__(
        self,
        relay_status: StatusSource,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._relay_status = relay_status
        self._clock = clock
        self._monitor = BluetoothLinkMonitor()
        self._task: asyncio.Task[None] | None = None

    def current(self) -> BluetoothLinkReading | None:
        """The Bluetooth link strength now; ``None`` without a Bluetooth Input."""
        return self._monitor.current(self._clock())

    async def poll_once(self) -> None:
        """Read the Relay status once and feed its Input status to the monitor.

        A status read that fails feeds nothing: the verdict goes stale
        rather than turning into a level.
        """
        try:
            status = await self._relay_status()
        except Exception:
            logger.debug("Relay status read for Bluetooth link failed", exc_info=True)
            return
        self._monitor.observe(
            status.input if status is not None else None, self._clock()
        )

    def start(self) -> None:
        """Start the background poller.  Idempotent."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="sp_rtk_base.bluetooth_link"
            )

    async def stop(self) -> None:
        """Cancel the background poller."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
