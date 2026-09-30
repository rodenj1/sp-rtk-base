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
# Connected, no fresh Snapshot, and the receiver was asked less than
# NO_ANSWER_AFTER_SECONDS ago (e.g. just connected).
WAITING_REASON = "Waiting for signal data from the receiver."
NO_ANSWER_REASON = "The receiver did not answer the signal poll."
# Connected, but no poll has been answered for this long.
NO_ANSWER_AFTER_SECONDS = 10.0


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
        # Since the receiver was last connected: when polling began, and
        # when a poll was last answered (monotonic clock).
        self._asking_since: float | None = None
        self._answered_at: float | None = None
        # True while a poll is in flight; a tick that finds it set is skipped.
        self._polling = False

    def current(self) -> SignalQualityReading:
        """The Signal Quality to show right now, or why there is none."""
        if not self._device.is_connected:
            return SignalQualityNoData(reason=NOT_CONNECTED_REASON)
        now = self._clock()
        verdict = self._monitor.current(now)
        if verdict is not None:
            return verdict
        unanswered_since = (
            self._answered_at if self._answered_at is not None else self._asking_since
        )
        if (
            unanswered_since is not None
            and now - unanswered_since >= NO_ANSWER_AFTER_SECONDS
        ):
            return SignalQualityNoData(reason=NO_ANSWER_REASON)
        return SignalQualityNoData(reason=WAITING_REASON)

    async def poll_once(self) -> None:
        """Poll one Signal Snapshot, if the receiver is connected.

        While disconnected, forget the last verdict so a reconnected
        receiver never shows one from before.
        """
        if not self._device.is_connected:
            self._monitor.reset()
            self._asking_since = self._answered_at = None
            return
        if self._polling:
            # The background loop awaits each poll, so it never overlaps
            # itself; this guards any other caller from queuing behind a
            # slow poll (e.g. one stuck behind a configuration write).
            return
        if self._asking_since is None:
            self._asking_since = self._clock()
        self._polling = True
        try:
            snapshot = await self._device.get_signal_snapshot()
        except NotImplementedError:
            # The driver can't supply Snapshots at all: that is not the
            # receiver failing to answer, so it never becomes "no answer".
            self._asking_since = None
            return
        except Exception:
            logger.debug("Signal Snapshot poll failed", exc_info=True)
            return
        finally:
            self._polling = False
        now = self._clock()
        self._answered_at = now
        self._monitor.observe(snapshot, now)

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
