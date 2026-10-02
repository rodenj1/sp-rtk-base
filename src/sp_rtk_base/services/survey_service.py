"""Survey-in, whoever averages it.

A receiver with a Receiver survey-in (``DeviceCapability.SURVEY_IN``) averages
its own position. On one without, the station averages instead: it reads a
high-precision position once a second, keeps the valid 3D fixes, and commits
their mean as the fixed base itself, whether or not a page is open.

A **Corrected survey-in** is always averaged by the station: the receiver
works as a rover, the survey's own Relay instance feeds it a Correction
source's RTCM 3 (ADR 0004), and only RTK Fixed solutions are Observations. Its engine is
stopped and the receiver's input settings restored on every exit, and
before the fixed base is committed and saved to flash.

Both report the same :class:`SurveyInProgress`, with ``averaged_by``,
``outcome`` and ``abort_reason``. A survey lives in memory only and is lost
on restart; the receiver then stays in rover mode.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sp_rtk_base.models.config_models import CorrectionSourceProfile
from sp_rtk_base.models.device_models import (
    CorrectedSurveyInConfig,
    CorrectionDiagnostics,
    CorrectionInputCounters,
    DeviceCapability,
    FixedBaseConfig,
    SurveyAbortReason,
    SurveyInConfig,
    SurveyInProgress,
    SurveyOutcome,
    SurveyPosition,
)
from sp_rtk_base.services.correction_feed import CorrectionFeed
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.base import GpsReceiverDriver
from sp_rtk_base.services.geodesy import ecef_to_llh
from sp_rtk_base.services.link_diagnostics import Sampler

logger = logging.getLogger(__name__)

# The station samples the receiver's position once a second; each accepted
# observation counts one second towards the minimum duration.
SAMPLE_INTERVAL_S: float = 1.0

# An application-averaged survey that hasn't completed by the time the
# wall-clock time reaches max(this, HARD_CAP_MULTIPLE x the minimum duration)
# gives up with ``accuracy_not_reached``.
HARD_CAP_MIN_S: float = 3600.0
HARD_CAP_MULTIPLE: float = 3.0

# A Corrected survey-in whose observation time hasn't grown for this long
# (since the start, or since the last RTK Fixed Observation) aborts
# (decision rodenj1/rtk_development#19); it warns once RTK Fixed has been
# missing for STALL_WARNING_S. Not operator settings.
STALL_ABORT_S: float = 600.0
STALL_WARNING_S: float = 60.0
# How often a Corrected survey-in reads the receiver's own count of what
# reached its console port (a MON-COMMS poll on u-blox).
RECEIVER_COUNTERS_EVERY_S: float = 30.0
# A Corrected survey-in counts an RTK Fixed solution as an Observation only
# once Fixed has held this long without a break, and only while its
# corrections are no older than MAX_CORRECTION_AGE_S: a Fixed that has just
# formed, or that rests on stale corrections, drifts (#197, run 5's spread
# grew from 30 to 91 mm on 30-45 s old corrections). The bench confirms both.
FIXED_SETTLE_S: float = 30.0
MAX_CORRECTION_AGE_S: float = 10.0
# An RTK Fixed solution more than this far (3D) from the survey's mean so far
# (or, before any Observation, from the last Fixed) is a jump to a different
# Fixed solution: the averaging restarts, so two Fixed solutions never mix.
# Fixed noise is millimetres; on the bench a false Fixed sat 9-13 cm off the
# right one for 3 minutes with no break in Fixed (#197, run 1 on P472).
FIXED_JUMP_M: float = 0.05

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class SurveyBusyError(RuntimeError):
    """A survey is already running."""


@dataclass(frozen=True)
class _Limits:
    """What an application-averaged survey needs to complete."""

    min_duration_s: int
    accuracy_limit_mm: float
    corrected: bool  # only RTK Fixed epochs count


@dataclass
class _Averaging:
    """The station's running mean of accepted observations (ECEF, metres).

    Welford's method: ECEF coordinates are millions of metres, so summing
    their squares would lose the millimetres the spread is made of.
    """

    count: int = 0
    mean_xyz: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    m2_xyz: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    sum_accuracy_m: float = 0.0

    def add(self, position: SurveyPosition) -> None:
        self.count += 1
        xyz = (position.ecef_x_m, position.ecef_y_m, position.ecef_z_m)
        for axis, value in enumerate(xyz):
            delta = value - self.mean_xyz[axis]
            self.mean_xyz[axis] += delta / self.count
            self.m2_xyz[axis] += delta * (value - self.mean_xyz[axis])
        self.sum_accuracy_m += position.accuracy_3d_m

    def mean(self) -> tuple[float, float, float]:
        x, y, z = self.mean_xyz
        return (x, y, z)

    def accuracy_m(self) -> float:
        """max(mean 3D accuracy, 3D std-dev about the mean), with no sqrt(N)."""
        mean_accuracy = self.sum_accuracy_m / self.count
        spread = math.sqrt(sum(self.m2_xyz) / self.count)
        return max(mean_accuracy, spread)


class _ReceiverCounts:
    """The receiver's console-port counters, accumulated since the start.

    The receiver's counters are cumulative: message counts wrap at 16 bits,
    so each read adds its wrapped difference. Byte counts are 32-bit and
    don't wrap in a survey, so one that goes backwards means the receiver
    reset its counters, and counts again from zero.
    """

    def __init__(self) -> None:
        self._last: CorrectionInputCounters | None = None
        self.rtcm3_messages = 0
        self.rx_bytes = 0
        self.skipped_bytes = 0
        self.overrun_errors = 0

    def add(self, now: CorrectionInputCounters) -> None:
        last, self._last = self._last, now
        if last is None:
            return  # the first read is the baseline
        if now.rx_bytes < last.rx_bytes:  # the receiver reset its counters
            last = CorrectionInputCounters(
                rx_bytes=0, rtcm3_messages=0, skipped_bytes=0, overrun_errors=0
            )
        self.rtcm3_messages += (now.rtcm3_messages - last.rtcm3_messages) % 2**16
        self.rx_bytes += now.rx_bytes - last.rx_bytes
        self.skipped_bytes += max(0, now.skipped_bytes - last.skipped_bytes)
        self.overrun_errors += (now.overrun_errors - last.overrun_errors) % 2**16


def _stall_reason(position: SurveyPosition) -> SurveyAbortReason:
    """Why there are no Observations: no fresh corrections, or only Float.

    "Fresh" is the same age an Observation needs, so a Fixed on stale
    corrections is never blamed on Float.
    """
    age = position.correction_age_s
    if age is None or age > MAX_CORRECTION_AGE_S:
        return "no_corrections"
    return "no_fixed"


class SurveyService:
    """Runs one Survey-in at a time and reports its progress."""

    def __init__(
        self,
        device: DeviceService,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        stall_warning_s: float = STALL_WARNING_S,
        stall_abort_s: float = STALL_ABORT_S,
        fixed_settle_s: float = FIXED_SETTLE_S,
    ) -> None:
        self._device = device
        self._stall_warning_s = stall_warning_s
        self._stall_abort_s = stall_abort_s
        self._fixed_settle_s = fixed_settle_s
        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        # Start and cancel each await the device; one at a time.
        self._lock = asyncio.Lock()
        self._progress: SurveyInProgress | None = None
        # The receiver the survey runs on; a survey belongs to it alone.
        self._survey_driver: GpsReceiverDriver | None = None
        # Receiver survey-in: its duration counter when this survey started.
        self._receiver_duration_offset = 0
        # A Corrected survey-in's own Relay instance, while it runs.
        self._feed: CorrectionFeed | None = None
        # The source a Corrected survey-in is connecting to, before it runs.
        self._starting_source: str | None = None
        # What the receiver says reached its console port, while corrected.
        self._receiver_counts: _ReceiverCounts | None = None
        self._receiver_counters_last: CorrectionInputCounters | None = None
        self._written_at_receiver_read: int | None = None
        # Bench diagnosis (#197): the survey's own sampling, on the wall clock.
        self._sample_interval_s = Sampler()
        self._position_read_s = Sampler()
        self._receiver_counts_at = 0.0
        device.add_before_disconnect(self._before_disconnect)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, config: SurveyInConfig) -> None:
        """Start a plain Survey-in with whichever averager the receiver allows.

        Raises:
            SurveyBusyError: If a survey is already running.
            RuntimeError: If the device isn't connected or the relay runs.
        """
        async with self._lock:
            await self._forget_if_receiver_changed()
            if self._is_running():
                raise SurveyBusyError("A survey is already running")
            self._survey_driver = self._device.driver
            if DeviceCapability.SURVEY_IN in self._device.capabilities:
                await self._start_receiver_survey(config)
            else:
                await self._start_application_survey(config)

    async def start_corrected(
        self, config: CorrectedSurveyInConfig, source: CorrectionSourceProfile
    ) -> None:
        """Start a Corrected survey-in against ``source``.

        Needs no Verification and no first Frame: starting the survey's
        Relay instance is itself one fail-fast connect attempt.

        Raises:
            SurveyBusyError: If a survey is already running.
            CorrectionSourceUnreachableError: If it can't connect;
                the receiver's input settings are restored.
            RuntimeError: If the device isn't connected or the relay runs.
        """
        async with self._lock:
            await self._forget_if_receiver_changed()
            if self._is_running():
                raise SurveyBusyError("A survey is already running")
            await self._device.disable_base_mode()  # a rover, taking corrections
            # In use from now: no rename, delete or Verify while connecting.
            self._starting_source = source.name
            try:
                await self._device.begin_correction_input()
                feed = CorrectionFeed(self._device, source.to_relay_config())
                await feed.start()
            except Exception:
                await self._restore_correction_input()
                raise
            finally:
                self._starting_source = None
            self._survey_driver = self._device.driver
            self._feed = feed
            self._receiver_counts = _ReceiverCounts()
            self._sample_interval_s = Sampler()
            self._position_read_s = Sampler()
            await self._read_receiver_counts(now=True)  # the baseline
            self._progress = SurveyInProgress(
                active=True,
                averaged_by="application",
                outcome="running",
                min_duration_seconds=config.min_duration_seconds,
                accuracy_limit_mm=config.accuracy_limit_mm,
                correction_source=source.name,
                source_connected=True,
                fixed_jumps=0,
                fixed_settle_seconds=int(self._fixed_settle_s),
            )
            limits = _Limits(
                config.min_duration_seconds, config.accuracy_limit_mm, corrected=True
            )
            self._task = asyncio.create_task(self._run_application_survey(limits))
            logger.info("Corrected survey-in started against %s", source.name)

    def correction_source_in_use(self) -> str | None:
        """The Correction source a Corrected survey-in pulls (or is
        connecting to), if any."""
        if self._starting_source is not None:
            return self._starting_source
        if not self._is_running() or self._progress is None:
            return None
        return self._progress.correction_source

    def corrected_survey_running(self) -> bool:
        """Whether a Corrected survey-in is running."""
        return self.correction_source_in_use() is not None

    async def progress(self) -> SurveyInProgress:
        """The current survey's progress.

        Raises:
            RuntimeError: If the receiver must be read and isn't connected.
        """
        await self._forget_if_receiver_changed()
        if self._progress is not None and self._progress.averaged_by == "application":
            return self._progress
        if DeviceCapability.SURVEY_IN not in self._device.capabilities:
            return self._progress or SurveyInProgress()
        raw = await self._device.get_survey_in_status()
        if self._progress is None:
            return raw  # no survey started here (e.g. after a restart)
        return self._receiver_progress(raw)

    async def cancel(self) -> None:
        """Stop the current survey and leave the receiver in rover mode.

        Raises:
            RuntimeError: If the device isn't connected or the relay runs.
        """
        async with self._lock:
            await self._forget_if_receiver_changed()
            if (
                self._progress is not None
                and self._progress.averaged_by == "application"
            ):
                await self._stop_task()
                await self._stop_corrections()
                if self._progress.outcome != "running":
                    return  # finished: never undo a committed fixed base
                self._finish("cancelled")
                await self._device.disable_base_mode()
                return
            await self._device.cancel_survey_in()
            if self._progress is not None and self._progress.outcome == "running":
                self._finish("cancelled")

    async def shutdown(self) -> None:
        """Stop sampling (on app shutdown)."""
        await self._stop_task()
        await self._stop_corrections()

    async def _before_disconnect(self) -> None:
        """Stop a running survey while the receiver still answers."""
        async with self._lock:
            if not self._is_running():
                return
            await self._stop_task()
            await self._stop_corrections()
            if self._progress is not None and self._progress.outcome == "running":
                self._finish("aborted", "device_disconnected")
                logger.warning("Survey-in aborted: the receiver was disconnected")

    # ------------------------------------------------------------------
    # Receiver survey-in
    # ------------------------------------------------------------------

    async def _start_receiver_survey(self, config: SurveyInConfig) -> None:
        await self._device.configure_survey_in(config)
        # The receiver's duration counter can carry over from an earlier
        # session; elapsed time is counted from where it stood at the start.
        try:
            started = await self._device.get_survey_in_status()
            self._receiver_duration_offset = started.duration_seconds
        except Exception:
            logger.exception("Could not read the survey-in counter at the start")
            self._receiver_duration_offset = 0
        self._progress = SurveyInProgress(
            active=True, averaged_by="receiver", outcome="running"
        )

    def _receiver_progress(self, raw: SurveyInProgress) -> SurveyInProgress:
        assert self._progress is not None
        if self._progress.outcome in ("cancelled", "aborted"):
            return self._progress
        outcome: SurveyOutcome = "completed" if raw.valid else "running"
        self._progress = raw.model_copy(
            update={
                "duration_seconds": max(
                    0, raw.duration_seconds - self._receiver_duration_offset
                ),
                # HPG 1.12 can report active=False while still surveying.
                "active": not raw.valid,
                "averaged_by": "receiver",
                "outcome": outcome,
            }
        )
        return self._progress

    # ------------------------------------------------------------------
    # Application averaging
    # ------------------------------------------------------------------

    async def _start_application_survey(self, config: SurveyInConfig) -> None:
        await self._device.disable_base_mode()  # rover, while the station averages
        self._progress = SurveyInProgress(
            active=True,
            averaged_by="application",
            outcome="running",
            min_duration_seconds=config.min_duration_seconds,
            accuracy_limit_mm=config.accuracy_limit_mm,
        )
        limits = _Limits(
            config.min_duration_seconds, config.accuracy_limit_mm, corrected=False
        )
        self._task = asyncio.create_task(self._run_application_survey(limits))

    async def _run_application_survey(self, limits: _Limits) -> None:
        started_at = self._clock()
        hard_cap_s = max(HARD_CAP_MIN_S, HARD_CAP_MULTIPLE * limits.min_duration_s)
        limit_m = limits.accuracy_limit_mm / 1000.0
        averaging = _Averaging()
        next_sample_at = started_at
        last_growth_at = started_at  # the start, or the last Observation
        last_sample_wall: float | None = None
        fixed_since: float | None = None  # when the current Fixed began
        last_fixed: tuple[float, float, float] | None = None
        try:
            while True:
                # Bench diagnosis (#197), on the real (monotonic) clock, not
                # the injectable one: how often the survey really samples,
                # and how long each read takes.
                read_began = time.monotonic()
                position = await self._device.get_survey_position()
                read_done = time.monotonic()
                self._position_read_s.add(read_done - read_began)
                if last_sample_wall is not None:
                    self._sample_interval_s.add(read_began - last_sample_wall)
                last_sample_wall = read_began
                fixed = position.fix_ok and position.rtk_status == "fixed"
                if fixed and limits.corrected:
                    here = (position.ecef_x_m, position.ecef_y_m, position.ecef_z_m)
                    reference = averaging.mean() if averaging.count else last_fixed
                    if (
                        reference is not None
                        and math.dist(here, reference) > FIXED_JUMP_M
                    ):
                        # A different Fixed solution: start the average and
                        # the settling again, so the two never mix.
                        self._restart_after_jump(math.dist(here, reference))
                        averaging = _Averaging()
                        fixed_since = None
                    last_fixed = here
                if not fixed:
                    fixed_since = None
                elif fixed_since is None:
                    fixed_since = self._clock()
                held_s = self._clock() - fixed_since if fixed_since is not None else 0.0
                observed = self._is_observation(position, limits, held_s)
                if observed:
                    last_growth_at = self._clock()
                if limits.corrected:
                    stalled_s = self._clock() - last_growth_at
                    await self._update_correction_progress(position, stalled_s, held_s)
                    if stalled_s >= self._stall_abort_s:
                        reason: SurveyAbortReason = _stall_reason(position)
                        # Restored before the outcome shows; never a fallback.
                        if not await self._stop_corrections():
                            reason = "input_not_restored"
                        self._finish("aborted", reason)
                        logger.warning(
                            "Corrected survey-in aborted: no RTK Fixed for %.0f s (%s)",
                            stalled_s,
                            reason,
                        )
                        return
                if observed:
                    averaging.add(position)
                    self._update_application_progress(averaging)
                    if (
                        averaging.count >= limits.min_duration_s
                        and averaging.accuracy_m() <= limit_m
                    ):
                        await self._commit(averaging)
                        return
                if self._clock() - started_at >= hard_cap_s:
                    await self._stop_corrections()
                    self._finish("aborted", "accuracy_not_reached")
                    logger.warning("Survey-in aborted: accuracy not reached in time")
                    return
                # Once a second, however long the read itself took.
                next_sample_at += SAMPLE_INTERVAL_S
                await self._sleep(max(0.0, next_sample_at - self._clock()))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Survey-in aborted: the receiver stopped answering")
            self._finish("aborted", "device_disconnected")
        finally:
            # Every exit stops a Corrected survey-in's Relay instance and
            # restores the receiver's input (already done before a commit).
            # Shielded: a second cancel mustn't cut the restore short.
            await asyncio.shield(self._stop_corrections())

    def _is_observation(
        self, position: SurveyPosition, limits: _Limits, held_s: float
    ) -> bool:
        """Plain: any valid 3D fix. Corrected: an RTK Fixed solution that has
        held for the settling time, on fresh corrections."""
        if not limits.corrected:
            return position.fix_ok
        age = position.correction_age_s
        return (
            position.fix_ok
            and position.rtk_status == "fixed"
            and held_s >= self._fixed_settle_s
            and age is not None
            and age <= MAX_CORRECTION_AGE_S
        )

    async def _update_correction_progress(
        self, position: SurveyPosition, stalled_s: float, held_s: float
    ) -> None:
        """The receiver's RTK status and correction age, the source's state,
        and how long the survey has gone without an RTK Fixed Observation."""
        assert self._progress is not None
        delivery: dict[str, object] = {}
        if self._feed is not None:
            feed = await self._feed.status()
            delivery = {
                "source_connected": feed.connected,
                "source_last_error": feed.last_error,
                "corrections_written": feed.written,
                "correction_bytes_written": feed.bytes_written,
                "correction_write_failures": feed.write_failures,
                "corrections_dropped": feed.dropped,
            }
        delivery.update(await self._read_receiver_counts())
        delivery["diagnostics"] = self._diagnostics()
        without_fixed = int(stalled_s)
        self._progress = self._progress.model_copy(
            update={
                **delivery,
                "rtk_status": position.rtk_status,
                "correction_age_s": position.correction_age_s,
                "seconds_without_fixed": without_fixed,
                "fixed_held_seconds": int(held_s),
                "stall_abort_in_seconds": max(
                    0, int(self._stall_abort_s) - without_fixed
                ),
                "stall_warning": stalled_s > self._stall_warning_s,
                "stall_reason": (
                    None if without_fixed == 0 else _stall_reason(position)
                ),
            }
        )

    def _diagnostics(self) -> CorrectionDiagnostics:
        """How corrections are travelling to the receiver, as measured so far."""
        last = self._receiver_counters_last
        feed = self._feed
        return CorrectionDiagnostics(
            frame_age_s=feed.frame_age_s.spread() if feed else None,
            batch_frames=feed.batch_frames.spread() if feed else None,
            sample_interval_s=self._sample_interval_s.spread(),
            written_at_receiver_read=self._written_at_receiver_read,
            position_read_s=self._position_read_s.spread(),
            receiver_tx_pending=last.tx_pending if last else None,
            receiver_rx_pending=last.rx_pending if last else None,
            receiver_tx_peak_usage=last.tx_peak_usage if last else None,
            receiver_rx_peak_usage=last.rx_peak_usage if last else None,
            driver=self._device.get_link_diagnostics(),
        )

    async def _read_receiver_counts(self, *, now: bool = False) -> dict[str, object]:
        """What the receiver says reached its console port since the start.

        Read every RECEIVER_COUNTERS_EVERY_S (best effort; a missed read is
        retried at the next); between reads, the last counts.
        """
        counts = self._receiver_counts
        if counts is None:
            return {}
        if now or self._clock() - self._receiver_counts_at >= RECEIVER_COUNTERS_EVERY_S:
            self._receiver_counts_at = self._clock()
            try:
                read = await self._device.get_correction_input_counters()
            except Exception:
                logger.warning(
                    "Could not read the receiver's input counters", exc_info=True
                )
                read = None
            if read is not None:
                counts.add(read)
                self._receiver_counters_last = read
                self._written_at_receiver_read = (
                    self._feed.written if self._feed is not None else None
                )
        return {
            "receiver_rtcm3_messages": counts.rtcm3_messages,
            "receiver_rx_bytes": counts.rx_bytes,
            "receiver_skipped_bytes": counts.skipped_bytes,
            "receiver_overrun_errors": counts.overrun_errors,
        }

    def _restart_after_jump(self, jump_m: float) -> None:
        """Report a jump while Fixed, and the averaging it discarded."""
        assert self._progress is not None
        jumps = (self._progress.fixed_jumps or 0) + 1
        logger.warning(
            "Corrected survey-in: RTK Fixed jumped %.0f mm; discarding %d "
            "Observations and settling again",
            jump_m * 1000,
            self._progress.observations,
        )
        self._progress = self._progress.model_copy(
            update={
                "fixed_jumps": jumps,
                "last_jump_mm": jump_m * 1000,
                "duration_seconds": 0,
                "observations": 0,
                "mean_accuracy_mm": 0.0,
                "latitude": None,
                "longitude": None,
                "altitude_m": None,
            }
        )

    def _update_application_progress(self, averaging: _Averaging) -> None:
        assert self._progress is not None
        lat, lon, alt = ecef_to_llh(*averaging.mean())
        self._progress = self._progress.model_copy(
            update={
                "duration_seconds": int(averaging.count * SAMPLE_INTERVAL_S),
                "observations": averaging.count,
                "mean_accuracy_mm": averaging.accuracy_m() * 1000.0,
                "latitude": lat,
                "longitude": lon,
                "altitude_m": alt,
            }
        )

    async def _commit(self, averaging: _Averaging) -> None:
        """Make the mean the fixed base and save it, then report completion.

        A Corrected survey-in first stops its Relay instance and restores the
        receiver's input settings. If they can't be restored it commits
        nothing: saving to flash would make the temporary input permanent.
        """
        if not await self._stop_corrections():
            self._finish("aborted", "input_not_restored")
            logger.error(
                "Survey-in aborted: the receiver's input settings couldn't be "
                "restored, so nothing was committed or saved to flash"
            )
            return
        lat, lon, alt = ecef_to_llh(*averaging.mean())
        await self._device.configure_fixed_base(
            FixedBaseConfig(
                latitude=lat,
                longitude=lon,
                altitude_m=alt,
                accuracy_mm=max(1, round(averaging.accuracy_m() * 1000.0)),
            )
        )
        await self._device.save_to_flash()
        self._finish("completed")
        logger.info("Survey-in completed: fixed base %.9f, %.9f, %.4f m", lat, lon, alt)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _finish(
        self, outcome: SurveyOutcome, abort_reason: SurveyAbortReason | None = None
    ) -> None:
        assert self._progress is not None
        self._progress = self._progress.model_copy(
            update={
                "active": False,
                "valid": outcome == "completed",
                "outcome": outcome,
                "abort_reason": abort_reason,
            }
        )

    async def _forget_if_receiver_changed(self) -> None:
        """Drop the survey once a different receiver is connected."""
        if self._progress is None or self._device.driver is self._survey_driver:
            return
        await self._stop_task()
        self._progress = None
        self._survey_driver = None

    async def _stop_corrections(self) -> bool:
        """Stop a Corrected survey-in's Relay instance and restore the input.

        Safe to call more than once, and on a plain survey.

        Returns:
            Whether the receiver's input settings are as they were (True
            when there was nothing to restore).
        """
        feed, self._feed = self._feed, None
        if feed is None:
            return True
        try:
            await feed.stop()
        except Exception:
            logger.exception("Could not stop the survey's Relay instance")
        restored = await self._restore_correction_input()
        if self._progress is not None:
            self._progress = self._progress.model_copy(
                update={"source_connected": False}
            )
        return restored

    async def _restore_correction_input(self) -> bool:
        try:
            await self._device.end_correction_input()
        except Exception:
            logger.exception("Could not restore the receiver's input settings")
            return False
        return True

    def _is_running(self) -> bool:
        """Whether the station is averaging.

        A Receiver survey-in never blocks a new start: starting again
        restarts the receiver's survey, as it always has.
        """
        return self._task is not None and not self._task.done()

    async def _stop_task(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
