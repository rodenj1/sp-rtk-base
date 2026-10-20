"""NetworkLinkService: the Network link sampler.

A background task reads the host every :data:`SAMPLE_INTERVAL_SECONDS`
(off the event loop, so a slow read never blocks the UI, ``/metrics`` or
the Relay) and feeds the reading to one :class:`NetworkLinkMonitor`.
Sampling in the background, rather than per page, means every viewer
sees the same snapshot, already warm.  ``current()`` is what the
dashboard (and metrics) read; it never samples.

Cancelling the task can't stop a read already running in its worker
thread, so the host is only read and closed under one lock: ``stop()``
waits for a read in flight before closing, and a closed host is never
read (or reopened) again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable

from sp_rtk_base.models.network_link_models import HostReading, NetworkLinkSnapshot
from sp_rtk_base.services.network_link.monitor import (
    SAMPLE_INTERVAL_SECONDS,
    NetworkLinkMonitor,
)
from sp_rtk_base.services.network_link.reader import HostReaderLike

logger = logging.getLogger(__name__)


class NetworkLinkService:
    """Network link health for the UI: one monitor, fed by the host reader."""

    def __init__(
        self,
        host: HostReaderLike,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._host = host
        self._clock = clock
        self._monitor = NetworkLinkMonitor()
        self._task: asyncio.Task[None] | None = None
        self._host_lock = threading.Lock()
        self._closed = False

    def current(self) -> NetworkLinkSnapshot:
        """The Network link snapshot now."""
        return self._monitor.snapshot(self._clock())

    async def sample_once(self) -> None:
        """Read the host once, off the event loop, and feed the monitor.

        A read that raises is a sample in which every source failed.  After
        :meth:`stop` it reads nothing.
        """
        if self._closed:
            return
        try:
            reading = await asyncio.to_thread(self._read)
        except Exception:
            if self._closed:
                return
            logger.debug("Network link read failed", exc_info=True)
            reading = HostReading()
        self._monitor.observe(reading, self._clock())

    def start(self) -> None:
        """Start the background sampler.  Idempotent."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="sp_rtk_base.network_link"
            )

    async def stop(self) -> None:
        """Cancel the background sampler and close the host reader, once
        any read in flight has finished."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await asyncio.to_thread(self._close)

    def _read(self) -> HostReading:
        with self._host_lock:
            if self._closed:
                raise RuntimeError("the Network link sampler is stopped")
            return self._host.read()

    def _close(self) -> None:
        with self._host_lock:
            if not self._closed:
                self._closed = True
                self._host.close()

    async def _run(self) -> None:
        while True:
            await self.sample_once()
            await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
