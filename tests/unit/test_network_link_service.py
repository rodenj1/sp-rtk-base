"""The Network link sampler, with a fake host reader and a fake clock."""

from __future__ import annotations

import asyncio
import threading

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


class SlowHost(FakeHost):
    """A host whose read blocks until released, recording whether it was
    closed while a read was in flight."""

    def __init__(self) -> None:
        super().__init__(READING)
        self.started = threading.Event()
        self.release = threading.Event()
        self.reading = False
        self.closed_mid_read = False

    def read(self) -> HostReading:
        self.reading = True
        self.started.set()
        self.release.wait(5.0)
        self.reading = False
        return super().read()

    def close(self) -> None:
        self.closed_mid_read = self.reading
        super().close()


@pytest.mark.asyncio()
async def test_stopping_waits_for_a_read_in_flight_before_closing_the_host() -> None:
    host = SlowHost()
    service = NetworkLinkService(host, clock=Clock())
    service.start()
    assert await asyncio.to_thread(host.started.wait, 5.0)

    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0.05)
    assert host.closed is False
    host.release.set()
    await asyncio.wait_for(stopping, 5.0)

    assert host.closed is True
    assert host.closed_mid_read is False


@pytest.mark.asyncio()
async def test_a_stopped_sampler_never_reads_the_closed_host_again() -> None:
    host = FakeHost(READING)
    service = NetworkLinkService(host, clock=Clock())
    await service.sample_once()
    await service.stop()

    await service.sample_once()

    assert host.reads == 1
