"""A Corrected survey-in's own Relay instance, feeding the receiver (ADR 0004).

The Relay pulls; the application pushes: each write carries every Frame
waiting, whole and in order. The instance's input is the
Correction source, it has no destinations and one Frame subscriber, and
every Frame it reads is written whole and unfiltered to the receiver
through the driver, under the same lock as the position polls.

The instance belongs to the survey, not to ``RelayService``: it is invisible
to "relay running", the dashboard, metrics (no collector) and Signal
Quality, which all keep meaning the operator's Relay.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from sp_rtk_base_relay import FrameSubscription, RelayEngine
from sp_rtk_base_relay.config import InputConfig
from sp_rtk_base_relay.exceptions import NtripConnectionError

from sp_rtk_base.models.verification_models import VerificationStage
from sp_rtk_base.services.correction_verification import failure_stage
from sp_rtk_base.services.device_service import DeviceService

logger = logging.getLogger(__name__)

# How long the pump waits for a Frame before checking it should go on.
_FRAME_WAIT_S = 0.5
# A write stops taking more Frames once it holds this many bytes.
_MAX_BYTES_PER_WRITE = 4096
# After the first failed write, log only every this many.
_LOG_EVERY_FAILURES = 100


@dataclass(frozen=True)
class FeedStatus:
    """The source's connection, and how many Frames reached the port."""

    connected: bool
    last_error: str | None
    written: int  # Frames written to the receiver
    bytes_written: int
    write_failures: int  # Frame writes that failed
    dropped: int  # Frames the pump fell too far behind to take


class CorrectionSourceUnreachableError(RuntimeError):
    """The survey's Relay instance couldn't connect to the Correction source.

    ``stage`` names where it failed, in the Verification's Stage names
    (connect / caster / auth / mountpoint), and ``code`` the failure.
    """

    def __init__(self, stage: VerificationStage, code: str, detail: str) -> None:
        super().__init__(f"The Correction source failed at {stage.value}: {detail}")
        self.stage = stage
        self.code = code
        self.detail = detail


class CorrectionFeed:
    """Pulls a Correction source and writes every Frame to the receiver."""

    def __init__(self, device: DeviceService, input_config: InputConfig) -> None:
        self._device = device
        self._engine = RelayEngine(input_config)
        self._subscription: FrameSubscription | None = None
        self._pump: asyncio.Task[None] | None = None
        self._written = 0
        self._bytes_written = 0
        self._write_failures = 0

    async def start(self) -> None:
        """Connect once (fail fast), then start pushing Frames.

        Raises:
            CorrectionSourceUnreachableError: If the engine can't connect.
        """
        try:
            await asyncio.to_thread(self._engine.start, [])
        except NtripConnectionError as exc:
            stage, code = failure_stage(exc)
            raise CorrectionSourceUnreachableError(stage, code, exc.message) from exc
        except Exception as exc:
            raise CorrectionSourceUnreachableError(
                VerificationStage.CONNECT, "other", str(exc)
            ) from exc
        self._subscription = self._engine.subscribe_frames()
        self._pump = asyncio.create_task(self._push_frames(self._subscription))

    async def status(self) -> FeedStatus:
        """The source's connection and last error, and the Frame counts."""
        connected, last_error, dropped = False, None, 0
        if self._engine.is_running:
            status = await asyncio.to_thread(self._engine.get_status)
            connected, last_error = status.input.connected, status.input.last_error
            dropped = status.frame_subscriber_drops
        return FeedStatus(
            connected=connected,
            last_error=last_error,
            written=self._written,
            bytes_written=self._bytes_written,
            write_failures=self._write_failures,
            dropped=dropped,
        )

    async def stop(self) -> None:
        """Stop pushing and stop the Relay instance. Safe to call more than once."""
        pump, self._pump = self._pump, None
        if self._subscription is not None:
            self._subscription.close()
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        await asyncio.to_thread(self._engine.stop)

    async def _push_frames(self, subscription: FrameSubscription) -> None:
        while True:
            frame = await asyncio.to_thread(subscription.get_frame, _FRAME_WAIT_S)
            if frame is None:
                if subscription.closed:
                    return
                continue
            # Every Frame waiting goes in one write: each write waits its turn
            # for the driver lock, which slow receiver reads can hold for
            # seconds, so one Frame per turn falls far behind (#197).
            frames = [frame]
            size = len(frame.data)
            # Up to about 0.7 s of a 57 600-baud UART, so one write never
            # holds the lock (and the receiver reads) for long.
            while size < _MAX_BYTES_PER_WRITE:
                more = subscription.drain(1)
                if not more:
                    break
                frames += more
                size += len(more[0].data)
            data = b"".join(f.data for f in frames)
            try:
                await self._device.write_corrections(data)
            except Exception:
                # Skip these Frames and keep going: a passing serial error
                # mustn't end the corrections. A receiver that stopped
                # answering ends the survey through its position reads.
                before = self._write_failures
                self._write_failures += len(frames)
                failures = self._write_failures
                # The first failure, then once per _LOG_EVERY_FAILURES Frames.
                if before == 0 or (
                    failures // _LOG_EVERY_FAILURES != before // _LOG_EVERY_FAILURES
                ):
                    logger.exception(
                        "Could not write corrections to the receiver (%d so far)",
                        failures,
                    )
            else:
                self._written += len(frames)
                self._bytes_written += len(data)
