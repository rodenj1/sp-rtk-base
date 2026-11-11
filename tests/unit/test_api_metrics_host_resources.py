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
CPU_SECONDS = "sp_rtk_base_host_cpu_seconds_total"
BYTES_GAUGES = (
    "sp_rtk_base_host_memory_total_bytes",
    "sp_rtk_base_host_memory_available_bytes",
    "sp_rtk_base_host_root_filesystem_size_bytes",
    "sp_rtk_base_host_root_filesystem_available_bytes",
)
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


def _reading(**fields: object) -> HostResourcesReading:
    """A sample in which every reading read, but for *fields*."""
    good: dict[str, object] = {
        "cpu_seconds": {"user": 100.0, "idle": 900.0},
        "memory_total_bytes": 4_000_000_000,
        "memory_available_bytes": 3_000_000_000,
        "cpu_temperature_millicelsius": 50_000,
        "root_filesystem_size_bytes": 32_000_000_000,
        "root_filesystem_available_bytes": 20_000_000_000,
    }
    return HostResourcesReading.model_validate(good | fields)


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
        await _fed(_reading(cpu_temperature_millicelsius=NOT_SUPPORTED))
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
        _reading(), _reading(cpu_temperature_millicelsius=None), clock=clock
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
    samples = await _scrape(await _fed(_reading()))

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


@pytest.mark.asyncio()
async def test_cpu_seconds_are_a_counter_by_mode_with_no_per_core_label() -> None:
    samples = await _scrape(
        await _fed(_reading(cpu_seconds={"user": 101.5, "idle": 902.0, "iowait": 3.0}))
    )

    assert sorted(samples[CPU_SECONDS], key=lambda s: s.labels["mode"]) == [
        Sample("counter", "", {"mode": "idle"}, 902.0),
        Sample("counter", "", {"mode": "iowait"}, 3.0),
        Sample("counter", "", {"mode": "user"}, 101.5),
    ]


@pytest.mark.asyncio()
async def test_cpu_seconds_never_go_backwards_when_iowait_falls() -> None:
    samples = await _scrape(
        await _fed(
            _reading(cpu_seconds={"iowait": 50.0}),
            _reading(cpu_seconds={"iowait": 48.0}),
        )
    )

    assert samples[CPU_SECONDS] == [Sample("counter", "", {"mode": "iowait"}, 50.0)]


@pytest.mark.parametrize(
    "failure", [_reading(cpu_seconds=None), RuntimeError("the whole read raised")]
)
@pytest.mark.asyncio()
async def test_cpu_seconds_hold_their_last_value_through_no_reading(
    failure: HostResourcesReading | Exception,
) -> None:
    clock = _Clock()
    service = await _fed(_reading(cpu_seconds={"user": 100.0}), failure, clock=clock)
    clock.now += 60.0

    samples = await _scrape(service)

    assert samples[CPU_SECONDS] == [Sample("counter", "", {"mode": "user"}, 100.0)]


@pytest.mark.asyncio()
async def test_before_any_sample_there_are_no_cpu_seconds() -> None:
    samples = await _scrape(await _fed())

    assert CPU_SECONDS not in samples


@pytest.mark.asyncio()
async def test_memory_and_root_filesystem_are_unlabelled_gauges_in_bytes() -> None:
    samples = await _scrape(
        await _fed(
            _reading(
                memory_total_bytes=3_980_000_000,
                memory_available_bytes=2_100_000_000,
                root_filesystem_size_bytes=31_000_000_000,
                root_filesystem_available_bytes=24_500_000_000,
            )
        )
    )

    assert {name: samples[name] for name in BYTES_GAUGES} == {
        "sp_rtk_base_host_memory_total_bytes": [Sample("gauge", "", {}, 3_980_000_000)],
        "sp_rtk_base_host_memory_available_bytes": [
            Sample("gauge", "", {}, 2_100_000_000)
        ],
        "sp_rtk_base_host_root_filesystem_size_bytes": [
            Sample("gauge", "", {}, 31_000_000_000)
        ],
        "sp_rtk_base_host_root_filesystem_available_bytes": [
            Sample("gauge", "", {}, 24_500_000_000)
        ],
    }


@pytest.mark.parametrize("name", BYTES_GAUGES)
@pytest.mark.asyncio()
async def test_a_failed_memory_or_filesystem_read_is_nan_with_the_series_kept(
    name: str,
) -> None:
    field = name.removeprefix("sp_rtk_base_host_")
    samples = await _scrape(await _fed(_reading(), _reading(**{field: None})))

    (gauge,) = samples[name]
    assert math.isnan(gauge.value)


@pytest.mark.asyncio()
async def test_before_any_sample_memory_and_filesystem_are_nan() -> None:
    samples = await _scrape(await _fed())

    for name in BYTES_GAUGES:
        (gauge,) = samples[name]
        assert math.isnan(gauge.value)
