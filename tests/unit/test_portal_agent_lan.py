"""The LAN addresses a Check-in reports (ticket #87)."""

from __future__ import annotations

from sp_rtk_portal_protocol import LanAddress

from sp_rtk_base.portal_agent.lan import (
    default_route_interface,
    lan_addresses,
    select,
)

ROUTES = """\
Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0
eth0\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\t0\t0\t0
eth0\t0000000A\t00000000\t0001\t0\t0\t100\t000000FF\t0\t0\t0
"""


def test_the_default_route_with_the_lowest_metric_wins() -> None:
    assert default_route_interface(ROUTES) == "eth0"


def test_no_default_route() -> None:
    assert default_route_interface(ROUTES.splitlines()[0]) is None


def test_private_addresses_on_up_interfaces_with_the_default_route_primary() -> None:
    rows = [
        ("lo", "127.0.0.1", True),
        ("wlan0", "192.168.1.20", True),
        ("eth0", "10.0.0.7", True),
        ("eth0:1", "172.16.5.5", True),
        ("usb0", "192.168.7.2", False),  # down
        ("wwan0", "100.64.3.4", True),  # carrier-grade NAT: not the LAN
        ("ppp0", "81.2.69.160", True),  # public
    ]

    assert select(rows, "eth0") == [
        LanAddress(address="10.0.0.7", interface="eth0", primary=True),
        LanAddress(address="192.168.1.20", interface="wlan0", primary=False),
        LanAddress(address="172.16.5.5", interface="eth0", primary=False),
    ]


def test_without_a_default_route_nothing_is_primary() -> None:
    assert select([("eth0", "10.0.0.7", True)], None) == [
        LanAddress(address="10.0.0.7", interface="eth0", primary=False)
    ]


def test_this_hosts_addresses_are_private_ipv4() -> None:
    import ipaddress

    for a in lan_addresses():
        assert ipaddress.IPv4Address(a.address).is_private
    assert sum(a.primary for a in lan_addresses()) <= 1
