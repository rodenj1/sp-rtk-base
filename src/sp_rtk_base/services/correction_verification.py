"""Verifying a Correction source (issue #194).

A Verification rehearses what a Corrected survey-in would do with the form's
values: it builds the Relay's own NTRIP client input, connects once (no
retries), reads for a short window, and disconnects. Its Stages map one for
one to the Relay's typed NTRIP errors: connect -> caster -> auth ->
mountpoint -> data. Green means a Corrected survey-in started now would
receive RTCM 3 Frames. See CONTEXT.md (Verification) and decision
rodenj1/rtk_development#20.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from sp_rtk_base_relay.config import NtripInputConfig
from sp_rtk_base_relay.core.input_sources.ntrip_input import NtripInputSource
from sp_rtk_base_relay.exceptions import NtripConnectionError, NtripFailure
from sp_rtk_base_relay.rtcm_decoder import RTCMMessageDecoder

from sp_rtk_base.models.config_models import NtripCorrectionConfig
from sp_rtk_base.models.verification_models import (
    CORRECTION_SOURCE_STAGES,
    StageResult,
    StageStatus,
    VerificationResult,
    VerificationStage,
    build_result,
)
from sp_rtk_base.services.verification import VerificationRefusedError

logger = logging.getLogger(__name__)

#: How long to wait for a reference-station position (1005/1006).
DATA_WINDOW_SECONDS = 15.0
#: How long the connect and the caster's reply may take.
CONNECT_TIMEOUT_SECONDS = 5.0
#: The whole Verification's budget: a slow connect shortens the data window.
TOTAL_BUDGET_SECONDS = 20.0

_REFERENCE_POSITION_IDS = frozenset({1005, 1006})

# The Stage each typed NTRIP failure belongs to.
_FAILURE_STAGES = {
    NtripFailure.CONNECT: VerificationStage.CONNECT,
    NtripFailure.CASTER: VerificationStage.CASTER,
    NtripFailure.AUTH: VerificationStage.AUTH,
    NtripFailure.MOUNTPOINT: VerificationStage.MOUNTPOINT,
    NtripFailure.DATA_TIMEOUT: VerificationStage.DATA,
}
_FAILURE_CODES = {
    NtripFailure.CASTER: "bad_reply",
    NtripFailure.AUTH: "rejected",
    NtripFailure.MOUNTPOINT: "not_offered",
    NtripFailure.DATA_TIMEOUT: "silent",
}


def failure_stage(exc: NtripConnectionError) -> tuple[VerificationStage, str]:
    """The Stage a typed NTRIP failure belongs to, and its code.

    Shared by the Verification and by a Corrected survey-in's refused start,
    so both name a failure the same way.
    """
    stage = _FAILURE_STAGES.get(exc.reason, VerificationStage.CASTER)
    code = (
        exc.connect_failure.value
        if exc.reason is NtripFailure.CONNECT and exc.connect_failure
        else _FAILURE_CODES.get(exc.reason, "other")
    )
    return stage, code


@dataclass
class _Frames:
    """What the data window saw."""

    bytes_read: int = 0
    frames: int = 0
    reference_position: bool = False


def _scan(buffer: bytearray, seen: _Frames) -> None:
    """Count the whole CRC-valid RTCM 3 Frames in ``buffer``, consuming them.

    Resyncs one byte at a time past anything that isn't a Frame; keeps a
    partial Frame at the end for the next read.
    """
    while True:
        start = buffer.find(0xD3)
        if start < 0:
            buffer.clear()
            return
        del buffer[:start]
        length = RTCMMessageDecoder.extract_message_length(bytes(buffer[:3]))
        if length is None:
            return  # fewer than 3 bytes so far
        total = 3 + length + 3
        if len(buffer) < total:
            return
        frame = bytes(buffer[:total])
        if RTCMMessageDecoder.is_valid_rtcm_frame(frame):
            seen.frames += 1
            if RTCMMessageDecoder.extract_message_id(frame) in _REFERENCE_POSITION_IDS:
                seen.reference_position = True
            del buffer[:total]
        else:
            del buffer[:1]


class CorrectionSourceVerificationService:
    """Runs one Correction source Verification at a time."""

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
        data_window_seconds: float = DATA_WINDOW_SECONDS,
    ) -> None:
        self.connect_timeout_seconds = connect_timeout_seconds
        self.data_window_seconds = data_window_seconds
        self.total_budget_seconds = max(TOTAL_BUDGET_SECONDS, data_window_seconds)
        self._running = False
        self._survey_running: Callable[[], bool] = lambda: False

    def set_survey_running_check(self, check: Callable[[], bool]) -> None:
        """Refuse a Verification while ``check()`` says a Corrected survey runs.

        Its connection could compete with the survey's own for the same
        caster account (decision rodenj1/rtk_development#20).
        """
        self._survey_running = check

    async def verify(self, config: NtripCorrectionConfig) -> VerificationResult:
        """Verify ``config``: would a Corrected survey-in receive corrections?

        Raises:
            VerificationRefusedError: ``verification_in_progress`` if another
                Verification is running (nothing is touched).
        """
        if self._survey_running():
            raise VerificationRefusedError(
                "survey_running",
                "A Corrected survey-in is running; Verify once it has finished.",
            )
        if self._running:
            raise VerificationRefusedError(
                "verification_in_progress",
                "A Verification is already running; wait for it to finish.",
            )
        self._running = True
        worker = asyncio.ensure_future(asyncio.to_thread(self._walk, config))
        # The slot frees when the walk ends, not when the caller stops
        # waiting (a closed page): a second Verification must not overlap it.
        worker.add_done_callback(lambda _: setattr(self, "_running", False))
        return await asyncio.shield(worker)

    def _walk(self, config: NtripCorrectionConfig) -> VerificationResult:
        started = time.monotonic()
        recorded: dict[VerificationStage, StageResult] = {}
        source = NtripInputSource(
            NtripInputConfig(
                caster=config.caster,
                port=config.port,
                mountpoint=config.mountpoint,
                username=config.username,
                password=config.password,
                version=config.version,
                tls=config.tls,
                connection_timeout=self.connect_timeout_seconds,
                # The window ends the read; never let the Relay drop it first.
                data_timeout=self.data_window_seconds + 1.0,
            )
        )
        try:
            source.connect()
        except NtripConnectionError as exc:
            failed, code = failure_stage(exc)
            for stage in CORRECTION_SOURCE_STAGES:
                if stage is failed:
                    break
                recorded[stage] = StageResult(stage=stage, status=StageStatus.PASSED)
            recorded[failed] = StageResult(
                stage=failed, status=StageStatus.FAILED, code=code, message=exc.message
            )
            return build_result(recorded, order=CORRECTION_SOURCE_STAGES)
        except Exception as exc:  # anything else at connect: still a verdict
            logger.exception("Correction source Verification: connect failed")
            recorded[VerificationStage.CONNECT] = StageResult(
                stage=VerificationStage.CONNECT,
                status=StageStatus.FAILED,
                code="other",
                message=str(exc),
            )
            return build_result(recorded, order=CORRECTION_SOURCE_STAGES)

        for stage in CORRECTION_SOURCE_STAGES[:-1]:
            recorded[stage] = StageResult(stage=stage, status=StageStatus.PASSED)
        window = min(
            self.data_window_seconds,
            self.total_budget_seconds - (time.monotonic() - started),
        )
        try:
            recorded[VerificationStage.DATA] = self._read_window(source, window)
        finally:
            source.disconnect()
        return build_result(recorded, order=CORRECTION_SOURCE_STAGES)

    def _read_window(self, source: NtripInputSource, window_s: float) -> StageResult:
        seen = _Frames()
        buffer = bytearray()
        deadline = time.monotonic() + max(window_s, 0.0)
        while (left := deadline - time.monotonic()) > 0:
            try:
                data = source.read_data(timeout=min(left, 0.5))
            except NtripConnectionError:
                break  # the caster stopped answering; judge what arrived
            if data:
                seen.bytes_read += len(data)
                buffer += data
                _scan(buffer, seen)
                if seen.reference_position:
                    break
            elif not source.is_connected:
                break  # the caster closed the stream
        data_stage = VerificationStage.DATA
        if seen.reference_position:
            return StageResult(stage=data_stage, status=StageStatus.PASSED)
        if seen.frames:
            return StageResult(
                stage=data_stage,
                status=StageStatus.WARNING,
                code="no_reference_position",
                message=(
                    "RTCM 3 Frames arrived, but no reference station position "
                    "(1005/1006) within the window; the caster may send it rarely."
                ),
            )
        if seen.bytes_read:
            return StageResult(
                stage=data_stage,
                status=StageStatus.FAILED,
                code="not_rtcm3",
                message="Bytes arrived, but none formed an RTCM 3 Frame.",
            )
        return StageResult(
            stage=data_stage,
            status=StageStatus.FAILED,
            code="silent",
            message="The caster accepted the request but sent no data.",
        )
