"""The Uplink rule: where IPv4 traffic actually leaves (spec #132, ticket #133)."""

from __future__ import annotations

from sp_rtk_base.uplink import Uplink, picked_source, uplink

ADDRESSES = [
    ("lo", "127.0.0.1"),
    ("eth0", "10.0.0.7"),
    ("wlan0", "192.168.1.20"),
    ("wg0", "10.200.0.2"),
]
NETWORK_LINKS = {"eth0", "wlan0"}


def test_traffic_leaving_by_a_network_link_makes_it_the_uplink() -> None:
    assert uplink("192.168.1.20", ADDRESSES, NETWORK_LINKS) == Uplink(
        interface="wlan0", network_link=True
    )


def test_traffic_leaving_by_a_vpn_makes_an_uplink_that_is_not_a_network_link() -> None:
    assert uplink("10.200.0.2", ADDRESSES, NETWORK_LINKS) == Uplink(
        interface="wg0", network_link=False
    )


def test_an_unreachable_network_means_no_uplink() -> None:
    assert uplink(None, ADDRESSES, NETWORK_LINKS) is None


def test_a_source_address_on_no_known_interface_means_no_uplink() -> None:
    assert uplink("172.16.9.9", ADDRESSES, NETWORK_LINKS) is None


def test_an_address_label_counts_as_its_interface() -> None:
    addresses = [("eth0", "10.0.0.7"), ("eth0:1", "172.16.5.5")]

    assert uplink("172.16.5.5", addresses, NETWORK_LINKS) == Uplink(
        interface="eth0", network_link=True
    )


def test_the_kernel_picks_a_source_address_without_a_packet_sent() -> None:
    # Loopback: answers the same on any host, with or without a network.
    assert picked_source(("127.0.0.1", 9)) == "127.0.0.1"
