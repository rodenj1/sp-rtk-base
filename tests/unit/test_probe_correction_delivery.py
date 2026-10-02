"""Tests for the correction delivery probe's verdict (bench diagnosis, #197).

Seam under test: ``verdict()`` in ``tools/probe_correction_delivery.py``, the
pure function that decides red or green from a survey's progress.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

SCRIPT = Path(__file__).parents[2] / "tools" / "probe_correction_delivery.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_correction_delivery", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load()


def _spread(p95: float) -> dict[str, float]:
    return {"count": 50, "p50": p95 / 2, "p95": p95, "max": p95}


def _healthy() -> dict[str, Any]:
    return {
        "corrections_written": 500,
        "corrections_dropped": 0,
        "correction_write_failures": 0,
        "receiver_rtcm3_messages": 500,
        "diagnostics": {
            "written_at_receiver_read": 480,
            "frame_age_s": _spread(1.5),
            "sample_interval_s": _spread(1.1),
            "driver": {
                "rtcm": {
                    "1077": {"received": 90, "used": 90, "not_used": 0},
                    "1005": {"received": 2, "used": 2, "not_used": 0},
                }
            },
        },
    }


class TestVerdict:
    def test_a_healthy_link_is_green(self) -> None:
        assert probe.verdict(_healthy()) == []

    def test_dropped_frames_are_red(self) -> None:
        progress = _healthy() | {"corrections_dropped": 12}

        assert any("dropped" in reason for reason in probe.verdict(progress))

    def test_stale_frames_are_red(self) -> None:
        progress = _healthy()
        progress["diagnostics"]["frame_age_s"] = _spread(31.0)

        assert any("old" in reason for reason in probe.verdict(progress))

    def test_a_slow_survey_sample_is_red(self) -> None:
        progress = _healthy()
        progress["diagnostics"]["sample_interval_s"] = _spread(4.2)

        assert any("sample" in reason for reason in probe.verdict(progress))

    def test_frames_the_receiver_didnt_count_are_red(self) -> None:
        progress = _healthy() | {"receiver_rtcm3_messages": 200}

        assert any("receiver" in reason for reason in probe.verdict(progress))

    def test_frames_written_since_the_receivers_last_count_are_not_missing(
        self,
    ) -> None:
        # Counted 480 of the 480 written by the last read; 20 more since.
        progress = _healthy() | {"receiver_rtcm3_messages": 480}

        assert probe.verdict(progress) == []

    def test_corrections_the_receiver_didnt_use_are_red(self) -> None:
        progress = _healthy()
        progress["diagnostics"]["driver"]["rtcm"]["1077"] = {
            "received": 90,
            "used": 30,
            "not_used": 60,
        }

        assert any("used" in reason for reason in probe.verdict(progress))

    def test_unknown_verdicts_are_not_counted_as_unused(self) -> None:
        progress = _healthy()
        progress["diagnostics"]["driver"]["rtcm"]["1077"] = {
            "received": 90,
            "used": 30,
            "not_used": 0,
        }

        assert probe.verdict(progress) == []

    def test_a_base_position_never_used_is_red(self) -> None:
        progress = _healthy()
        progress["diagnostics"]["driver"]["rtcm"]["1005"] = {
            "received": 3,
            "used": 0,
            "not_used": 3,
        }

        assert any("1005" in reason for reason in probe.verdict(progress))

    def test_no_frames_written_at_all_is_red(self) -> None:
        progress = _healthy() | {"corrections_written": 0, "receiver_rtcm3_messages": 0}

        assert probe.verdict(progress)
