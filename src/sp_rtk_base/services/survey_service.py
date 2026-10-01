"""Survey-in, whoever averages it.

A receiver with a Receiver survey-in (``DeviceCapability.SURVEY_IN``) averages
its own position. On one without, the station averages instead: it reads a
high-precision position once a second, keeps the valid 3D fixes, and commits
their mean as the fixed base itself, whether or not a page is open.

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

from sp_rtk_base.models.device_models import (
    DeviceCapability,
    FixedBaseConfig,
    SurveyAbortReason,
    SurveyInConfig,
    SurveyInProgress,
    SurveyOutcome,
    SurveyPosition,
)
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
            active=True, averaged_by="application", outcome="running"
        )
        self._task = asyncio.create_task(self._run_application_survey(config))

    async def _run_application_survey(self, config: SurveyInConfig) -> None:
        started_at = self._clock()
        hard_cap_s = max(
            HARD_CAP_MIN_S, HARD_CAP_MULTIPLE * config.min_duration_seconds
        )
        limit_m = config.accuracy_limit_mm / 1000.0
        averaging = _Averaging()
        next_sample_at = started_at
        try:
            while True:
                position = await self._device.get_survey_position()
                if position.fix_ok:
                    averaging.add(position)
                    self._update_application_progress(averaging)
                    if (
                        averaging.count >= config.min_duration_seconds
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
        """Make the mean the fixed base and save it, then report completion."""
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
