"""Network link health, tested through ``NetworkLinkMonitor``.

The seam is the monitor: tests feed it raw host readings (sysfs
interfaces, addresses, the Uplink probe) at times from a fake clock, 2 s
apart like the sampler, and assert on the snapshot.
"""

from __future__ import annotations

import pytest

from sp_rtk_base.models.network_link_models import (
    AddressReading,
    Counters,
    EgressReading,
    HostReading,
    InterfaceReading,
    NetworkLink,
    NetworkLinkSnapshot,
)
from sp_rtk_base.services.network_link.monitor import NetworkLinkMonitor
from sp_rtk_base.uplink import Uplink


class Clock:
    """Fake monotonic clock: each ``tick()`` is one 2 s sample later."""

    def __init__(self) -> None:
        self.now = 1000.0

    def tick(self, seconds: float = 2.0) -> float:
        self.now += seconds
        return self.now


def _eth(
    name: str = "eth0",
    *,
    carrier: bool | None = True,
    speed: int | None = 100,
    full_duplex: bool | None = True,
    rx_bytes: int = 0,
    tx_bytes: int = 0,
    rx_errors: int = 0,
) -> InterfaceReading:
    return InterfaceReading(
        name=name,
        kind="ethernet",
        operstate="up" if carrier else "down",
        carrier=carrier,
        speed_mbps=speed if carrier else None,
        full_duplex=full_duplex if carrier else None,
        counters=Counters(rx_bytes=rx_bytes, tx_bytes=tx_bytes, rx_errors=rx_errors),
    )


def _wifi(
    name: str = "wlan0", *, operstate: str = "up", rx_bytes: int = 0
) -> InterfaceReading:
    return InterfaceReading(
        name=name,
        kind="wifi",
        operstate=operstate,
        # brcmfmac leaves carrier up across a disassociation.
        carrier=True,
        counters=Counters(rx_bytes=rx_bytes),
    )


ETH_ADDR = AddressReading(interface="eth0", address="192.0.2.10", prefixlen=24)
WIFI_ADDR = AddressReading(interface="wlan0", address="192.0.2.20", prefixlen=24)


def _reading(
    *interfaces: InterfaceReading,
    addresses: tuple[AddressReading, ...] | None = None,
    source: str | None = None,
) -> HostReading:
    """Every source read; addresses default to one per interface given."""
    if addresses is None:
        known = {a.interface: a for a in (ETH_ADDR, WIFI_ADDR)}
        addresses = tuple(known[i.name] for i in interfaces if i.name in known)
    return HostReading(
        interfaces=interfaces, addresses=addresses, egress=EgressReading(source=source)
    )


def _fed(*readings: HostReading) -> tuple[NetworkLinkMonitor, Clock]:
    """A monitor fed one reading per sample, 2 s apart."""
    monitor, clock = NetworkLinkMonitor(), Clock()
    for reading in readings:
        monitor.observe(reading, clock.tick())
    return monitor, clock


def _snapshot(*readings: HostReading) -> NetworkLinkSnapshot:
    monitor, clock = _fed(*readings)
    return monitor.snapshot(clock.now)


def _link(snapshot: NetworkLinkSnapshot, name: str) -> NetworkLink:
    (link,) = (link for link in snapshot.links if link.name == name)
    return link


def test_a_cabled_ethernet_link_is_connected_with_its_speed_and_duplex() -> None:
    snapshot = _snapshot(_reading(_eth(), source="192.0.2.10"))

    (link,) = snapshot.links
    assert (link.name, link.kind, link.state, link.reason) == (
        "eth0",
        "ethernet",
        "Connected",
        None,
    )
    assert link.ethernet is not None
    assert link.ethernet.speed_mbps is not None
    assert link.ethernet.speed_mbps.value == 100
    assert link.ethernet.full_duplex is not None
    assert link.ethernet.full_duplex.value is True
    assert link.ipv4 == "192.0.2.10/24"
    assert link.uplink is True
    assert snapshot.uplink == Uplink(interface="eth0", network_link=True)


def test_an_associated_wifi_link_is_connected_with_no_ethernet_details() -> None:
    snapshot = _snapshot(_reading(_wifi(), source="192.0.2.20"))

    (link,) = snapshot.links
    assert (link.name, link.kind, link.state) == ("wlan0", "wifi", "Connected")
    assert link.ethernet is None
    assert link.ipv4 == "192.0.2.20/24"
    assert snapshot.uplink == Uplink(interface="wlan0", network_link=True)


@pytest.mark.parametrize(
    ("source", "uplink_name", "order"),
    [
        ("192.0.2.10", "eth0", ["eth0", "wlan0"]),
        ("192.0.2.20", "wlan0", ["wlan0", "eth0"]),
    ],
)
def test_with_both_links_connected_the_uplink_is_where_traffic_leaves_and_listed_first(
    source: str, uplink_name: str, order: list[str]
) -> None:
    snapshot = _snapshot(_reading(_wifi(), _eth(), source=source))

    assert snapshot.uplink == Uplink(interface=uplink_name, network_link=True)
    assert [link.name for link in snapshot.links] == order
    assert [link.uplink for link in snapshot.links] == [True, False]
    assert {link.state for link in snapshot.links} == {"Connected"}


