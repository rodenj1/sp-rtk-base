"""Tests for the Corrected survey-in bench logger's summary (issue #197).

Seam under test: ``summarise()`` in ``tools/bench_corrected_survey.py``,
the pure function that turns a run's samples into the figures #197 records.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType

import pytest

from sp_rtk_base.services.geodesy import ecef_to_llh, llh_to_ecef

SCRIPT = Path(__file__).parents[2] / "tools" / "bench_corrected_survey.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_corrected_survey", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = _load()
ORIGIN = llh_to_ecef(32.7329015, -117.2362788, 27.94)


def _sample(t: float, rtk: str, dx_m: float = 0.0, **progress: object) -> object:
    lat, lon, alt = ecef_to_llh(ORIGIN[0] + dx_m, ORIGIN[1], ORIGIN[2])
    return bench.Sample(
        t=t,
        rtk_status=rtk,
        latitude=lat,
        longitude=lon,
        altitude_m=alt,
        horizontal_accuracy_m=0.012,
        vertical_accuracy_m=0.016,
        progress=dict(progress),
    )


class TestTimeToFirstFixed:
    def test_is_the_first_fixed_sample_after_the_start(self) -> None:
        samples = [_sample(0, "none"), _sample(20, "float"), _sample(42, "fixed")]

        assert bench.summarise(samples, []).time_to_first_fixed_s == 42

    def test_is_none_when_never_fixed(self) -> None:
        samples = [_sample(0, "none"), _sample(20, "float")]

        assert bench.summarise(samples, []).time_to_first_fixed_s is None


class TestSpread:
    def test_is_the_3d_std_of_fixed_positions_in_the_window(self) -> None:
        # Fixed from t=10: +/-10 mm along ECEF X for 5 min, then a 1 m jump.
        samples = [_sample(0, "float", dx_m=5.0)]
        samples += [
            _sample(10 + i, "fixed", dx_m=0.01 if i % 2 else -0.01) for i in range(300)
        ]
        samples += [_sample(400, "fixed", dx_m=1.0)]

        summary = bench.summarise(samples, [])

        assert summary.spread_5min_mm == pytest.approx(10.0, abs=0.2)

    def test_is_none_until_the_window_is_covered(self) -> None:
        samples = [_sample(i, "fixed") for i in range(100)]

        summary = bench.summarise(samples, [])

        assert summary.spread_5min_mm is None
        assert summary.spread_30min_mm is None

    def test_typical_accuracy_is_the_median_fixed_3d_estimate(self) -> None:
        samples = [_sample(i, "fixed") for i in range(10)]

        summary = bench.summarise(samples, [])

        assert summary.typical_accuracy_3d_mm == pytest.approx(math.hypot(12.0, 16.0))


class TestOutcome:
    def test_reports_the_last_progress(self) -> None:
        samples = [
            _sample(0, "none", outcome="running"),
            _sample(
                400,
                "fixed",
                outcome="completed",
                duration_seconds=300,
                mean_accuracy_mm=14.2,
                abort_reason=None,
            ),
        ]

        summary = bench.summarise(samples, [])

        assert summary.outcome == "completed"
        assert summary.observation_time_s == 300
        assert summary.final_accuracy_mm == 14.2
        assert summary.run_time_s == 400


class TestAcks:
    def test_counts_failures_and_latency_percentiles(self) -> None:
        acks = [
            bench.Ack(t=i, ok=i != 3, latency_ms=float(10 * (i + 1))) for i in range(10)
        ]

        summary = bench.summarise([], acks)

        assert summary.acks == 10
        assert summary.ack_failures == 1
        # the failed read isn't a latency: the median of the other nine
        assert summary.ack_p50_ms == pytest.approx(60.0)
        assert summary.ack_max_ms == 100.0


class TestReviewFixes:
    def test_a_position_read_that_failed_is_left_out_of_the_spread(self) -> None:
        samples = [
            _sample(i, "fixed", dx_m=0.01 if i % 2 else -0.01) for i in range(301)
        ]
        failed = bench.Sample(
            t=150.5,
            rtk_status="fixed",
            latitude=0.0,
            longitude=0.0,
            altitude_m=0.0,
            horizontal_accuracy_m=0.0,
            vertical_accuracy_m=0.0,
            position_ok=False,
        )
        samples.insert(151, failed)

        summary = bench.summarise(samples, [])

        assert summary.spread_5min_mm == pytest.approx(10.0, abs=0.2)

    def test_a_window_ending_a_second_short_still_counts(self) -> None:
        # The survey commits after 300 Fixed seconds: the last sample may be
        # at first_fixed + 299.
        samples = [_sample(10 + i, "fixed") for i in range(300)]

        assert bench.summarise(samples, []).spread_5min_mm is not None

    def test_the_committed_position_is_recorded(self) -> None:
        samples = [
            _sample(
                400,
                "fixed",
                outcome="completed",
                valid=True,
                latitude=32.5,
                longitude=-117.25,
                altitude_m=27.9,
            )
        ]

        summary = bench.summarise(samples, [])

        assert (summary.latitude, summary.longitude, summary.altitude_m) == (
            32.5,
            -117.25,
            27.9,
        )

    def test_no_position_is_recorded_unless_the_survey_completed(self) -> None:
        samples = [_sample(400, "none", outcome="aborted", latitude=32.5)]

        assert bench.summarise(samples, []).latitude is None

    def test_slow_receiver_reads_are_counted(self) -> None:
        acks = [bench.Ack(t=0, ok=True, latency_ms=50.0)] * 8 + [
            bench.Ack(t=0, ok=True, latency_ms=1500.0)
        ] * 2

        assert bench.summarise([], acks).ack_slow == 2
