"""The LAN addresses a Check-in reports (ticket #87); the primary one is on
the Uplink (ticket #133)."""

from __future__ import annotations

from sp_rtk_portal_protocol import LanAddress

from sp_rtk_base.portal_agent.lan import lan_addresses, select

ROWS = [
    ("lo", "127.0.0.1", True),
    ("wlan0", "192.168.1.20", True),
    ("eth0", "10.0.0.7", True),
    ("eth0:1", "172.16.5.5", True),
    ("usb0", "192.168.7.2", False),  # down
    ("wwan0", "100.64.3.4", True),  # carrier-grade NAT: not the LAN
    ("ppp0", "81.2.69.160", True),  # public
]


def test_private_addresses_on_up_interfaces_with_the_uplinks_primary() -> None:
    assert select(ROWS, "10.0.0.7") == [
        LanAddress(address="10.0.0.7", interface="eth0", primary=True),
        LanAddress(address="192.168.1.20", interface="wlan0", primary=False),
        LanAddress(address="172.16.5.5", interface="eth0", primary=False),
    ]


def test_the_uplink_wins_even_where_a_cable_is_plugged_in() -> None:
    # Traffic leaves by Wi-Fi, whatever the default routes' metrics say.
    assert select(ROWS, "192.168.1.20")[0] == LanAddress(
        address="192.168.1.20", interface="wlan0", primary=True
    )


def test_a_vpn_uplinks_private_address_is_primary() -> None:
    rows = [*ROWS, ("wg0", "10.200.0.2", True)]

    assert select(rows, "10.200.0.2")[0] == LanAddress(
        address="10.200.0.2", interface="wg0", primary=True
    )


def test_with_no_uplink_nothing_is_primary() -> None:
    assert select([("eth0", "10.0.0.7", True)], None) == [
        LanAddress(address="10.0.0.7", interface="eth0", primary=False)
    ]


def test_a_public_uplink_leaves_no_private_address_primary() -> None:
    assert not any(a.primary for a in select(ROWS, "81.2.69.160"))


def test_this_hosts_addresses_are_private_ipv4() -> None:
    import ipaddress

    for a in lan_addresses():
        assert ipaddress.IPv4Address(a.address).is_private
    assert sum(a.primary for a in lan_addresses()) <= 1