def test_without_an_uplink_links_list_ethernet_then_wifi_each_by_name() -> None:
    snapshot = _snapshot(
        _reading(_wifi("wlan1"), _eth("eth1"), _wifi("wlan0"), _eth("eth0"))
    )

    assert [link.name for link in snapshot.links] == ["eth0", "eth1", "wlan0", "wlan1"]


def test_no_uplink_when_links_are_up_but_the_network_is_unreachable() -> None:
    snapshot = _snapshot(_reading(_wifi(), _eth(), source=None))

    assert snapshot.uplink is None
    assert snapshot.uplink_no_reading is False
    assert not any(link.uplink for link in snapshot.links)
    assert {link.state for link in snapshot.links} == {"Connected"}


def test_a_vpn_uplink_is_not_a_network_link() -> None:
    vpn = AddressReading(interface="wg0", address="10.8.0.2", prefixlen=32)
    snapshot = _snapshot(_reading(_eth(), addresses=(ETH_ADDR, vpn), source="10.8.0.2"))

    assert snapshot.uplink == Uplink(interface="wg0", network_link=False)
    assert [link.name for link in snapshot.links] == ["eth0"]
    assert snapshot.links[0].uplink is False


def test_a_pulled_cable_is_disconnected_with_no_speed_and_counts_a_link_change() -> (
    None
):
    snapshot = _snapshot(_reading(_eth()), _reading(_eth(carrier=False)))

    (link,) = snapshot.links
    assert (link.state, link.reason) == ("Disconnected", "No cable.")
    assert link.link_changes == 1
    # Speed and duplex are supported, just not current.
    assert link.ethernet is not None
    assert link.ethernet.speed_mbps is not None
    assert link.ethernet.speed_mbps.value is None
    assert link.ethernet.full_duplex is not None
    assert link.ethernet.full_duplex.value is None


def test_a_reinserted_cable_reconnects_and_counts_both_link_changes() -> None:
    snapshot = _snapshot(
        _reading(_eth()), _reading(_eth(carrier=False)), _reading(_eth())
    )

    (link,) = snapshot.links
    assert (link.state, link.reason, link.link_changes) == ("Connected", None, 2)
    assert link.ethernet is not None
    assert link.ethernet.speed_mbps is not None
    assert link.ethernet.speed_mbps.value == 100


def test_link_changes_start_at_zero_even_for_a_link_first_seen_down() -> None:
    snapshot = _snapshot(_reading(_eth(carrier=False)), _reading(_eth(carrier=False)))

    assert snapshot.links[0].link_changes == 0


def test_dormant_wifi_is_disconnected_even_though_its_carrier_is_up() -> None:
    snapshot = _snapshot(_reading(_wifi()), _reading(_wifi(operstate="dormant")))

    (link,) = snapshot.links
    assert (link.state, link.reason) == ("Disconnected", "Wi-Fi is disconnected.")
    assert link.link_changes == 1


def test_a_reading_missing_while_up_is_not_supported_until_it_appears() -> None:
    no_speed = _eth(speed=None, full_duplex=None)
    before = _snapshot(_reading(no_speed))
    after = _snapshot(_reading(no_speed), _reading(_eth(speed=1000)))

    assert before.links[0].ethernet is not None
    assert before.links[0].ethernet.speed_mbps is None
    assert before.links[0].ethernet.full_duplex is None
    assert after.links[0].ethernet is not None
    assert after.links[0].ethernet.speed_mbps is not None
    assert after.links[0].ethernet.speed_mbps.value == 1000


def test_a_reading_missing_while_disconnected_is_not_judged_unsupported() -> None:
    # Speed and duplex are never given without a cable; that isn't Not
    # supported, but nothing says they are supported either.
    snapshot = _snapshot(_reading(_eth(carrier=False)))

    assert snapshot.links[0].ethernet is not None
    assert snapshot.links[0].ethernet.speed_mbps is None


def test_throughput_is_empty_for_the_first_sample() -> None:
    snapshot = _snapshot(_reading(_eth(rx_bytes=5000)))

    assert snapshot.links[0].throughput is None


def test_throughput_is_the_byte_rate_between_the_last_two_samples() -> None:
    snapshot = _snapshot(
        _reading(_eth(rx_bytes=1000, tx_bytes=0)),
        _reading(_eth(rx_bytes=1000 + 8000, tx_bytes=400)),
    )

    throughput = snapshot.links[0].throughput
    assert throughput is not None
    assert (throughput.rx_bytes_per_second, throughput.tx_bytes_per_second) == (
        4000.0,
        200.0,
    )


def _blank_but_counters(link: NetworkLink) -> None:
    """Every value of a No reading link is empty, except its counter totals."""
    assert link.state == "No reading"
    assert (link.ipv4, link.ipv6, link.throughput) == (None, None, None)
    if link.ethernet is not None:
        for supported in (link.ethernet.speed_mbps, link.ethernet.full_duplex):
            assert supported is None or supported.value is None


