"""SignalQualityService: feeds the monitor and explains missing data.

Owns one :class:`SignalQualityMonitor`, fed from one of two sources:

- **survey-in**: a background poller asks the connected receiver for a
  Signal Snapshot every :data:`POLL_INTERVAL_SECONDS`;
- **Relay**: while the Relay owns the port, MSM Frames from its Frame
  subscription (:meth:`relay_started`) are assembled into Snapshots.

Switching source resets the monitor, so a verdict never mixes the two.
``current(view)`` is what the pages (and metrics) read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Literal

from sp_rtk_base_relay import Frame, FrameSubscription

from sp_rtk_base.models.signal_quality_models import (
    SignalQualityNoData,
    SignalQualityReading,
)
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.signal_quality.frame_bridge import FrameBridge
from sp_rtk_base.services.signal_quality.monitor import SignalQualityMonitor
from sp_rtk_base.services.signal_quality.msm import MsmSnapshotAssembler, is_msm

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0

View = Literal["survey", "dashboard"]
Source = Literal["survey", "relay"]

NOT_CONNECTED_REASON = "Connect the receiver to see Signal Quality."
# No fresh Snapshot yet, but the source was only asked (or started) less
# than NO_DATA_AFTER_SECONDS ago.
WAITING_REASON = "Waiting for signal data from the receiver."
NO_ANSWER_REASON = "The receiver did not answer the signal poll."
RELAY_STOPPED_REASON = "Relay is stopped. Signal Quality shows while it runs."
NO_RTCM_REASON = "No RTCM is arriving from the receiver."
NO_MSM_REASON = (
    "No MSM messages in the RTCM stream. Enable MSM4 or MSM7 output on the "
    "receiver port the Relay reads."
)
# How long a source may go without data before its reason replaces "Waiting".
NO_DATA_AFTER_SECONDS = 10.0


class SignalQualityService:
    """Signal Quality for the UI: one monitor, fed by survey-in or the Relay."""

    def __init__(
        self,
        device_service: DeviceService,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._device = device_service
        self._clock = clock
        self._monitor = SignalQualityMonitor()
        self._source: Source | None = None
        self._task: asyncio.Task[None] | None = None
        # Survey-in, since the receiver was last connected: when polling
        # began, and when a poll was last answered (monotonic clock).
        self._asking_since: float | None = None
        self._answered_at: float | None = None
        # True while a poll is in flight; a poll that finds it set is skipped.
        self._polling = False
        # Relay mode: when it started, and when a Frame / an MSM last arrived.
        self._assembler = MsmSnapshotAssembler()
        self._bridge: FrameBridge | None = None
        self._relay_started_at: float | None = None
        self._last_frame_at: float | None = None
        self._last_msm_at: float | None = None

    # ------------------------------------------------------------------
    # What the pages read
    # ------------------------------------------------------------------

    def current(self, view: View = "survey") -> SignalQualityReading:
        """The Signal Quality to show on *view* right now, or why there is none."""
        now = self._clock()
        if self._source == "relay":
            return self._monitor.current(now) or self._relay_no_data(now)
        if view == "dashboard":
            return SignalQualityNoData(reason=RELAY_STOPPED_REASON)
        if not self._device.is_connected:
            return SignalQualityNoData(reason=NOT_CONNECTED_REASON)
        return self._monitor.current(now) or self._survey_no_data(now)

    def _survey_no_data(self, now: float) -> SignalQualityNoData:
        since = (
            self._answered_at if self._answered_at is not None else self._asking_since
        )
        if since is not None and now - since >= NO_DATA_AFTER_SECONDS:
            return SignalQualityNoData(reason=NO_ANSWER_REASON)
        return SignalQualityNoData(reason=WAITING_REASON)

    def _relay_no_data(self, now: float) -> SignalQualityNoData:
        started = self._relay_started_at if self._relay_started_at is not None else now
        frame_at = self._last_frame_at if self._last_frame_at is not None else started
        msm_at = self._last_msm_at if self._last_msm_at is not None else started
        if now - frame_at >= NO_DATA_AFTER_SECONDS:
            return SignalQualityNoData(reason=NO_RTCM_REASON)
        if now - msm_at >= NO_DATA_AFTER_SECONDS:
            return SignalQualityNoData(reason=NO_MSM_REASON)
        return SignalQualityNoData(reason=WAITING_REASON)

    # ------------------------------------------------------------------
    # Relay source (the RelayService's Frame consumer)
    # ------------------------------------------------------------------

    def relay_started(self, subscription: FrameSubscription) -> None:
        """The Relay has started: judge its MSM from now on."""
        self._stop_bridge()
        self._switch_to("relay")
        self._relay_started_at = self._clock()
        self._last_frame_at = self._last_msm_at = None
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self._bridge = FrameBridge(subscription, self.take_frame, loop)
        self._bridge.start()

    def relay_stopped(self) -> None:
        """The Relay has stopped: its Frames no longer feed Signal Quality."""
        self._stop_bridge()
        if self._source == "relay":
            self._switch_to(None)
        self._relay_started_at = None

    def take_frame(self, frame: Frame) -> None:
        """Take in one Frame from the Relay (called on the event loop)."""
        if self._source != "relay":
            return
        now = self._clock()
        self._last_frame_at = now
        if is_msm(frame.message_id):
            self._last_msm_at = now
        for snapshot in self._assembler.add(frame, now):
            self._monitor.observe(snapshot, now)

    def _stop_bridge(self) -> None:
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            bridge.stop()

    # ------------------------------------------------------------------
    # Survey-in source
    # ------------------------------------------------------------------

    async def poll_once(self) -> None:
        """Poll one Signal Snapshot, if the receiver is connected.

        While disconnected, forget any survey-in verdict so a reconnected
        receiver never shows one from before.  Never touches a Relay-fed
        verdict: the receiver is disconnected by design while the Relay runs.
        """
        if self._source == "relay":
            return
        if not self._device.is_connected:
            if self._source == "survey":
                self._switch_to(None)
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
        if self._relay_is_source():
            return  # the Relay took over while this poll was in flight
        self._switch_to("survey")
        now = self._clock()
        self._answered_at = now
        self._monitor.observe(snapshot, now)

    def _relay_is_source(self) -> bool:
        """Re-read the source (it can change while a poll is awaited)."""
        return self._source == "relay"

    def _switch_to(self, source: Source | None) -> None:
        """Change source; a verdict never carries over from the other one."""
        if source != self._source:
            self._monitor.reset()
            self._assembler.reset()
            self._source = source

    # ------------------------------------------------------------------
    # Background poller
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background poller.  Idempotent."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="sp_rtk_base.signal_quality"
            )

    async def stop(self) -> None:
        """Cancel the background poller and stop any Frame bridge."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._stop_bridge()

    async def _run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
