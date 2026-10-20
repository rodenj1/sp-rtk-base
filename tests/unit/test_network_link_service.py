"""The Network link sampler, with a fake host reader and a fake clock."""

from __future__ import annotations

import asyncio

import pytest

from sp_rtk_base.models.network_link_models import (
    AddressReading,
    EgressReading,
    HostReading,
    InterfaceReading,
)
from sp_rtk_base.services.network_link.service import NetworkLinkService

READING = HostReading(
    interfaces=(InterfaceReading(name="eth0", kind="ethernet", carrier=True),),
    addresses=(AddressReading(interface="eth0", address="192.0.2.10", prefixlen=24),),
    egress=EgressReading(source="192.0.2.10"),
    wifi=(),
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeHost:
    """Serves readings in turn; an exception in the list is raised."""

    def __init__(self, *readings: HostReading | Exception) -> None:
        self.readings = list(readings)
        self.reads = 0
        self.closed = False

    def read(self) -> HostReading:
        self.reads += 1
        reading = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        if isinstance(reading, Exception):
            raise reading
        return reading

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio()
async def test_a_sample_reads_the_host_into_the_snapshot() -> None:
    clock = Clock()
    service = NetworkLinkService(FakeHost(READING), clock=clock)

    await service.sample_once()
    clock.now += 1.0
    snapshot = service.current()

    assert [link.name for link in snapshot.links] == ["eth0"]
    assert snapshot.links[0].state == "Connected"
    assert snapshot.last_sample_age_seconds == 1.0


@pytest.mark.asyncio()
async def test_a_host_read_that_raises_is_no_reading_not_a_crash() -> None:
    clock = Clock()
    service = NetworkLinkService(FakeHost(READING, RuntimeError("boom")), clock=clock)

    await service.sample_once()
    clock.now += 2.0
    await service.sample_once()

    (link,) = service.current().links
    assert link.state == "No reading"


@pytest.mark.asyncio()
async def test_the_sampler_runs_in_the_background_until_stopped() -> None:
    host = FakeHost(READING)
    service = NetworkLinkService(host, clock=Clock())

    service.start()
    service.start()  # idempotent
    for _ in range(100):
        if host.reads:
            break
        await asyncio.sleep(0.01)
    await service.stop()

    assert host.reads == 1
    assert host.closed is True
    assert service.current().links[0].name == "eth0"
