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
    return HostResourcesReading(cpu_temperature_millicelsius=millicelsius)


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
