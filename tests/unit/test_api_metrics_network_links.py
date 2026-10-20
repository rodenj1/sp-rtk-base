"""The Network link metrics on /metrics, scraped through the real endpoint.

The real Network link sampler is fed by a fake host reader and a fake
clock, then injected through FastAPI dependency overrides, as the
Bluetooth link tests in ``test_api_metrics.py`` do.
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
from sp_rtk_base.models.network_link_models import (
    AddressReading,
    Counters,
    EgressReading,
    HostReading,
    InterfaceReading,
    StationReading,
    WifiReading,
)
from sp_rtk_base.services import (
    get_config_service,
    get_metrics_service,
    get_network_link_service,
    get_relay_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.network_link.service import NetworkLinkService
from sp_rtk_base.services.relay_service import RelayService

WALL_NOW = 1_800_000_000.0

ETH0 = InterfaceReading(
    name="eth0",
    kind="ethernet",
    operstate="up",
    carrier=True,
    speed_mbps=100,
    full_duplex=True,
    counters=Counters(rx_bytes=5000, tx_bytes=700),
)
ETH0_ADDRESS = AddressReading(interface="eth0", address="192.0.2.10", prefixlen=24)


def _reading(
    *interfaces: InterfaceReading,
    addresses: tuple[AddressReading, ...] = (ETH0_ADDRESS,),
    source: str | None = "192.0.2.10",
    wifi: tuple[WifiReading, ...] | None = (),
) -> HostReading:
    return HostReading(
        interfaces=interfaces,
        addresses=addresses,
        egress=EgressReading(source=source),
        wifi=wifi,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeHost:
    """Serves one reading per sample; an exception is a read that raised."""

    def __init__(self, *readings: HostReading | Exception) -> None:
        self.readings = list(readings)

    def read(self) -> HostReading:
        reading = self.readings.pop(0)
        if isinstance(reading, Exception):
            raise reading
        return reading

    def close(self) -> None:
        pass


async def _fed(
    *readings: HostReading | Exception, clock: _Clock | None = None
) -> NetworkLinkService:
    """The real sampler after one sample per reading, 2 s apart."""
    clock = clock or _Clock()
    service = NetworkLinkService(_FakeHost(*readings), clock=clock)
    for _ in readings:
        clock.now += 2.0
        await service.sample_once()
    return service


@dataclass(frozen=True)
class Sample:
    type: str
    labels: dict[str, str]
    value: float


def _scrape(service: NetworkLinkService) -> dict[str, list[Sample]]:
    """Scrape /metrics: the Network link samples (and Wi-Fi ones), by name."""
    relay = MagicMock(spec=RelayService)
    relay.is_running = False
    config = MagicMock(spec=ConfigService)
    config.get_settings.return_value = AppSettings(metrics_enabled=True)
    app = create_api_app()
    metrics = MetricsService(wall_clock=lambda: WALL_NOW)
    app.dependency_overrides[get_relay_service] = lambda: relay
    app.dependency_overrides[get_metrics_service] = lambda: metrics
    app.dependency_overrides[get_config_service] = lambda: config
    app.dependency_overrides[get_network_link_service] = lambda: service
    text = TestClient(app).get("/metrics").text
    samples: dict[str, list[Sample]] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name.startswith(("sp_rtk_base_network_", "sp_rtk_base_wifi_")):
                samples.setdefault(sample.name, []).append(
                    Sample(family.type, dict(sample.labels), sample.value)
                )
    return samples


def _value(samples: dict[str, list[Sample]], name: str, interface: str) -> float:
    (sample,) = [s for s in samples[name] if s.labels["interface"] == interface]
    return sample.value


@pytest.mark.asyncio()
async def test_a_connected_link_is_up_with_interface_and_kind_labels() -> None:
    samples = _scrape(await _fed(_reading(ETH0)))

    assert samples["sp_rtk_base_network_link_up"] == [
        Sample("gauge", {"interface": "eth0", "kind": "ethernet"}, 1.0)
    ]


@pytest.mark.asyncio()
async def test_a_disconnected_link_is_down_and_no_reading_is_nan() -> None:
    unplugged = ETH0.model_copy(update={"carrier": False})

    disconnected = _scrape(await _fed(_reading(unplugged)))
    no_reading = _scrape(await _fed(_reading(ETH0), HostReading()))

    assert _value(disconnected, "sp_rtk_base_network_link_up", "eth0") == 0.0
    assert math.isnan(_value(no_reading, "sp_rtk_base_network_link_up", "eth0"))


@pytest.mark.asyncio()
async def test_the_uplink_link_is_marked_and_the_others_are_not() -> None:
    wlan0 = InterfaceReading(name="wlan0", kind="wifi", operstate="up")

    samples = _scrape(await _fed(_reading(ETH0, wlan0)))

    assert samples["sp_rtk_base_network_link_uplink"] == [
        Sample("gauge", {"interface": "eth0", "kind": "ethernet"}, 1.0),
        Sample("gauge", {"interface": "wlan0", "kind": "wifi"}, 0.0),
    ]
    assert samples["sp_rtk_base_network_uplink_other"] == [Sample("gauge", {}, 0.0)]


@pytest.mark.asyncio()
async def test_an_uplink_that_is_not_a_network_link_is_other() -> None:
    vpn = AddressReading(interface="wg0", address="10.8.0.2", prefixlen=24)

    samples = _scrape(
        await _fed(_reading(ETH0, addresses=(ETH0_ADDRESS, vpn), source="10.8.0.2"))
    )

    assert _value(samples, "sp_rtk_base_network_link_uplink", "eth0") == 0.0
    assert samples["sp_rtk_base_network_uplink_other"] == [Sample("gauge", {}, 1.0)]


@pytest.mark.asyncio()
async def test_no_uplink_is_zero_everywhere() -> None:
    samples = _scrape(await _fed(_reading(ETH0, source=None)))

    assert _value(samples, "sp_rtk_base_network_link_uplink", "eth0") == 0.0
    assert samples["sp_rtk_base_network_uplink_other"] == [Sample("gauge", {}, 0.0)]


@pytest.mark.asyncio()
async def test_the_uplink_is_nan_while_it_has_no_reading() -> None:
    failed_probe = HostReading(
        interfaces=(ETH0,), addresses=(ETH0_ADDRESS,), egress=None
    )

    samples = _scrape(await _fed(_reading(ETH0), failed_probe))

    assert math.isnan(_value(samples, "sp_rtk_base_network_link_uplink", "eth0"))
    (other,) = samples["sp_rtk_base_network_uplink_other"]
    assert math.isnan(other.value)


COUNTED = ETH0.model_copy(
    update={
        "counters": Counters(
            rx_bytes=5000,
            tx_bytes=700,
            rx_packets=50,
            tx_packets=7,
            rx_errors=3,
            tx_errors=1,
            rx_drops=4,
            tx_drops=2,
        )
    }
)


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("sp_rtk_base_network_receive_bytes_total", 5000.0),
        ("sp_rtk_base_network_transmit_bytes_total", 700.0),
        ("sp_rtk_base_network_receive_packets_total", 50.0),
        ("sp_rtk_base_network_transmit_packets_total", 7.0),
        ("sp_rtk_base_network_receive_errors_total", 3.0),
        ("sp_rtk_base_network_transmit_errors_total", 1.0),
        ("sp_rtk_base_network_receive_drops_total", 4.0),
        ("sp_rtk_base_network_transmit_drops_total", 2.0),
    ],
)
async def test_kernel_counters_are_counters_as_the_kernel_reports_them(
    name: str, expected: float
) -> None:
    samples = _scrape(await _fed(_reading(COUNTED)))

    assert samples[name] == [
        Sample("counter", {"interface": "eth0", "kind": "ethernet"}, expected)
    ]


@pytest.mark.asyncio()
async def test_link_changes_are_a_counter() -> None:
    unplugged = ETH0.model_copy(update={"carrier": False})

    samples = _scrape(await _fed(_reading(ETH0), _reading(unplugged), _reading(ETH0)))

    assert samples["sp_rtk_base_network_link_changes_total"] == [
        Sample("counter", {"interface": "eth0", "kind": "ethernet"}, 2.0)
    ]


@pytest.mark.asyncio()
async def test_counters_hold_their_last_value_through_no_reading() -> None:
    clock = _Clock()
    unplugged = ETH0.model_copy(update={"carrier": False})
    service = await _fed(
        _reading(ETH0), _reading(unplugged), _reading(COUNTED), clock=clock
    )
    clock.now += 30.0  # the sampler stopped: everything is No reading

    samples = _scrape(service)

    assert math.isnan(_value(samples, "sp_rtk_base_network_link_up", "eth0"))
    assert _value(samples, "sp_rtk_base_network_receive_bytes_total", "eth0") == 5000
    assert _value(samples, "sp_rtk_base_network_receive_drops_total", "eth0") == 4
    assert _value(samples, "sp_rtk_base_network_link_changes_total", "eth0") == 2


SPEED = "sp_rtk_base_network_ethernet_speed_bits_per_second"
DUPLEX = "sp_rtk_base_network_ethernet_full_duplex"


@pytest.mark.asyncio()
async def test_ethernet_speed_is_in_bits_per_second_and_duplex_is_a_flag() -> None:
    half = ETH0.model_copy(update={"full_duplex": False})
    wlan0 = InterfaceReading(name="wlan0", kind="wifi", operstate="up")

    samples = _scrape(await _fed(_reading(half, wlan0)))

    assert samples[SPEED] == [Sample("gauge", {"interface": "eth0"}, 100_000_000.0)]
    assert samples[DUPLEX] == [Sample("gauge", {"interface": "eth0"}, 0.0)]


@pytest.mark.asyncio()
async def test_ethernet_speed_and_duplex_are_nan_when_disconnected() -> None:
    unplugged = ETH0.model_copy(
        update={"carrier": False, "speed_mbps": None, "full_duplex": None}
    )

    samples = _scrape(await _fed(_reading(ETH0), _reading(unplugged)))

    assert math.isnan(_value(samples, SPEED, "eth0"))
    assert math.isnan(_value(samples, DUPLEX, "eth0"))


@pytest.mark.asyncio()
async def test_unknown_duplex_on_a_connected_link_is_nan() -> None:
    unknown = ETH0.model_copy(update={"full_duplex": None})

    samples = _scrape(await _fed(_reading(ETH0), _reading(unknown)))

    assert math.isnan(_value(samples, DUPLEX, "eth0"))
    assert _value(samples, SPEED, "eth0") == 100_000_000.0


@pytest.mark.asyncio()
async def test_a_not_supported_reading_has_no_series() -> None:
    no_speed = ETH0.model_copy(update={"speed_mbps": None, "full_duplex": None})

    samples = _scrape(await _fed(_reading(no_speed)))

    assert SPEED not in samples
    assert DUPLEX not in samples
    assert _value(samples, "sp_rtk_base_network_link_up", "eth0") == 1.0


TIMESTAMP = "sp_rtk_base_network_last_sample_timestamp_seconds"


@pytest.mark.asyncio()
async def test_the_timestamp_is_the_unix_time_of_the_last_full_sample() -> None:
    clock = _Clock()
    failed_probe = HostReading(
        interfaces=(ETH0,), addresses=(ETH0_ADDRESS,), egress=None, wifi=()
    )
    service = await _fed(_reading(ETH0), failed_probe, clock=clock)
    clock.now += 1.5

    samples = _scrape(service)

    # The full sample was 2 s (the failed one) + 1.5 s before the scrape.
    assert samples[TIMESTAMP] == [Sample("gauge", {}, WALL_NOW - 3.5)]


@pytest.mark.asyncio()
async def test_before_any_sample_there_are_no_links_and_no_timestamp() -> None:
    samples = _scrape(await _fed())

    (timestamp,) = samples[TIMESTAMP]
    assert math.isnan(timestamp.value)
    assert "sp_rtk_base_network_link_up" not in samples


@pytest.mark.asyncio()
async def test_a_vanished_interface_loses_its_series() -> None:
    wlan0 = InterfaceReading(name="wlan0", kind="wifi", operstate="up")

    samples = _scrape(await _fed(_reading(ETH0, wlan0), _reading(wlan0)))

    interfaces = {
        sample.labels["interface"]
        for series in samples.values()
        for sample in series
        if sample.labels
    }
    assert interfaces == {"wlan0"}


@pytest.mark.asyncio()
async def test_labels_are_only_interface_and_kind() -> None:
    wlan0 = InterfaceReading(name="wlan0", kind="wifi", operstate="up")

    samples = _scrape(await _fed(_reading(ETH0, wlan0)))

    label_names = {
        name: {label for sample in series for label in sample.labels}
        for name, series in samples.items()
    }
    assert all(names <= {"interface", "kind"} for names in label_names.values())
    label_values = {
        value
        for series in samples.values()
        for sample in series
        for value in sample.labels.values()
    }
    assert label_values == {"eth0", "wlan0", "ethernet", "wifi"}


@pytest.mark.asyncio()
async def test_an_interface_reappearing_at_zero_is_a_plain_counter_reset() -> None:
    wlan0 = InterfaceReading(name="wlan0", kind="wifi", operstate="up")
    fresh = ETH0.model_copy(update={"counters": Counters()})

    samples = _scrape(
        await _fed(_reading(COUNTED, wlan0), _reading(wlan0), _reading(fresh, wlan0))
    )

    assert _value(samples, "sp_rtk_base_network_receive_bytes_total", "eth0") == 0
    assert _value(samples, "sp_rtk_base_network_link_changes_total", "eth0") == 0
    assert not any(name.endswith("_created") for name in samples)


# ---------------------------------------------------------------------------
# Wi-Fi details
# ---------------------------------------------------------------------------

WLAN0 = InterfaceReading(name="wlan0", kind="wifi", operstate="up")
WLAN0_DORMANT = WLAN0.model_copy(update={"operstate": "dormant"})
ASSOCIATED = WifiReading(
    interface="wlan0",
    mode="station",
    wiphy=0,
    frequency_mhz=5745,
    ssid="Test",
    station=StationReading(
        bssid="02:00:00:00:00:01",
        signal_dbm=-55,
        tx_bitrate_bps=325_000_000,
        rx_bitrate_bps=433_300_000,
        tx_failed=11,
    ),
)
UNASSOCIATED = ASSOCIATED.model_copy(update={"station": None})
FREQUENCY = "sp_rtk_base_wifi_frequency_hertz"
TX_BITRATE = "sp_rtk_base_wifi_tx_bitrate_bits_per_second"
RX_BITRATE = "sp_rtk_base_wifi_rx_bitrate_bits_per_second"
TX_FAILED = "sp_rtk_base_wifi_tx_failed_total"
STRENGTH = "sp_rtk_base_wifi_link_strength"
STRENGTH_DBM = "sp_rtk_base_wifi_link_strength_dbm"
WIFI_GAUGES = (FREQUENCY, TX_BITRATE, RX_BITRATE, STRENGTH, STRENGTH_DBM)


@pytest.mark.asyncio()
async def test_wifi_frequency_and_bitrates_are_gauges_in_si_units() -> None:
    samples = _scrape(await _fed(_reading(WLAN0, wifi=(ASSOCIATED,))))

    assert samples[FREQUENCY] == [Sample("gauge", {"interface": "wlan0"}, 5.745e9)]
    assert samples[TX_BITRATE] == [
        Sample("gauge", {"interface": "wlan0"}, 325_000_000.0)
    ]
    assert samples[RX_BITRATE] == [
        Sample("gauge", {"interface": "wlan0"}, 433_300_000.0)
    ]


def _station_at(dbm: int) -> WifiReading:
    station = ASSOCIATED.station
    assert station is not None
    return ASSOCIATED.model_copy(
        update={"station": station.model_copy(update={"signal_dbm": dbm})}
    )


@pytest.mark.asyncio()
@pytest.mark.parametrize(("dbm", "verdict"), [(-55, 0.0), (-70, 1.0), (-85, 2.0)])
async def test_wifi_link_strength_is_the_verdict_and_the_latest_dbm(
    dbm: int, verdict: float
) -> None:
    samples = _scrape(await _fed(_reading(WLAN0, wifi=(_station_at(dbm),))))

    assert samples[STRENGTH] == [Sample("gauge", {"interface": "wlan0"}, verdict)]
    assert samples[STRENGTH_DBM] == [
        Sample("gauge", {"interface": "wlan0"}, float(dbm))
    ]


@pytest.mark.asyncio()
async def test_wifi_link_strength_dbm_is_the_latest_reading_not_the_median() -> None:
    samples = _scrape(
        await _fed(
            _reading(WLAN0, wifi=(_station_at(-55),)),
            _reading(WLAN0, wifi=(_station_at(-55),)),
            _reading(WLAN0, wifi=(_station_at(-85),)),
        )
    )

    assert _value(samples, STRENGTH, "wlan0") == 0.0
    assert _value(samples, STRENGTH_DBM, "wlan0") == -85.0


@pytest.mark.asyncio()
async def test_wifi_failed_sends_are_a_counter() -> None:
    samples = _scrape(await _fed(_reading(WLAN0, wifi=(ASSOCIATED,))))

    assert samples[TX_FAILED] == [Sample("counter", {"interface": "wlan0"}, 11.0)]


@pytest.mark.asyncio()
async def test_wifi_gauges_are_nan_when_disconnected_and_failed_sends_hold() -> None:
    samples = _scrape(
        await _fed(
            _reading(WLAN0, wifi=(ASSOCIATED,)),
            _reading(WLAN0_DORMANT, wifi=(UNASSOCIATED,)),
        )
    )

    assert all(math.isnan(_value(samples, name, "wlan0")) for name in WIFI_GAUGES)
    assert _value(samples, TX_FAILED, "wlan0") == 11.0


@pytest.mark.asyncio()
async def test_wifi_gauges_are_nan_while_nl80211_gives_no_reading() -> None:
    samples = _scrape(
        await _fed(_reading(WLAN0, wifi=(ASSOCIATED,)), _reading(WLAN0, wifi=None))
    )

    assert all(math.isnan(_value(samples, name, "wlan0")) for name in WIFI_GAUGES)
    assert _value(samples, TX_FAILED, "wlan0") == 11.0


@pytest.mark.asyncio()
async def test_wifi_readings_never_given_have_no_series() -> None:
    samples = _scrape(await _fed(_reading(ETH0, WLAN0_DORMANT, wifi=(UNASSOCIATED,))))

    for name in (*WIFI_GAUGES, TX_FAILED):
        assert name not in samples
