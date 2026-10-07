"""Signal Quality, tested through ``SignalQualityService``.

The seam is the service: tests feed it Signal Snapshots through its real
survey-in input (a ``DeviceService`` connected to the fake GPS driver),
drive it with an injected clock, and assert on ``current()``.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from sp_rtk_base.models.device_models import GnssConstellation, SerialLink
from sp_rtk_base.models.signal_quality_models import (
    Band,
    Signal,
    SignalQualityNoData,
    SignalQualityVerdict,
    SignalSnapshot,
)
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.fake import FakeGpsDriver
from sp_rtk_base.services.signal_quality.service import SignalQualityService


class Clock:
    """Injected monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _connected(driver: FakeGpsDriver) -> DeviceService:
    device = DeviceService()
    device.set_driver(driver)
    await device.connect(SerialLink(port="FAKE", baud_rate=115200))
    return device


@pytest.mark.asyncio()
async def test_clear_sky_on_the_fake_receiver_reads_good() -> None:
    device = await _connected(FakeGpsDriver())
    service = SignalQualityService(device, clock=Clock())

    await service.poll_once()

    reading = service.current()
    assert isinstance(reading, SignalQualityVerdict)
    assert reading.level == "Good"
    assert reading.cause == ""
    assert reading.l1_strength_dbhz == pytest.approx(52.0, abs=0.5)
    assert reading.l2_strength_dbhz == pytest.approx(51.0, abs=0.5)
    assert reading.usable_satellites == 28


def _sky(satellites: int, l1: float | None, l2: float | None) -> tuple[Signal, ...]:
    """*satellites* GPS satellites, each with the given L1 / L2 C/N0 (None = absent)."""
    signals: list[Signal] = []
    for sv in range(1, satellites + 1):
        if l1 is not None:
            signals.append(
                Signal(
                    constellation=GnssConstellation.GPS,
                    satellite=sv,
                    band=Band.L1,
                    cn0_dbhz=l1,
                )
            )
        if l2 is not None:
            signals.append(
                Signal(
                    constellation=GnssConstellation.GPS,
                    satellite=sv,
                    band=Band.L2,
                    cn0_dbhz=l2,
                )
            )
    return tuple(signals)


async def _reading_for(signals: tuple[Signal, ...]) -> SignalQualityVerdict:
    driver = FakeGpsDriver()
    driver.set_signals(signals)
    service = SignalQualityService(await _connected(driver), clock=Clock())
    await service.poll_once()
    reading = service.current()
    assert isinstance(reading, SignalQualityVerdict)
    return reading


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    ("l1", "expected"),
    [
        (52.0, "Good"),
        (44.0, "Good"),
        (43.9, "Marginal"),
        (40.0, "Marginal"),
        (39.9, "Poor"),
    ],
)
async def test_l1_band_strength_thresholds(l1: float, expected: str) -> None:
    reading = await _reading_for(_sky(20, l1=l1, l2=50.0))
    assert (reading.l1_level, reading.level) == (expected, expected)


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    ("l2", "expected"),
    [(41.0, "Good"), (40.9, "Marginal"), (37.0, "Marginal"), (36.9, "Poor")],
)
async def test_l2_band_strength_thresholds(l2: float, expected: str) -> None:
    reading = await _reading_for(_sky(20, l1=50.0, l2=l2))
    assert (reading.l2_level, reading.level) == (expected, expected)


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    ("satellites", "expected"),
    [(15, "Good"), (14, "Marginal"), (10, "Marginal"), (9, "Poor")],
)
async def test_usable_satellite_thresholds(satellites: int, expected: str) -> None:
    reading = await _reading_for(_sky(satellites, l1=50.0, l2=50.0))
    assert reading.usable_satellites == satellites
    assert (reading.satellites_level, reading.level) == (expected, expected)


