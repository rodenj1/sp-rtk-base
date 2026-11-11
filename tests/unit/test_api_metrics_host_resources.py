"""The Host resources metrics on /metrics, scraped through the real endpoint.

The real host sampler is fed by a fake host reader and a fake clock, then
injected through FastAPI dependency overrides beside a Network link
sampler, as the Network link metrics tests do.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import AppSettings
from sp_rtk_base.models.host_resources_models import (
    NOT_SUPPORTED,
    HostResourcesReading,
)
from sp_rtk_base.models.network_link_models import HostReading
from sp_rtk_base.services import (
    get_config_service,
    get_host_resources_service,
    get_metrics_service,
    get_network_link_service,
    get_relay_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.host_resources.service import HostResourcesService
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.network_link.service import NetworkLinkService
from sp_rtk_base.services.relay_service import RelayService

WALL_NOW = 1_800_000_000.0
TEMPERATURE = "sp_rtk_base_host_cpu_temperature_celsius"
TIMESTAMP = "sp_rtk_base_host_last_sample_timestamp_seconds"
NETWORK_TIMESTAMP = "sp_rtk_base_network_last_sample_timestamp_seconds"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeHost:
    """Serves one reading per sample; an exception is a read that raised."""

    def __init__(self, *readings: HostResourcesReading | Exception) -> None:
        self.readings = list(readings)

    def read(self) -> HostResourcesReading:
        reading = self.readings.pop(0)
        if isinstance(reading, Exception):
            raise reading
        return reading


class _FailingNetworkHost:
    def read(self) -> HostReading:
        raise RuntimeError("no network host")

    def close(self) -> None:
        pass


async def _fed(
    *readings: HostResourcesReading | Exception, clock: _Clock | None = None
) -> HostResourcesService:
    """The real sampler after one sample per reading, 2 s apart."""
    clock = clock or _Clock()
    service = HostResourcesService(_FakeHost(*readings), clock=clock)
    for _ in readings:
        clock.now += 2.0
        await service.sample_once()
    return service


@dataclass(frozen=True)
class Sample:
    type: str
    unit: str
    labels: dict[str, str]
    value: float


async def _scrape(service: HostResourcesService) -> dict[str, list[Sample]]:
    """Scrape /metrics beside a Network link sampler whose every read fails:
    the Host resources and Network link samples, by name."""
    network = NetworkLinkService(_FailingNetworkHost(), clock=_Clock())
    await network.sample_once()
    relay = MagicMock(spec=RelayService)
    relay.is_running = False
    config = MagicMock(spec=ConfigService)
    config.get_settings.return_value = AppSettings(metrics_enabled=True)
    app = create_api_app()
    metrics = MetricsService(wall_clock=lambda: WALL_NOW)
    app.dependency_overrides[get_relay_service] = lambda: relay
    app.dependency_overrides[get_metrics_service] = lambda: metrics
    app.dependency_overrides[get_config_service] = lambda: config
    app.dependency_overrides[get_network_link_service] = lambda: network
    app.dependency_overrides[get_host_resources_service] = lambda: service
    text = TestClient(app).get("/metrics").text
    samples: dict[str, list[Sample]] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name.startswith(("sp_rtk_base_host_", NETWORK_TIMESTAMP)):
                samples.setdefault(sample.name, []).append(
                    Sample(family.type, family.unit, dict(sample.labels), sample.value)
                )
    return samples


@pytest.mark.asyncio()
async def test_the_cpu_temperature_is_an_unlabelled_gauge_in_celsius() -> None:
    samples = await _scrape(
        await _fed(HostResourcesReading(cpu_temperature_millicelsius=48_312))
    )

    (temperature,) = samples[TEMPERATURE]
    assert temperature.type == "gauge"
    assert temperature.labels == {}
    assert temperature.value == pytest.approx(48.312)


@pytest.mark.asyncio()
async def test_a_host_with_no_cpu_thermal_zone_has_no_temperature_series() -> None:
    samples = await _scrape(
        await _fed(HostResourcesReading(cpu_temperature_millicelsius=NOT_SUPPORTED))
    )

    assert TEMPERATURE not in samples
    assert samples[TIMESTAMP] == [Sample("gauge", "", {}, WALL_NOW)]


@pytest.mark.parametrize(
    "failure",
    [
        HostResourcesReading(cpu_temperature_millicelsius=None),
        RuntimeError("the whole read raised"),
    ],
)
@pytest.mark.asyncio()
async def test_a_failed_temperature_read_is_nan_with_the_series_kept(
    failure: HostResourcesReading | Exception,
) -> None:
    good = HostResourcesReading(cpu_temperature_millicelsius=50_000)

    samples = await _scrape(await _fed(good, failure))

    (temperature,) = samples[TEMPERATURE]
    assert math.isnan(temperature.value)


@pytest.mark.asyncio()
async def test_a_temperature_over_6_s_old_is_nan() -> None:
    clock = _Clock()
    service = await _fed(
        HostResourcesReading(cpu_temperature_millicelsius=50_000), clock=clock
    )
    clock.now += 6.5

    samples = await _scrape(service)

    (temperature,) = samples[TEMPERATURE]
    assert math.isnan(temperature.value)


@pytest.mark.asyncio()
async def test_the_timestamp_is_the_unix_time_of_the_last_full_sample() -> None:
    clock = _Clock()
    service = await _fed(
        HostResourcesReading(cpu_temperature_millicelsius=50_000),
        HostResourcesReading(cpu_temperature_millicelsius=None),
        clock=clock,
    )
    clock.now += 1.5

    samples = await _scrape(service)

    # The full sample was 2 s (the failed one) + 1.5 s before the scrape.
    assert samples[TIMESTAMP] == [Sample("gauge", "", {}, WALL_NOW - 3.5)]


@pytest.mark.asyncio()
async def test_before_any_sample_there_is_no_temperature_and_no_timestamp() -> None:
    samples = await _scrape(await _fed())

    (timestamp,) = samples[TIMESTAMP]
    assert math.isnan(timestamp.value)
    assert TEMPERATURE not in samples


@pytest.mark.asyncio()
async def test_a_failing_network_link_sampler_leaves_the_host_sample_fresh() -> None:
    samples = await _scrape(
        await _fed(HostResourcesReading(cpu_temperature_millicelsius=50_000))
    )

    (network_timestamp,) = samples[NETWORK_TIMESTAMP]
    assert math.isnan(network_timestamp.value)
    assert samples[TIMESTAMP] == [Sample("gauge", "", {}, WALL_NOW)]
    assert samples[TEMPERATURE][0].value == 50.0


@pytest.mark.asyncio()
async def test_scraping_never_reads_the_host() -> None:
    host = _FakeHost()  # any read would pop from an empty list and raise
    service = HostResourcesService(host, clock=_Clock())

    samples = await _scrape(service)
    samples = await _scrape(service)

    assert math.isnan(samples[TIMESTAMP][0].value)
