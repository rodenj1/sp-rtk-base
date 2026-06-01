"""Tests for the pure formatting helpers used by the Dashboard page.

The page-rendering closure itself drives NiceGUI elements and can't
be meaningfully unit-tested without a full browser harness (that's
what tests/e2e covers).  These helpers are pure functions extracted
to make the rate/uptime display logic verifiable.
"""

from __future__ import annotations

import pytest

from sp_rtk_base.ui.pages.dashboard import (
    RelayControlState,
    _compute_relay_control_state,
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


class TestComputeRelayControlState:
    """Tests for the single-button toggle decision logic.

    The Dashboard used to have two buttons (Start + Stop) with the
    inactive one greyed out.  v0.3.22 collapses them into one toggle
    whose text/icon/color depends on the relay's running state, and
    which is disabled with an explanatory message when stopped-but-
    preconditions-not-met.
    """

    def test_running_yields_stop_button(self) -> None:
        """Running relay → red enabled Stop button, no config message."""
        s = _compute_relay_control_state(
            is_running=True, has_input=True, enabled_destination_count=2
        )
        assert s == RelayControlState(
            primary_text="Stop",
            primary_icon="stop",
            primary_color="red",
            primary_enabled=True,
            config_message=None,
        )

    def test_running_button_enabled_even_with_empty_config(self) -> None:
        """A running relay must still be Stop-able even if config drifted.

        Outputs can be deleted while the engine is running; we don't
        want to strand the operator with no way to stop the engine
        just because their destination list is empty mid-run.
        """
        s = _compute_relay_control_state(
            is_running=True, has_input=False, enabled_destination_count=0
        )
        assert s.primary_text == "Stop"
        assert s.primary_enabled is True
        assert s.config_message is None

    def test_stopped_ready_yields_enabled_start(self) -> None:
        """Stopped relay with full config → green enabled Start button."""
        s = _compute_relay_control_state(
            is_running=False, has_input=True, enabled_destination_count=1
        )
        assert s == RelayControlState(
            primary_text="Start",
            primary_icon="play_arrow",
            primary_color="green",
            primary_enabled=True,
            config_message=None,
        )

    def test_stopped_no_input_disables_and_points_at_input_page(self) -> None:
        """No input → button disabled, message routes to Input page."""
        s = _compute_relay_control_state(
            is_running=False, has_input=False, enabled_destination_count=0
        )
        assert s.primary_text == "Start"
        assert s.primary_enabled is False
        assert s.config_message is not None
        assert "Input page" in s.config_message

    def test_stopped_no_destinations_disables_and_points_at_outputs_page(self) -> None:
        """Input set but zero enabled destinations → message routes to Outputs."""
        s = _compute_relay_control_state(
            is_running=False, has_input=True, enabled_destination_count=0
        )
        assert s.primary_text == "Start"
        assert s.primary_enabled is False
        assert s.config_message is not None
        assert "Outputs page" in s.config_message

    def test_no_input_takes_precedence_over_destinations_message(self) -> None:
        """When both are missing, prioritise the Input message.

        Input must be configured first anyway — telling the operator
        to fix two pages in one banner is confusing.  Direct them to
        Input first; the destinations check fires on the next refresh
        once they've saved an input.
        """
        s = _compute_relay_control_state(
            is_running=False, has_input=False, enabled_destination_count=0
        )
        assert s.config_message is not None
        assert "Input page" in s.config_message
        assert "Outputs page" not in s.config_message


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