@pytest.mark.asyncio()
async def test_the_worst_measure_wins_and_every_measure_at_that_level_is_named() -> (
    None
):
    reading = await _reading_for(
        _sky(12, l1=50.0, l2=38.0)
    )  # L2 and satellites Marginal
    assert reading.level == "Marginal"
    assert reading.l1_level == "Good"
    assert reading.cause == "L2 weak, few satellites"


@pytest.mark.asyncio()
async def test_a_missing_l2_band_is_poor() -> None:
    reading = await _reading_for(_sky(24, l1=50.0, l2=None))  # L1-only antenna
    assert reading.level == "Poor"
    assert reading.l2_strength_dbhz is None
    assert reading.cause == "no L2 signal"


@pytest.mark.asyncio()
async def test_band_strength_is_the_mean_of_the_four_strongest_signals() -> None:
    l1 = [30.0, 52.0, 25.0, 48.0, 50.0, 46.0, 20.0]  # strongest four: 52, 50, 48, 46
    signals = tuple(
        Signal(
            constellation=GnssConstellation.GALILEO,
            satellite=sv,
            band=Band.L1,
            cn0_dbhz=cn0,
        )
        for sv, cn0 in enumerate(l1, start=1)
    )
    reading = await _reading_for(signals)
    assert reading.l1_strength_dbhz == pytest.approx(49.0)


@pytest.mark.asyncio()
async def test_a_satellite_is_usable_only_with_a_signal_at_35_dbhz_or_more() -> None:
    signals = (
        Signal(
            constellation=GnssConstellation.GPS,
            satellite=1,
            band=Band.L1,
            cn0_dbhz=35.0,
        ),
        Signal(
            constellation=GnssConstellation.GPS,
            satellite=2,
            band=Band.L1,
            cn0_dbhz=34.9,
        ),
        Signal(
            constellation=GnssConstellation.GPS,
            satellite=2,
            band=Band.L2,
            cn0_dbhz=36.0,
        ),
        Signal(
            constellation=GnssConstellation.GPS,
            satellite=3,
            band=Band.L1,
            cn0_dbhz=30.0,
        ),
        Signal(
            constellation=GnssConstellation.GLONASS,
            satellite=1,
            band=Band.OTHER,
            cn0_dbhz=40.0,
        ),
    )
    reading = await _reading_for(signals)
    assert reading.usable_satellites == 3  # GPS 1, GPS 2 (via L2), GLONASS 1


@pytest.mark.asyncio()
async def test_without_a_connected_receiver_it_asks_for_one() -> None:
    service = SignalQualityService(DeviceService(), clock=Clock())

    await service.poll_once()

    assert service.current() == SignalQualityNoData(
        reason="Connect the receiver to see Signal Quality."
    )


@pytest.mark.asyncio()
async def test_a_verdict_does_not_survive_a_reconnect() -> None:
    driver = FakeGpsDriver()
    device = await _connected(driver)
    service = SignalQualityService(device, clock=Clock())
    await service.poll_once()
    assert isinstance(service.current(), SignalQualityVerdict)

    await device.disconnect()
    await service.poll_once()  # the poller ticks while disconnected
    await device.connect(SerialLink(port="FAKE", baud_rate=115200))

    assert isinstance(service.current(), SignalQualityNoData)


# ---------------------------------------------------------------------------
# A steady verdict: smoothing, deadband and staleness (#168)
# ---------------------------------------------------------------------------


class Feed:
    """A connected fake receiver whose sky the test sets, poll by poll."""

    def __init__(
        self, driver: FakeGpsDriver, service: SignalQualityService, clock: Clock
    ):
        self.driver, self.service, self.clock = driver, service, clock

    async def poll_at(
        self, t: float, signals: tuple[Signal, ...]
    ) -> SignalQualityVerdict:
        self.clock.now = 1000.0 + t
        self.driver.set_signals(signals)
        await self.service.poll_once()
        reading = self.service.current()
        assert isinstance(reading, SignalQualityVerdict), reading
        return reading


async def _feed() -> Feed:
    driver, clock = FakeGpsDriver(), Clock()
    return Feed(
        driver, SignalQualityService(await _connected(driver), clock=clock), clock
    )


