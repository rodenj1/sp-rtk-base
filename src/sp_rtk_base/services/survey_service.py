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

logger = logging.getLogger(__name__)

# The station samples the receiver's position once a second; each accepted
# observation counts one second towards the minimum duration.
SAMPLE_INTERVAL_S: float = 1.0

# An application-averaged survey that hasn't completed by the time the
# wall-clock time reaches max(this, HARD_CAP_MULTIPLE x the minimum duration)
# gives up with ``accuracy_not_reached``.
HARD_CAP_MIN_S: float = 3600.0
HARD_CAP_MULTIPLE: float = 3.0

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


class SurveyService:
    """Runs one Survey-in at a time and reports its progress."""

    def __init__(
        self,
        device: DeviceService,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._device = device
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
            try:
                await self._device.begin_correction_input()
                feed = CorrectionFeed(self._device, source.to_relay_config())
                await feed.start()
            except Exception:
                await self._restore_correction_input()
                raise
            self._survey_driver = self._device.driver
            self._feed = feed
            self._progress = SurveyInProgress(
                active=True,
                averaged_by="application",
                outcome="running",
                min_duration_seconds=config.min_duration_seconds,
                accuracy_limit_mm=config.accuracy_limit_mm,
                correction_source=source.name,
                source_connected=True,
            )
            limits = _Limits(
                config.min_duration_seconds, config.accuracy_limit_mm, corrected=True
            )
            self._task = asyncio.create_task(self._run_application_survey(limits))
            logger.info("Corrected survey-in started against %s", source.name)

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
        try:
            while True:
                position = await self._device.get_survey_position()
                if limits.corrected:
                    await self._update_correction_progress(position)
                if self._is_observation(position, limits):
                    averaging.add(position)
                    self._update_application_progress(averaging)
                    if (
                        averaging.count >= limits.min_duration_s
                        and averaging.accuracy_m() <= limit_m
                    ):
                        await self._commit(averaging)
                        return
                if self._clock() - started_at >= hard_cap_s:
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

    @staticmethod
    def _is_observation(position: SurveyPosition, limits: _Limits) -> bool:
        """Corrected: RTK Fixed solutions only. Plain: any valid 3D fix."""
        if limits.corrected:
            return position.fix_ok and position.rtk_status == "fixed"
        return position.fix_ok

    async def _update_correction_progress(self, position: SurveyPosition) -> None:
        """The receiver's RTK status and correction age, and the source's state."""
        assert self._progress is not None
        connected, last_error = (
            await self._feed.status() if self._feed is not None else (False, None)
        )
        self._progress = self._progress.model_copy(
            update={
                "rtk_status": position.rtk_status,
                "correction_age_s": position.correction_age_s,
                "source_connected": connected,
                "source_last_error": last_error,
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
