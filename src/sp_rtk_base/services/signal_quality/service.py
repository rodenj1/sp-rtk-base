"""SignalQualityService: feeds the monitor and explains missing data.

Owns one :class:`SignalQualityMonitor` and the survey-in feed: a
background poller that asks the connected receiver for a Signal Snapshot
every :data:`POLL_INTERVAL_SECONDS`.  ``current()`` is what the UI (and
later the metrics) read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from sp_rtk_base.models.signal_quality_models import (
    SignalQualityNoData,
    SignalQualityReading,
)
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.signal_quality.monitor import SignalQualityMonitor

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0

NOT_CONNECTED_REASON = "Connect the receiver to see Signal Quality."
# Shown until the first Snapshot arrives.  TODO(#168): once staleness
# lands, failed polls show "The receiver did not answer the signal poll."
WAITING_REASON = "Waiting for signal data from the receiver."


class SignalQualityService:
    """Signal Quality for the UI: one monitor, fed while the receiver is connected."""

    def __init__(
        self,
        device_service: DeviceService,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._device = device_service
        self._clock = clock
        self._monitor = SignalQualityMonitor()
        self._task: asyncio.Task[None] | None = None

    def current(self) -> SignalQualityReading:
        """The Signal Quality to show right now, or why there is none."""
        if not self._device.is_connected:
            return SignalQualityNoData(reason=NOT_CONNECTED_REASON)
        verdict = self._monitor.current(self._clock())
        return (
            verdict
            if verdict is not None
            else SignalQualityNoData(reason=WAITING_REASON)
        )

    async def poll_once(self) -> None:
        """Poll one Signal Snapshot, if the receiver is connected.

        While disconnected, forget the last verdict so a reconnected
        receiver never shows one from before.
        """
        if not self._device.is_connected:
            self._monitor.reset()
            return
        try:
            snapshot = await self._device.get_signal_snapshot()
        except NotImplementedError:
            return
        except Exception:
            logger.debug("Signal Snapshot poll failed", exc_info=True)
            return
        self._monitor.observe(snapshot, self._clock())

    def start(self) -> None:
        """Start the background poller.  Idempotent."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="sp_rtk_base.signal_quality"
            )

    async def stop(self) -> None:
        """Cancel the background poller and wait for it to finish."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
