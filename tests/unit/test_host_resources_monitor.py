"""Host resources, tested through ``HostResourcesMonitor``.

The seam is the monitor: tests feed it raw host readings at times from a
fake clock, 2 s apart like the sampler, and assert on the snapshot.
"""

from __future__ import annotations

import pytest

from sp_rtk_base.models.host_resources_models import (
    NOT_SUPPORTED,
    HostResourcesReading,
    NotSupported,
)
from sp_rtk_base.services.host_resources.monitor import HostResourcesMonitor


class Clock:
    """Fake monotonic clock: each ``tick()`` is one 2 s sample later."""

    def __init__(self) -> None:
        self.now = 1000.0

    def tick(self, seconds: float = 2.0) -> float:
        self.now += seconds
        return self.now


def test_cpu_temperature_is_converted_from_millidegrees_to_celsius() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(
        HostResourcesReading(cpu_temperature_millicelsius=48_312), clock.tick()
    )

    temperature = monitor.snapshot(clock.now).cpu_temperature_celsius
    assert temperature is not None
    assert temperature.value == 48.312


def _temperature(
    millicelsius: int | NotSupported | None = 50_000,
) -> HostResourcesReading:
    # Every other reading Not supported, so only the temperature can fail.
    return HostResourcesReading(
        cpu_temperature_millicelsius=millicelsius, undervoltage_alarm=NOT_SUPPORTED
    )


def test_a_host_with_no_cpu_thermal_zone_has_no_temperature_reading() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_temperature(NOT_SUPPORTED), clock.tick())

    assert monitor.snapshot(clock.now).cpu_temperature_celsius is None


def test_a_temperature_that_goes_away_becomes_not_supported() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_temperature(50_000), clock.tick())
    monitor.observe(_temperature(NOT_SUPPORTED), clock.tick())

    assert monitor.snapshot(clock.now).cpu_temperature_celsius is None


def test_before_any_reading_the_temperature_is_not_known_to_be_supported() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(HostResourcesReading(), clock.tick())

    snapshot = monitor.snapshot(clock.now)
    assert snapshot.cpu_temperature_celsius is None
    assert snapshot.last_sample_age_seconds is None


def test_a_failed_temperature_read_is_no_reading() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_temperature(50_000), clock.tick())
    monitor.observe(_temperature(None), clock.tick())

    temperature = monitor.snapshot(clock.now).cpu_temperature_celsius
    assert temperature is not None
    assert temperature.value is None


def test_a_read_after_a_failed_one_is_current_again() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_temperature(50_000), clock.tick())
    monitor.observe(_temperature(None), clock.tick())
    monitor.observe(_temperature(51_000), clock.tick())

    temperature = monitor.snapshot(clock.now).cpu_temperature_celsius
    assert temperature is not None
    assert temperature.value == 51.0


@pytest.mark.parametrize(
    ("seconds_later", "current"), [(6.0, True), (6.1, False), (60.0, False)]
)
def test_a_temperature_over_6_s_old_is_no_reading(
    seconds_later: float, current: bool
) -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_temperature(50_000), clock.tick())

    temperature = monitor.snapshot(clock.now + seconds_later).cpu_temperature_celsius
    assert temperature is not None
    assert (temperature.value == 50.0) is current
    assert (temperature.value is None) is not current


def test_the_last_sample_age_is_since_the_last_sample_where_every_reading_read() -> (
    None
):
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_temperature(50_000), clock.tick())
    monitor.observe(_temperature(None), clock.tick())

    assert monitor.snapshot(clock.now + 1.0).last_sample_age_seconds == 3.0


def test_a_not_supported_reading_still_makes_a_full_sample() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_temperature(NOT_SUPPORTED), clock.tick())

    assert monitor.snapshot(clock.now).last_sample_age_seconds == 0.0


def _undervoltage(alarm: int | NotSupported | None) -> HostResourcesReading:
    return HostResourcesReading(undervoltage_alarm=alarm)


def _observe_alarms(
    monitor: HostResourcesMonitor, clock: Clock, *alarms: int | NotSupported | None
) -> None:
    for alarm in alarms:
        monitor.observe(_undervoltage(alarm), clock.tick())


def test_undervoltage_is_the_rpi_volt_alarm_as_a_flag() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    _observe_alarms(monitor, clock, 1)

    undervoltage = monitor.snapshot(clock.now).undervoltage
    assert undervoltage is not None
    assert undervoltage.value is True


def test_each_rise_of_the_undervoltage_alarm_is_one_event() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    _observe_alarms(monitor, clock, 0, 1, 0, 1)

    snapshot = monitor.snapshot(clock.now)
    assert snapshot.undervoltage_events == 2
    assert snapshot.undervoltage is not None
    assert snapshot.undervoltage.value is True


def test_an_alarm_held_across_samples_is_one_event() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    _observe_alarms(monitor, clock, 0, 1, 1, 1, 0)

    snapshot = monitor.snapshot(clock.now)
    assert snapshot.undervoltage_events == 1
    assert snapshot.undervoltage is not None
    assert snapshot.undervoltage.value is False


def test_an_alarm_already_raised_at_the_first_sample_is_an_event() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    _observe_alarms(monitor, clock, 1)

    assert monitor.snapshot(clock.now).undervoltage_events == 1


def test_a_host_with_no_rpi_volt_has_neither_undervoltage_reading() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    _observe_alarms(monitor, clock, NOT_SUPPORTED, NOT_SUPPORTED)

    snapshot = monitor.snapshot(clock.now)
    assert snapshot.undervoltage is None
    assert snapshot.undervoltage_events is None


def test_before_any_reading_undervoltage_is_not_known_to_be_supported() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    _observe_alarms(monitor, clock, None)

    snapshot = monitor.snapshot(clock.now)
    assert snapshot.undervoltage is None
    assert snapshot.undervoltage_events is None


def test_a_failed_undervoltage_read_is_no_reading_and_the_count_holds() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    _observe_alarms(monitor, clock, 0, 1, None)

    snapshot = monitor.snapshot(clock.now)
    assert snapshot.undervoltage is not None
    assert snapshot.undervoltage.value is None
    assert snapshot.undervoltage_events == 1


def test_a_stale_undervoltage_reading_is_no_reading_and_the_count_holds() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    _observe_alarms(monitor, clock, 1)

    snapshot = monitor.snapshot(clock.now + 6.1)
    assert snapshot.undervoltage is not None
    assert snapshot.undervoltage.value is None
    assert snapshot.undervoltage_events == 1


def test_an_alarm_held_across_a_failed_read_is_one_event() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    _observe_alarms(monitor, clock, 1, None, 1)

    assert monitor.snapshot(clock.now).undervoltage_events == 1


def test_the_event_count_survives_rpi_volt_going_away_and_coming_back() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    _observe_alarms(monitor, clock, 1, NOT_SUPPORTED)
    assert monitor.snapshot(clock.now).undervoltage_events is None

    _observe_alarms(monitor, clock, 0, 1)
    assert monitor.snapshot(clock.now).undervoltage_events == 2
