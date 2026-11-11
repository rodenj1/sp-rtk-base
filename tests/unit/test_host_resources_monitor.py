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


def _reading(**fields: object) -> HostResourcesReading:
    """A sample in which every reading read, but for *fields*."""
    good: dict[str, object] = {
        "cpu_seconds": {"user": 100.0, "idle": 900.0},
        "memory_total_bytes": 4_000_000_000,
        "memory_available_bytes": 3_000_000_000,
        "cpu_temperature_millicelsius": 50_000,
        "undervoltage_alarm": 0,
        "root_filesystem_size_bytes": 32_000_000_000,
        "root_filesystem_available_bytes": 20_000_000_000,
    }
    return HostResourcesReading.model_validate(good | fields)


def _temperature(
    millicelsius: int | NotSupported | None = 50_000,
) -> HostResourcesReading:
    return _reading(cpu_temperature_millicelsius=millicelsius)


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


def _cpu(**seconds: float) -> HostResourcesReading:
    return HostResourcesReading(cpu_seconds=seconds)


def test_cpu_seconds_are_the_latest_totals_by_mode() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_cpu(user=100.0, idle=900.0), clock.tick())
    monitor.observe(_cpu(user=101.5, idle=902.0), clock.tick())

    assert monitor.snapshot(clock.now).cpu_seconds == {"user": 101.5, "idle": 902.0}


def test_an_iowait_total_that_falls_is_clamped_and_then_grows_again() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_cpu(iowait=50.0), clock.tick())
    monitor.observe(_cpu(iowait=48.0), clock.tick())
    held = monitor.snapshot(clock.now).cpu_seconds
    monitor.observe(_cpu(iowait=49.5), clock.tick())

    assert held == {"iowait": 50.0}
    # It grows by the 1.5 s since the fall, never back down to 48.
    assert monitor.snapshot(clock.now).cpu_seconds == {"iowait": 51.5}


def test_cpu_seconds_hold_their_last_totals_through_no_reading() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_reading(cpu_seconds={"user": 100.0}), clock.tick())
    monitor.observe(_reading(cpu_seconds=None), clock.tick())

    snapshot = monitor.snapshot(clock.now + 60.0)
    assert snapshot.cpu_seconds == {"user": 100.0}
    assert snapshot.last_sample_age_seconds == 62.0


def test_before_any_cpu_reading_there_are_no_cpu_seconds() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_reading(cpu_seconds=None), clock.tick())

    assert monitor.snapshot(clock.now).cpu_seconds is None


def test_memory_and_root_filesystem_figures_are_in_bytes() -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(
        _reading(
            memory_total_bytes=3_980_000_000,
            memory_available_bytes=2_100_000_000,
            root_filesystem_size_bytes=31_000_000_000,
            root_filesystem_available_bytes=24_500_000_000,
        ),
        clock.tick(),
    )

    snapshot = monitor.snapshot(clock.now)
    assert snapshot.memory_total_bytes == 3_980_000_000
    assert snapshot.memory_available_bytes == 2_100_000_000
    assert snapshot.root_filesystem_size_bytes == 31_000_000_000
    assert snapshot.root_filesystem_available_bytes == 24_500_000_000


@pytest.mark.parametrize(
    "field",
    [
        "memory_total_bytes",
        "memory_available_bytes",
        "root_filesystem_size_bytes",
        "root_filesystem_available_bytes",
    ],
)
def test_a_failed_memory_or_filesystem_read_is_no_reading(field: str) -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_reading(), clock.tick())
    monitor.observe(_reading(**{field: None}), clock.tick())

    snapshot = monitor.snapshot(clock.now)
    assert getattr(snapshot, field) is None
    assert snapshot.last_sample_age_seconds == 2.0


@pytest.mark.parametrize(("seconds_later", "current"), [(6.0, True), (6.1, False)])
def test_memory_and_filesystem_figures_over_6_s_old_are_no_reading(
    seconds_later: float, current: bool
) -> None:
    clock = Clock()
    monitor = HostResourcesMonitor()

    monitor.observe(_reading(), clock.tick())

    snapshot = monitor.snapshot(clock.now + seconds_later)
    figures = [
        snapshot.memory_total_bytes,
        snapshot.memory_available_bytes,
        snapshot.root_filesystem_size_bytes,
        snapshot.root_filesystem_available_bytes,
    ]
    assert all((figure is not None) is current for figure in figures)


def test_before_any_reading_memory_and_filesystem_are_no_reading() -> None:
    monitor = HostResourcesMonitor()

    snapshot = monitor.snapshot(1000.0)

    assert snapshot.memory_total_bytes is None
    assert snapshot.root_filesystem_available_bytes is None


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