@pytest.mark.asyncio()
async def test_a_single_snapshot_dip_does_not_change_the_verdict() -> None:
    feed = await _feed()
    for t in (0, 2, 4, 6, 8):
        await feed.poll_at(t, _sky(20, l1=52.0, l2=50.0))

    reading = await feed.poll_at(10, _sky(20, l1=30.0, l2=50.0))  # one bad epoch

    assert reading.level == "Good"
    # The window is the last 10 s: t=0 has aged out, leaving 2, 4, 6, 8 and 10.
    assert reading.l1_strength_dbhz == pytest.approx((52.0 * 4 + 30.0) / 5)


@pytest.mark.asyncio()
async def test_usable_satellites_is_the_window_median_rounded_down() -> None:
    feed = await _feed()
    for t, n in ((0, 20), (2, 20), (4, 11), (6, 11)):  # median 15.5
        reading = await feed.poll_at(t, _sky(n, l1=50.0, l2=50.0))

    assert reading.usable_satellites == 15
    assert reading.satellites_level == "Good"


# Polls every 2 s, as the poller does; holding a value for five polls fills
# the 10 s window with it, so each step's final reading is judged on it.


async def _hold(
    feed: Feed, start: int, signals: tuple[Signal, ...]
) -> SignalQualityVerdict:
    for i in range(4):
        await feed.poll_at(start + 2 * i, signals)
    return await feed.poll_at(start + 8, signals)


@pytest.mark.asyncio()
async def test_a_value_oscillating_at_a_threshold_does_not_flicker() -> None:
    feed = await _feed()
    await _hold(feed, 0, _sky(20, l1=52.0, l2=50.0))
    levels = [
        (await feed.poll_at(10 + 2 * i, _sky(20, l1=l1, l2=50.0))).l1_level
        for i, l1 in enumerate([44.1, 43.9] * 6)
    ]
    assert levels == ["Good"] * 12


@pytest.mark.asyncio()
async def test_leaving_a_level_needs_a_one_db_crossing_entering_does_not() -> None:
    feed = await _feed()
    steps = [
        (52.0, "Good"),
        (43.1, "Good"),
        (42.9, "Marginal"),
        (44.5, "Marginal"),
        (45.0, "Good"),
        (38.5, "Poor"),
        (40.0, "Poor"),
        (41.0, "Marginal"),
    ]
    got = [
        (await _hold(feed, 10 * n, _sky(20, l1=l1, l2=50.0))).l1_level
        for n, (l1, _) in enumerate(steps)
    ]
    assert got == [level for _, level in steps]


@pytest.mark.asyncio()
async def test_usable_satellites_leave_a_level_only_one_satellite_past_it() -> None:
    feed = await _feed()
    steps = [
        (20, "Good"),
        (14, "Good"),
        (13, "Marginal"),
        (15, "Marginal"),
        (16, "Good"),
    ]
    got = [
        (await _hold(feed, 10 * n, _sky(count, l1=50.0, l2=50.0))).satellites_level
        for n, (count, _) in enumerate(steps)
    ]
    assert got == [level for _, level in steps]


def _stop_answering(driver: FakeGpsDriver) -> None:
    driver.set_signal_poll_error(RuntimeError("No NAV-SIG response"))


NO_ANSWER = SignalQualityNoData(reason="The receiver did not answer the signal poll.")


@pytest.mark.asyncio()
async def test_a_verdict_goes_stale_ten_seconds_after_the_last_answer() -> None:
    feed = await _feed()
    await feed.poll_at(0, _sky(20, l1=52.0, l2=50.0))
    _stop_answering(feed.driver)

    for t in (2, 4, 6, 8):
        feed.clock.now = 1000.0 + t
        await feed.service.poll_once()
    feed.clock.now = 1009.9
    assert isinstance(feed.service.current(), SignalQualityVerdict)  # still fresh

    feed.clock.now = 1010.0
    await feed.service.poll_once()
    assert feed.service.current() == NO_ANSWER  # never the old verdict


