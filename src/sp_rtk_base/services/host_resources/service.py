"""HostResourcesService: the host sampler.

A background task reads the host every :data:`SAMPLE_INTERVAL_SECONDS`
(off the event loop, so a slow read never blocks the UI, ``/metrics`` or
the Relay) and feeds the reading to one :class:`HostResourcesMonitor`.
``current()`` is what ``/metrics`` reads; it never samples.

It is separate from the Network link sampler, so a failing read in one
never touches the other's freshness.  Its reads open nothing that needs
closing, so stopping only cancels the task.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from sp_rtk_base.models.host_resources_models import (
    HostResourcesReading,
    HostResourcesSnapshot,
)
from sp_rtk_base.services.host_resources.monitor import (
    SAMPLE_INTERVAL_SECONDS,
    HostResourcesMonitor,
)
from sp_rtk_base.services.host_resources.reader import HostResourcesReaderLike

logger = logging.getLogger(__name__)


class HostResourcesService:
    """Host resources for ``/metrics``: one monitor, fed by the host reader."""

    def __init__(
        self,
        host: HostResourcesReaderLike,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._host = host
        self._clock = clock
        self._monitor = HostResourcesMonitor()
        self._task: asyncio.Task[None] | None = None

    def current(self) -> HostResourcesSnapshot:
        """The Host resources snapshot now."""
        return self._monitor.snapshot(self._clock())

    async def sample_once(self) -> None:
        """Read the host once, off the event loop, and feed the monitor.

        A read that raises is a sample in which every reading failed.
        """
        try:
            reading = await asyncio.to_thread(self._host.read)
        except Exception:
            logger.debug("Host resources read failed", exc_info=True)
            reading = HostResourcesReading()
        self._monitor.observe(reading, self._clock())

    def start(self) -> None:
        """Start the background sampler.  Idempotent."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="sp_rtk_base.host_resources"
            )

    async def stop(self) -> None:
        """Cancel the background sampler."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await self.sample_once()
            await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
