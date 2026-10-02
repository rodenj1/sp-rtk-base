"""A Corrected survey-in's own Relay instance, feeding the receiver (ADR 0004).

The Relay pulls; the application pushes. The instance's input is the
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

from sp_rtk_base_relay import FrameSubscription, RelayEngine
from sp_rtk_base_relay.config import InputConfig
from sp_rtk_base_relay.exceptions import NtripConnectionError

from sp_rtk_base.models.verification_models import VerificationStage
from sp_rtk_base.services.correction_verification import failure_stage
from sp_rtk_base.services.device_service import DeviceService

logger = logging.getLogger(__name__)

# How long the pump waits for a Frame before checking it should go on.
_FRAME_WAIT_S = 0.5
# After the first failed write, log only every this many.
_LOG_EVERY_FAILURES = 100


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

    async def status(self) -> tuple[bool, str | None]:
        """Whether the source is connected, and its last connection error."""
        if not self._engine.is_running:
            return False, None
        status = await asyncio.to_thread(self._engine.get_status)
        return status.input.connected, status.input.last_error

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
        failures = 0
        while True:
            frame = await asyncio.to_thread(subscription.get_frame, _FRAME_WAIT_S)
            if frame is None:
                if subscription.closed:
                    return
                continue
            try:
                await self._device.write_corrections(frame.data)
            except Exception:
                # Skip this Frame and keep going: a passing serial error
                # mustn't end the corrections. A receiver that stopped
                # answering ends the survey through its position reads.
                failures += 1
                if failures == 1 or failures % _LOG_EVERY_FAILURES == 0:
                    logger.exception(
                        "Could not write corrections to the receiver (%d so far)",
                        failures,
                    )