@pytest.mark.asyncio()
async def test_a_receiver_that_never_answers_is_waited_on_for_ten_seconds() -> None:
    feed = await _feed()
    _stop_answering(feed.driver)

    feed.clock.now = 1000.0
    await feed.service.poll_once()
    feed.clock.now = 1009.0
    await feed.service.poll_once()
    assert feed.service.current() == SignalQualityNoData(
        reason="Waiting for signal data from the receiver."
    )

    feed.clock.now = 1010.0
    await feed.service.poll_once()
    assert feed.service.current() == NO_ANSWER


@pytest.mark.asyncio()
async def test_a_receiver_that_answers_again_shows_a_verdict_again() -> None:
    feed = await _feed()
    await feed.poll_at(0, _sky(20, l1=52.0, l2=50.0))
    _stop_answering(feed.driver)
    feed.clock.now = 1012.0
    await feed.service.poll_once()
    assert feed.service.current() == NO_ANSWER

    feed.driver.set_signal_poll_error(None)
    reading = await feed.poll_at(14, _sky(20, l1=52.0, l2=50.0))
    assert reading.level == "Good"


@pytest.mark.asyncio()
async def test_a_poll_is_skipped_while_the_previous_one_has_not_returned() -> None:
    feed = await _feed()
    release, calls = threading.Event(), 0
    real = feed.driver.get_signal_snapshot

    def slow() -> SignalSnapshot:  # e.g. stuck behind a long configuration write
        nonlocal calls
        calls += 1
        release.wait(timeout=5)
        return real()

    feed.driver.get_signal_snapshot = slow  # type: ignore[method-assign]
    first = asyncio.create_task(feed.service.poll_once())
    await asyncio.sleep(0.05)

    await feed.service.poll_once()  # the next tick, while the first is stuck
    release.set()
    await first

    assert calls == 1
    assert isinstance(feed.service.current(), SignalQualityVerdict)


@pytest.mark.asyncio()
async def test_a_stale_verdict_does_not_hold_the_next_one() -> None:
    feed = await _feed()
    await feed.poll_at(0, _sky(20, l1=52.0, l2=50.0))  # Good

    reading = await feed.poll_at(
        100, _sky(20, l1=43.5, l2=50.0)
    )  # long after it went stale

    assert reading.l1_level == "Marginal"  # judged fresh, not held at Good


@pytest.mark.asyncio()
async def test_a_driver_without_snapshots_keeps_waiting_rather_than_not_answering() -> (
    None
):
    feed = await _feed()
    feed.driver.set_signal_poll_error(NotImplementedError())

    for t in (0, 6, 12, 30):
        feed.clock.now = 1000.0 + t
        await feed.service.poll_once()

    assert feed.service.current() == SignalQualityNoData(
        reason="Waiting for signal data from the receiver."
    )


@pytest.mark.asyncio()
async def test_a_band_missing_from_most_of_the_window_is_missing() -> None:
    feed = await _feed()
    with_l2, without_l2 = _sky(20, l1=50.0, l2=50.0), _sky(20, l1=50.0, l2=None)
    for i, signals in enumerate([with_l2, without_l2, without_l2, without_l2]):
        await feed.poll_at(2 * i, signals)

    reading = await feed.poll_at(8, without_l2)  # L2 in 1 of 5 Snapshots

    assert reading.level == "Poor"
    assert reading.cause == "no L2 signal"


@pytest.mark.asyncio()
async def test_a_band_present_in_most_of_the_window_is_judged_on_those() -> None:
    feed = await _feed()
    with_l2, without_l2 = _sky(20, l1=50.0, l2=48.0), _sky(20, l1=50.0, l2=None)
    for i, signals in enumerate([with_l2, without_l2, with_l2, without_l2]):
        await feed.poll_at(2 * i, signals)

    reading = await feed.poll_at(8, with_l2)  # L2 in 3 of 5 Snapshots

    assert reading.l2_strength_dbhz == pytest.approx(48.0)
    assert reading.level == "Good"
