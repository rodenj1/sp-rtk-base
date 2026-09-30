"""Signal Quality, tested through ``SignalQualityService``.

The seam is the service: tests feed it Signal Snapshots through its real
survey-in input (a ``DeviceService`` connected to the fake GPS driver),
drive it with an injected clock, and assert on ``current()``.
"""

from __future__ import annotations

import pytest

from sp_rtk_base.models.signal_quality_models import (
    Band,
    Signal,
    SignalQualityNoData,
    SignalQualityVerdict,
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
    await device.connect("FAKE", 115200)
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
                Signal(constellation="GPS", satellite=sv, band=Band.L1, cn0_dbhz=l1)
            )
        if l2 is not None:
            signals.append(
                Signal(constellation="GPS", satellite=sv, band=Band.L2, cn0_dbhz=l2)
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
        Signal(constellation="GALILEO", satellite=sv, band=Band.L1, cn0_dbhz=cn0)
        for sv, cn0 in enumerate(l1, start=1)
    )
    reading = await _reading_for(signals)
    assert reading.l1_strength_dbhz == pytest.approx(49.0)


@pytest.mark.asyncio()
async def test_a_satellite_is_usable_only_with_a_signal_at_35_dbhz_or_more() -> None:
    signals = (
        Signal(constellation="GPS", satellite=1, band=Band.L1, cn0_dbhz=35.0),
        Signal(constellation="GPS", satellite=2, band=Band.L1, cn0_dbhz=34.9),
        Signal(constellation="GPS", satellite=2, band=Band.L2, cn0_dbhz=36.0),
        Signal(constellation="GPS", satellite=3, band=Band.L1, cn0_dbhz=30.0),
        Signal(constellation="GLONASS", satellite=1, band=Band.OTHER, cn0_dbhz=40.0),
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
