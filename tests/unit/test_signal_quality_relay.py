"""Signal Quality in Relay mode, tested through ``SignalQualityService``.

While the Relay owns the receiver's port, Signal Quality is fed RTCM MSM
Frames (real MSM4 / MSM7 encodings) through the service's Frame consumer
input, with an injected clock.  The same sky must read the same as in
survey-in, where the fake driver supplies Snapshots.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from sp_rtk_base_relay import Frame, FrameSubscription

from sp_rtk_base.models.device_models import GnssConstellation
from sp_rtk_base.models.signal_quality_models import (
    Band,
    Signal,
    SignalQualityNoData,
    SignalQualityVerdict,
)
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.fake import FakeGpsDriver
from sp_rtk_base.services.signal_quality.service import SignalQualityService
from tests.unit.msm_frames import epoch_frames, msm_frame, other_frame, sky


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Relay:
    """A service in Relay mode, fed Frames epoch by epoch."""

    def __init__(self, service: SignalQualityService, clock: Clock) -> None:
        self.service, self.clock = service, clock

    def at(self, t: float, frames: Iterable[Frame]) -> None:
        self.clock.now = 1000.0 + t
        for frame in frames:
            self.service.take_frame(frame)


def _relay_mode() -> Relay:
    clock = Clock()
    service = SignalQualityService(DeviceService(), clock=clock)
    service.relay_started(FrameSubscription())
    return Relay(service, clock)


def test_msm_from_the_relay_gives_a_verdict_on_the_dashboard() -> None:
    relay = _relay_mode()

    relay.at(0, epoch_frames(sky(20, l1=52.0, l2=50.0)))

    reading = relay.service.current("dashboard")
    assert isinstance(reading, SignalQualityVerdict)
    assert reading.level == "Good"
    assert (reading.l1_strength_dbhz, reading.l2_strength_dbhz) == (52.0, 50.0)
    assert reading.usable_satellites == 20


async def _survey_reading(signals: tuple[Signal, ...]) -> SignalQualityVerdict:
    driver = FakeGpsDriver()
    driver.set_signals(signals)
    device = DeviceService()
    device.set_driver(driver)
    await device.connect("FAKE", 115200)
    service = SignalQualityService(device, clock=Clock())
    await service.poll_once()
    reading = service.current("survey")
    assert isinstance(reading, SignalQualityVerdict)
    return reading


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    "signals",
    [sky(28, l1=52.0, l2=51.0), sky(12, l1=46.0, l2=38.0), sky(24, l1=50.0, l2=None)],
    ids=["clear-sky", "marginal", "l1-only"],
)
async def test_the_same_sky_reads_the_same_in_both_modes(
    signals: tuple[Signal, ...],
) -> None:
    relay = _relay_mode()
    relay.at(0, epoch_frames(signals))

    assert relay.service.current("dashboard") == await _survey_reading(signals)


def test_msm7_c_n0_is_read_at_its_finer_resolution() -> None:
    relay = _relay_mode()

    relay.at(0, epoch_frames(sky(20, l1=45.3125, l2=41.0625), msm7=True))

    reading = relay.service.current("dashboard")
    assert isinstance(reading, SignalQualityVerdict)
    assert (reading.l1_strength_dbhz, reading.l2_strength_dbhz) == (45.3125, 41.0625)


def test_beidou_bands_follow_its_own_signal_codes() -> None:
    relay = _relay_mode()
    beidou_only = tuple(
        Signal(
            constellation=GnssConstellation.BEIDOU,
            satellite=sv,
            band=band,
            cn0_dbhz=cn0,
        )
        for sv in range(1, 21)
        for band, cn0 in ((Band.L1, 48.0), (Band.L2, 42.0))  # B1I '2I', B2I '7I'
    )

    relay.at(0, epoch_frames(beidou_only))

    reading = relay.service.current("dashboard")
    assert isinstance(reading, SignalQualityVerdict)
    assert (reading.l1_strength_dbhz, reading.l2_strength_dbhz) == (48.0, 42.0)


def test_a_snapshot_closes_on_the_epoch_s_last_msm() -> None:
    relay = _relay_mode()
    *leading, last = epoch_frames(
        sky(20, l1=52.0, l2=50.0)
    )  # GPS, GLONASS, Galileo | BeiDou

    relay.at(0, leading)  # more to follow: nothing judged yet
    assert isinstance(relay.service.current("dashboard"), SignalQualityNoData)

    relay.at(0.2, [last])  # the epoch's last MSM
    assert isinstance(relay.service.current("dashboard"), SignalQualityVerdict)


def test_an_epoch_whose_last_msm_is_lost_is_still_judged() -> None:
    relay = _relay_mode()
    first = epoch_frames(sky(20, l1=52.0, l2=50.0))
    second = epoch_frames(sky(20, l1=51.0, l2=49.0))

    relay.at(0, first[:-1])  # the epoch's last Frame never arrives
    relay.at(1.0, second[:1])  # next epoch starts: GPS again

    reading = relay.service.current("dashboard")
    assert isinstance(reading, SignalQualityVerdict)
    assert reading.l1_strength_dbhz == 52.0  # judged on the first epoch's Frames


def test_an_epoch_left_open_for_too_long_is_closed() -> None:
    relay = _relay_mode()
    gps, galileo = epoch_frames(sky(20, l1=52.0, l2=50.0))[:2]

    relay.at(0, [gps])
    relay.at(1.6, [galileo])  # > 1.5 s later: the first epoch is closed on its own

    reading = relay.service.current("dashboard")
    assert isinstance(reading, SignalQualityVerdict)


def _reason(service: SignalQualityService, view: str = "dashboard") -> str:
    reading = service.current(view)  # type: ignore[arg-type]
    assert isinstance(reading, SignalQualityNoData), reading
    return reading.reason


def test_the_dashboard_says_when_the_relay_is_stopped() -> None:
    service = SignalQualityService(DeviceService(), clock=Clock())

    assert _reason(service) == "Relay is stopped. Signal Quality shows while it runs."


def test_a_relay_with_no_rtcm_for_ten_seconds_says_so() -> None:
    relay = _relay_mode()

    relay.at(9.9, [])
    assert _reason(relay.service) == "Waiting for signal data from the receiver."
    relay.at(10, [])
    assert _reason(relay.service) == "No RTCM is arriving from the receiver."


def test_a_relay_with_rtcm_but_no_msm_for_ten_seconds_says_so() -> None:
    relay = _relay_mode()

    for t in range(0, 12, 2):
        relay.at(t, [other_frame(1005), other_frame(1230)])

    assert _reason(relay.service) == (
        "No MSM messages in the RTCM stream. Enable MSM4 or MSM7 output on the "
        "receiver port the Relay reads."
    )


def test_a_relay_verdict_goes_stale_when_msm_stops() -> None:
    relay = _relay_mode()
    relay.at(0, epoch_frames(sky(20, l1=52.0, l2=50.0)))

    for t in range(2, 12, 2):
        relay.at(t, [other_frame(1005)])

    assert _reason(relay.service) == (
        "No MSM messages in the RTCM stream. Enable MSM4 or MSM7 output on the "
        "receiver port the Relay reads."
    )


@pytest.mark.asyncio()
async def test_handoff_starts_the_relay_verdict_afresh_and_survey_shows_it_too() -> (
    None
):
    clock = Clock()
    driver = FakeGpsDriver()
    driver.set_signals(sky(20, l1=52.0, l2=50.0))
    device = DeviceService()
    device.set_driver(driver)
    await device.connect("FAKE", 115200)
    service = SignalQualityService(device, clock=clock)
    await service.poll_once()
    assert isinstance(service.current("survey"), SignalQualityVerdict)

    # Handoff: the device disconnects, the Relay starts on the same port.
    await device.disconnect()
    service.relay_started(FrameSubscription())
    assert _reason(service, "survey") == "Waiting for signal data from the receiver."

    clock.now += 2
    await service.poll_once()  # the poller ticks; the device is gone by design
    for frame in epoch_frames(sky(20, l1=46.0, l2=42.0)):
        service.take_frame(frame)

    for view in ("survey", "dashboard"):
        reading = service.current(view)  # type: ignore[arg-type]
        assert isinstance(reading, SignalQualityVerdict)
        assert reading.l1_strength_dbhz == 46.0  # nothing carried from survey-in


def test_stopping_the_relay_ends_its_verdict() -> None:
    relay = _relay_mode()
    relay.at(0, epoch_frames(sky(20, l1=52.0, l2=50.0)))

    relay.service.relay_stopped()

    assert (
        _reason(relay.service)
        == "Relay is stopped. Signal Quality shows while it runs."
    )
    assert (
        _reason(relay.service, "survey")
        == "Connect the receiver to see Signal Quality."
    )


@pytest.mark.asyncio()
async def test_frames_from_the_relay_subscription_reach_the_verdict() -> None:
    import asyncio

    clock = Clock()
    service = SignalQualityService(DeviceService(), clock=clock)
    subscription = FrameSubscription()
    service.relay_started(subscription)

    for frame in epoch_frames(sky(20, l1=52.0, l2=50.0)):
        subscription.offer(frame)  # as the Relay's hub does, from its thread
    for _ in range(100):
        if isinstance(service.current("dashboard"), SignalQualityVerdict):
            break
        await asyncio.sleep(0.01)

    assert isinstance(service.current("dashboard"), SignalQualityVerdict)
    service.relay_stopped()
    assert subscription.closed


def test_msm4_and_msm7_for_one_constellation_stay_one_epoch() -> None:
    relay = _relay_mode()
    gps = {sv: {2: 50.0, 16: 48.0} for sv in range(1, 9)}
    galileo = {sv: {2: 50.0, 14: 48.0} for sv in range(1, 9)}

    relay.at(
        0,
        [
            msm_frame(1074, gps, more_follow=True),  # both MSM4 and MSM7 enabled
            msm_frame(1077, gps, more_follow=True),
            msm_frame(1097, galileo),
        ],
    )

    reading = relay.service.current("dashboard")
    assert isinstance(reading, SignalQualityVerdict)
    assert reading.usable_satellites == 16  # GPS and Galileo judged together
