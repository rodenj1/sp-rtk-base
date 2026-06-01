"""Tests for the pure formatting helpers used by the Dashboard page.

The page-rendering closure itself drives NiceGUI elements and can't
be meaningfully unit-tested without a full browser harness (that's
what tests/e2e covers).  These helpers are pure functions extracted
to make the rate/uptime display logic verifiable.
"""

from __future__ import annotations

import pytest

from sp_rtk_base.ui.pages.dashboard import (
    _format_byte_rate,
    _format_bytes,
    _format_count_rate,
    _format_uptime,
)


class TestFormatBytes:
    """Bytes-to-human formatter (used for totals)."""

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (1023, "1023 B"),
            (1024, "1.0 KB"),
            (2048, "2.0 KB"),
            (1024 * 1024, "1.0 MB"),
            (1024 * 1024 * 1024, "1.00 GB"),
            (5 * 1024 * 1024 * 1024, "5.00 GB"),
        ],
    )
    def test_format_bytes(self, n: int, expected: str) -> None:
        assert _format_bytes(n) == expected


class TestFormatByteRate:
    """Byte-rate formatter — primary signal for 'is data flowing'."""

    @pytest.mark.parametrize(
        ("per_second", "expected"),
        [
            (0.0, "0 B/s"),
            (50.0, "50 B/s"),
            (1023.0, "1023 B/s"),
            (1024.0, "1.0 KB/s"),
            (5 * 1024.0, "5.0 KB/s"),
            (1024.0 * 1024, "1.0 MB/s"),
            (1024.0 * 1024 * 1024, "1.00 GB/s"),
        ],
    )
    def test_format_byte_rate(self, per_second: float, expected: str) -> None:
        assert _format_byte_rate(per_second) == expected


class TestFormatCountRate:
    """Count-rate formatter (messages, frames, chunks per second).

    Precision shrinks as the rate grows because 0.02 msg/s is
    meaningfully different from 0.50 msg/s, but at 150/s the decimals
    are noise.
    """

    @pytest.mark.parametrize(
        ("per_second", "unit", "expected"),
        [
            (0.0, "/s", "0.00/s"),
            (0.5, "/s", "0.50/s"),
            (9.9, "/s", "9.90/s"),
            (10.0, "/s", "10.0/s"),
            (99.9, "/s", "99.9/s"),
            (100.0, "/s", "100/s"),
            (12345.0, "/s", "12345/s"),
            (5.0, " msg/s", "5.00 msg/s"),
        ],
    )
    def test_format_count_rate(
        self, per_second: float, unit: str, expected: str
    ) -> None:
        assert _format_count_rate(per_second, unit) == expected


class TestFormatUptime:
    """Uptime formatter — must stay human-readable at multi-day scale.

    Reported pain: long-uptime instances showed e.g. "120:34:56" with
    unbounded hours.  Operators care about "is it been up ~5 days" not
    "120 hours".
    """

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (None, "--"),
            (0, "0s"),
            (1, "1s"),
            (59, "59s"),
            (60, "1m 00s"),
            (90, "1m 30s"),
            (3599, "59m 59s"),
            (3600, "1h 00m"),
            (3661, "1h 01m"),
            (86399, "23h 59m"),
            (86400, "1d 00h"),
            (4 * 86400 + 3 * 3600, "4d 03h"),
            (86400 * 365, "1y 00d"),
            (86400 * 400, "1y 35d"),
        ],
    )
    def test_format_uptime(self, seconds: float | None, expected: str) -> None:
        assert _format_uptime(seconds) == expected
