"""The host sampler, with a fake host reader and a fake clock."""

from __future__ import annotations

import asyncio

import pytest

from sp_rtk_base.models.host_resources_models import HostResourcesReading
from sp_rtk_base.services.host_resources.service import HostResourcesService

# A sample in which every reading read.
READING = HostResourcesReading(
    cpu_seconds={"user": 100.0, "idle": 900.0},
    memory_total_bytes=4_000_000_000,
    memory_available_bytes=3_000_000_000,
    cpu_temperature_millicelsius=50_000,
    undervoltage_alarm=0,
    root_filesystem_size_bytes=32_000_000_000,
    root_filesystem_available_bytes=20_000_000_000,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeHost:
    """Serves readings in turn; an exception in the list is raised."""

    def __init__(self, *readings: HostResourcesReading | Exception) -> None:
        self.readings = list(readings)
        self.reads = 0

    def read(self) -> HostResourcesReading:
        self.reads += 1
        reading = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        if isinstance(reading, Exception):
            raise reading
        return reading


@pytest.mark.asyncio()
async def test_a_sample_reads_the_host_into_the_snapshot() -> None:
    clock = Clock()
    service = HostResourcesService(FakeHost(READING), clock=clock)

    await service.sample_once()
    clock.now += 1.0
    snapshot = service.current()

    assert snapshot.cpu_temperature_celsius is not None
    assert snapshot.cpu_temperature_celsius.value == 50.0
    assert snapshot.last_sample_age_seconds == 1.0


@pytest.mark.asyncio()
async def test_a_host_read_that_raises_is_no_reading_not_a_crash() -> None:
    clock = Clock()
    service = HostResourcesService(FakeHost(READING, RuntimeError("boom")), clock=clock)

    await service.sample_once()
    clock.now += 2.0
    await service.sample_once()

    snapshot = service.current()
    assert snapshot.cpu_temperature_celsius is not None
    assert snapshot.cpu_temperature_celsius.value is None
    assert snapshot.last_sample_age_seconds == 2.0


@pytest.mark.asyncio()
async def test_the_sampler_runs_in_the_background_until_stopped() -> None:
    host = FakeHost(READING)
    service = HostResourcesService(host, clock=Clock())

    service.start()
    service.start()  # idempotent
    for _ in range(100):
        if host.reads:
            break
        await asyncio.sleep(0.01)
    await service.stop()
    await service.stop()  # idempotent

    assert host.reads == 1
    assert service.current().cpu_temperature_celsius is not None