def test_before_the_first_reading_there_are_no_links_and_no_reading() -> None:
    snapshot = NetworkLinkMonitor().snapshot(1000.0)

    assert snapshot.links == ()
    assert snapshot.uplink_no_reading is True
    assert snapshot.last_sample_age_seconds is None
    assert snapshot.stale is True


def test_a_fresh_sample_is_not_stale_and_says_how_old_it_is() -> None:
    monitor, clock = _fed(_reading(_eth(), source="192.0.2.10"))

    snapshot = monitor.snapshot(clock.tick(1.5))

    assert snapshot.last_sample_age_seconds == 1.5
    assert snapshot.stale is False


def test_a_stopped_sampler_goes_stale_after_6_s_keeping_only_the_counters() -> None:
    monitor, clock = _fed(
        _reading(_eth(rx_bytes=100, rx_errors=3), source="192.0.2.10"),
        _reading(_eth(rx_bytes=900, rx_errors=4), source="192.0.2.10"),
    )
    still_fresh = monitor.snapshot(clock.tick(6.0))
    stale = monitor.snapshot(clock.tick(1.0))

    assert still_fresh.links[0].state == "Connected"
    assert still_fresh.stale is False
    assert stale.stale is True
    assert stale.last_sample_age_seconds == 7.0
    assert stale.uplink is None
    assert stale.uplink_no_reading is True
    (link,) = stale.links
    _blank_but_counters(link)
    assert link.reason == "No reading for 7 s."
    assert (link.counters.rx_bytes, link.counters.rx_errors) == (900, 4)


def test_a_failed_sysfs_read_is_no_reading_at_once() -> None:
    snapshot = _snapshot(
        _reading(_eth(rx_bytes=100), source="192.0.2.10"),
        HostReading(
            interfaces=None,
            addresses=(ETH_ADDR,),
            egress=EgressReading(source="192.0.2.10"),
        ),
    )

    (link,) = snapshot.links
    _blank_but_counters(link)
    assert link.reason == "No reading for 2 s."
    assert link.counters.rx_bytes == 100
    assert link.link_changes == 0


def test_throughput_restarts_after_a_gap_in_the_readings() -> None:
    gap = HostReading(addresses=(ETH_ADDR,), egress=EgressReading(source=None))
    snapshot = _snapshot(
        _reading(_eth(rx_bytes=100)), gap, _reading(_eth(rx_bytes=900))
    )

    assert snapshot.links[0].state == "Connected"
    assert snapshot.links[0].throughput is None


def test_a_failed_address_read_blanks_the_addresses_and_uplink_only() -> None:
    snapshot = _snapshot(
        _reading(_eth(rx_bytes=0), source="192.0.2.10"),
        HostReading(
            interfaces=(_eth(rx_bytes=2000),),
            addresses=None,
            egress=EgressReading(source="192.0.2.10"),
        ),
    )

    (link,) = snapshot.links
    assert link.state == "Connected"
    assert (link.ipv4, link.ipv6) == (None, None)
    assert link.throughput is not None
    assert snapshot.uplink_no_reading is True
    # Not every source read, so the last full sample is the first one.
    assert snapshot.last_sample_age_seconds == 2.0


def test_a_vanished_interface_disappears_and_starts_over_when_it_returns() -> None:
    snapshot_gone = _snapshot(
        _reading(_eth(), _eth("eth1", carrier=True, rx_bytes=500)),
        _reading(_eth(), _eth("eth1", carrier=False, rx_bytes=600)),
        _reading(_eth()),
    )
    snapshot_back = _snapshot(
        _reading(_eth(), _eth("eth1", carrier=True, rx_bytes=500)),
        _reading(_eth(), _eth("eth1", carrier=False, rx_bytes=600)),
        _reading(_eth()),
        _reading(_eth(), _eth("eth1", carrier=True, rx_bytes=10)),
    )

    assert [link.name for link in snapshot_gone.links] == ["eth0"]
    back = _link(snapshot_back, "eth1")
    assert back.state == "Connected"
    assert back.link_changes == 0
    assert back.counters.rx_bytes == 10
    assert back.throughput is None


def test_a_link_prefers_a_global_ipv6_address_over_a_link_local_one() -> None:
    addresses = (
        AddressReading(interface="eth0", address="fe80::1", prefixlen=64),
        ETH_ADDR,
        AddressReading(interface="eth0", address="2001:db8::10", prefixlen=64),
        AddressReading(interface="wlan0", address="2001:db8::20", prefixlen=64),
    )
    snapshot = _snapshot(_reading(_eth(), addresses=addresses))

    assert (snapshot.links[0].ipv4, snapshot.links[0].ipv6) == (
        "192.0.2.10/24",
        "2001:db8::10/64",
    )


def test_an_address_on_no_known_interface_gives_no_uplink() -> None:
    snapshot = _snapshot(_reading(_eth(), source="198.51.100.7"))

    assert snapshot.uplink is None
    assert snapshot.uplink_no_reading is False
