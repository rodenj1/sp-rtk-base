"""Bluetooth link strength, tested through ``BluetoothLinkMonitor``.

The seam is the monitor: tests feed it a sequence of Input statuses (as
the Relay status reports them, ``None`` while the Relay is stopped) at
times from a fake clock, and assert on ``current()``.  The Input
statuses are the Relay's own ``InputStatus``, carrying ``link_rssi``, the
relative dB reading the Relay reports on a Bluetooth Input.
"""

from __future__ import annotations

import pytest
from sp_rtk_base_relay import InputStatus

from sp_rtk_base.models.bluetooth_link_models import (
    BluetoothLinkNoReading,
    BluetoothLinkVerdict,
)
from sp_rtk_base.services.bluetooth_link.monitor import BluetoothLinkMonitor


def _input_status(
    link_rssi: int | None = 0,
    *,
    connected: bool = True,
    source_type: str = "Bluetooth",
) -> InputStatus:
    """The Relay's Input status; only the fields the monitor reads vary."""
    return InputStatus(
        connected=connected,
        source_type=source_type,
        bytes_received=0,
        messages_received=0,
        seconds_since_last_data=0.0,
        reconnect_attempts=0,
        reconnect_successes=0,
        connected_since=None,
        link_rssi=link_rssi,
    )


class Clock:
    """Fake monotonic clock: each ``tick()`` is one 2 s poll later."""

    def __init__(self) -> None:
        self.now = 1000.0

    def tick(self, seconds: float = 2.0) -> float:
        self.now += seconds
        return self.now


def _fed(*readings: int | None) -> tuple[BluetoothLinkMonitor, Clock]:
    """A monitor fed one Bluetooth Input status per reading, 2 s apart."""
    monitor, clock = BluetoothLinkMonitor(), Clock()
    for rssi in readings:
        monitor.observe(_input_status(link_rssi=rssi), clock.tick())
    return monitor, clock


def _verdict(monitor: BluetoothLinkMonitor, clock: Clock) -> BluetoothLinkVerdict:
    reading = monitor.current(clock.now)
    assert isinstance(reading, BluetoothLinkVerdict)
    return reading


@pytest.mark.parametrize(
    ("rssi", "expected"),
    [(3, "Good"), (0, "Good"), (-1, "Marginal"), (-5, "Marginal"), (-6, "Poor")],
)
def test_threshold_edges(rssi: int, expected: str) -> None:
    assert _verdict(*_fed(rssi)).level == expected


def test_a_lone_dip_among_zeros_leaves_the_verdict_good() -> None:
    verdict = _verdict(*_fed(0, 0, -1, 0, 0))
    assert (verdict.level, verdict.median_db) == ("Good", 0)


def test_the_verdict_is_the_median_of_the_last_10_seconds() -> None:
    # 2 s apart, a 10 s window holds five readings: the oldest -9s fall out.
    monitor, clock = _fed(-9, -9, -9, -9, -9, 0, 0)
    assert _verdict(monitor, clock).median_db == -9
    monitor.observe(_input_status(link_rssi=0), clock.tick())
    assert _verdict(monitor, clock).median_db == 0


@pytest.mark.parametrize(
    ("readings", "expected"),
    [
        # No level is held: a median one step past a line changes level.
        ((0, -1, -1), "Marginal"),
        ((-3, -6, -6), "Poor"),
        # Climbing back uses the same lines.
        ((-2, 0, 0), "Good"),
        ((-7, -5, -5), "Marginal"),
    ],
)
def test_a_level_changes_as_soon_as_the_median_crosses_its_line(
    readings: tuple[int, ...], expected: str
) -> None:
    assert _verdict(*_fed(*readings)).level == expected


@pytest.mark.parametrize(
    ("readings", "median_db", "expected"),
    [
        ((0, -1), -1, "Marginal"),
        ((-5, -6), -6, "Poor"),
        ((-1, -2, -3, -4), -3, "Marginal"),
    ],
)
def test_an_even_window_is_judged_from_its_lower_median(
    readings: tuple[int, ...], median_db: int, expected: str
) -> None:
    verdict = _verdict(*_fed(*readings))
    assert (verdict.median_db, verdict.level) == (median_db, expected)


NO_READING = "The adapter gave no reading."


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (None, "Waiting for the first reading."),
        (
            _input_status(connected=False, link_rssi=None),
            "The Bluetooth link is disconnected.",
        ),
        (_input_status(link_rssi=None), NO_READING),
    ],
    ids=["relay stopped", "disconnected", "no reading"],
)
def test_no_reading_is_never_a_level(status: InputStatus | None, reason: str) -> None:
    monitor, clock = _fed(-9, -9, -9)
    monitor.observe(status, clock.tick())
    assert monitor.current(clock.now) == BluetoothLinkNoReading(reason=reason)


def test_after_the_relay_stops_it_waits_for_the_first_reading() -> None:
    # The row only shows while the Relay runs, so the reading left by the
    # last poll of a stopped Relay is what shows until the next poll.
    monitor, clock = BluetoothLinkMonitor(), Clock()
    monitor.observe(None, clock.tick())
    assert monitor.current(clock.now) == BluetoothLinkNoReading(
        reason="Waiting for the first reading."
    )


def test_the_window_does_not_carry_across_a_disconnect() -> None:
    monitor, clock = _fed(-9, -9, -9, -9)
    monitor.observe(_input_status(connected=False, link_rssi=None), clock.tick())
    monitor.observe(_input_status(link_rssi=0), clock.tick())
    assert _verdict(monitor, clock).median_db == 0.0


@pytest.mark.parametrize("source_type", ["Serial", "TCP", "ntrip"])
def test_a_non_bluetooth_input_gives_no_verdict(source_type: str) -> None:
    monitor, clock = _fed(-9, -9)
    monitor.observe(_input_status(source_type=source_type), clock.tick())
    assert monitor.current(clock.now) is None


@pytest.mark.parametrize(
    ("readings", "subvalue"),
    [
        ((3,), None),
        ((0,), None),
        ((0, 0, -1), None),
        ((-3,), "3 dB below comfortable"),
        ((-7, -7, -8), "7 dB below comfortable"),
        ((-1, -2), "2 dB below comfortable"),
    ],
)
def test_the_subvalue_says_how_far_below_comfortable_a_negative_median_is(
    readings: tuple[int, ...], subvalue: str | None
) -> None:
    assert _verdict(*_fed(*readings)).subvalue == subvalue


def test_a_verdict_no_poll_has_renewed_for_10_seconds_is_no_reading() -> None:
    monitor, clock = _fed(0, 0)
    clock.tick(9.9)
    assert _verdict(monitor, clock).level == "Good"
    clock.tick(0.1)
    assert monitor.current(clock.now) == BluetoothLinkNoReading(reason=NO_READING)
