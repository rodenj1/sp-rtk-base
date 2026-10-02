"""Tests for the correction link's measuring helpers (bench diagnosis, #197).

Seam under test: the public helpers in services/link_diagnostics.py.
"""

from __future__ import annotations

import pytest

from sp_rtk_base.services.link_diagnostics import Sampler, msm_epoch_age_s
from tests.unit.msm_frames import msm_frame, other_frame

# 2026-10-02 12:00:00 UTC; GPS time of week (Friday) = 5 d 12 h + 18 s leap.
NOW_UNIX = 1_790_942_400.0
NOW_TOW_MS = (5 * 86_400 + 12 * 3_600 + 18) * 1000


class TestMsmEpochAge:
    @pytest.mark.parametrize("message_number", [1074, 1077, 1094, 1097])
    def test_a_gps_or_galileo_msm_is_as_old_as_its_epoch(
        self, message_number: int
    ) -> None:
        frame = msm_frame(message_number, {1: {2: 45.0}}, epoch_ms=NOW_TOW_MS - 2_500)

        assert msm_epoch_age_s(frame.data, NOW_UNIX) == pytest.approx(2.5)

    def test_an_epoch_from_last_week_wraps(self) -> None:
        week_ms = 604_800_000
        frame = msm_frame(1077, {1: {2: 45.0}}, epoch_ms=week_ms - 1_000)
        just_after_rollover = NOW_UNIX - NOW_TOW_MS / 1000.0 + 0.5

        assert msm_epoch_age_s(frame.data, just_after_rollover) == pytest.approx(1.5)

    @pytest.mark.parametrize(
        "frame", [other_frame(1005), msm_frame(1084, {1: {2: 45.0}})]
    )
    def test_other_frames_have_no_age(self, frame: object) -> None:
        assert msm_epoch_age_s(frame.data, NOW_UNIX) is None  # type: ignore[attr-defined]


class TestSampler:
    def test_summarises_the_values(self) -> None:
        sampler = Sampler()
        for value in range(1, 101):
            sampler.add(float(value))

        spread = sampler.spread()

        assert spread is not None
        assert (spread.count, spread.p50, spread.p95, spread.max) == (
            100,
            50.5,
            95.0,
            100.0,
        )

    def test_is_empty_before_the_first_value(self) -> None:
        assert Sampler().spread() is None

    def test_keeps_only_the_recent_values_but_counts_them_all(self) -> None:
        sampler = Sampler(size=3)
        for value in (100.0, 1.0, 2.0, 3.0):
            sampler.add(value)

        spread = sampler.spread()

        assert spread is not None and (spread.count, spread.max) == (4, 3.0)
