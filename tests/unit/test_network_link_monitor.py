"""Network link health, tested through ``NetworkLinkMonitor``.

The seam is the monitor: tests feed it raw host readings (sysfs
interfaces, addresses, the Uplink probe, nl80211) at times from a fake clock, 2 s
apart like the sampler, and assert on the snapshot.
"""

from __future__ import annotations

from typing import Literal, cast

import pytest

from sp_rtk_base.models.level_models import Level
from sp_rtk_base.models.network_link_models import (
    AddressReading,
    Counters,
    EgressReading,
    HostReading,
    InterfaceReading,
    NetworkLink,
    NetworkLinkSnapshot,
    StationReading,
    Supported,
    WifiDetails,
    WifiMode,
    WifiReading,
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


BSSID = "02:00:00:00:00:01"


def _station(
    bssid: str = BSSID,
    *,
    signal_dbm: int | None = -55,
    tx_failed: int | None = 11,
) -> StationReading:
    return StationReading(
        bssid=bssid,
        signal_dbm=signal_dbm,
        tx_bitrate_bps=325_000_000,
        rx_bitrate_bps=433_300_000,
        tx_failed=tx_failed,
    )


def _nl80211(
    name: str = "wlan0",
    *,
    station: StationReading | None = None,
    mode: WifiMode = "station",
    wiphy: int = 0,
    frequency_mhz: int = 5745,
) -> WifiReading:
    return WifiReading(
        interface=name,
        mode=mode,
        wiphy=wiphy,
        frequency_mhz=frequency_mhz,
        ssid="Test",
        station=station,
    )


def _reading(
    *interfaces: InterfaceReading,
    addresses: tuple[AddressReading, ...] | None = None,
    source: str | None = None,
    wifi: tuple[WifiReading, ...] | None | Literal["associated if up"] = (
        "associated if up"
    ),
) -> HostReading:
    """Every source read; addresses default to one per interface given, and
    nl80211 to an association for each Wi-Fi interface whose operstate is
    up."""
    if addresses is None:
        known = {a.interface: a for a in (ETH_ADDR, WIFI_ADDR)}
        addresses = tuple(known[i.name] for i in interfaces if i.name in known)
    if wifi == "associated if up":
        wifi = tuple(
            _nl80211(i.name, station=_station() if i.operstate == "up" else None)
            for i in interfaces
            if i.kind == "wifi"
        )
    return HostReading(
        interfaces=interfaces,
        addresses=addresses,
        egress=EgressReading(source=source),
        wifi=wifi,
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
    if link.wifi is not None:
        for name, supported in link.wifi:
            if name != "tx_failed" and isinstance(supported, Supported):
                assert cast("Supported[object]", supported).value is None


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


# ---------------------------------------------------------------------------
# Wi-Fi details from nl80211
# ---------------------------------------------------------------------------


def _details(link: NetworkLink) -> WifiDetails:
    assert link.wifi is not None
    return link.wifi


def _values(details: WifiDetails) -> dict[str, object]:
    """Each Wi-Fi reading's value; absent for a Not supported one."""
    return {
        name: cast("Supported[object]", field).value
        for name, field in details
        if isinstance(field, Supported)
    }


def test_an_associated_wifi_link_reads_its_network_frequency_and_bitrates() -> None:
    snapshot = _snapshot(_reading(_wifi()))

    (link,) = snapshot.links
    details = _details(link)
    assert details.no_reading_reason is None
    assert _values(details) == {
        "ssid": "Test",
        "bssid": BSSID,
        "frequency_mhz": 5745,
        "tx_bitrate_bps": 325_000_000,
        "rx_bitrate_bps": 433_300_000,
        "signal_dbm": -55,
        "link_strength": "Good",
        "tx_failed": 11,
    }


def test_an_ethernet_link_has_no_wifi_details() -> None:
    assert _snapshot(_reading(_eth())).links[0].wifi is None


def test_wifi_with_operstate_up_but_no_station_is_disconnected() -> None:
    snapshot = _snapshot(_reading(_wifi(), wifi=(_nl80211(station=None),)))

    (link,) = snapshot.links
    assert (link.state, link.reason) == ("Disconnected", "Wi-Fi is disconnected.")


def test_the_frequency_is_ignored_while_unassociated() -> None:
    # The interface's frequency hops while it scans.
    snapshot = _snapshot(
        _reading(_wifi(operstate="dormant"), wifi=(_nl80211(frequency_mhz=2412),))
    )

    details = _details(snapshot.links[0])
    assert details.frequency_mhz is None
    assert details.ssid is None


def test_a_disconnect_leaves_no_wifi_values_but_the_failed_sends_total() -> None:
    snapshot = _snapshot(
        _reading(_wifi()),
        _reading(_wifi(operstate="dormant"), wifi=(_nl80211(frequency_mhz=2412),)),
    )

    (link,) = snapshot.links
    assert link.state == "Disconnected"
    assert _values(_details(link)) == {
        "ssid": None,
        "bssid": None,
        "frequency_mhz": None,
        "tx_bitrate_bps": None,
        "rx_bitrate_bps": None,
        "signal_dbm": None,
        "link_strength": None,
        "tx_failed": 11,
    }


def test_the_setup_access_point_is_disconnected_with_its_reason() -> None:
    snapshot = _snapshot(_reading(_wifi(), wifi=(_nl80211(mode="ap"),)))

    (link,) = snapshot.links
    assert (link.state, link.reason) == (
        "Disconnected",
        "The setup access point is on.",
    )
    assert set(_values(_details(link)).values()) <= {None}


def test_starting_the_setup_access_point_counts_a_link_change() -> None:
    snapshot = _snapshot(
        _reading(_wifi()), _reading(_wifi(), wifi=(_nl80211(mode="ap"),))
    )

    assert snapshot.links[0].link_changes == 1


def test_roaming_to_another_bssid_counts_a_link_change() -> None:
    snapshot = _snapshot(
        _reading(_wifi()),
        _reading(_wifi(), wifi=(_nl80211(station=_station("02:00:00:00:00:09")),)),
        _reading(_wifi(), wifi=(_nl80211(station=_station("02:00:00:00:00:09")),)),
    )

    (link,) = snapshot.links
    assert link.state == "Connected"
    assert link.link_changes == 1
    assert _details(link).bssid == Supported[str](value="02:00:00:00:00:09")


def test_reassociating_to_another_bssid_counts_only_the_drop_and_return() -> None:
    snapshot = _snapshot(
        _reading(_wifi()),
        _reading(_wifi(operstate="dormant")),
        _reading(_wifi(), wifi=(_nl80211(station=_station("02:00:00:00:00:09")),)),
    )

    assert snapshot.links[0].link_changes == 2


def test_a_failing_nl80211_leaves_the_sysfs_fields_and_counters_current() -> None:
    snapshot = _snapshot(
        _reading(_wifi(rx_bytes=1000), source="192.0.2.20"),
        _reading(_wifi(rx_bytes=5000), source="192.0.2.20", wifi=None),
    )

    (link,) = snapshot.links
    # Without nl80211, operstate alone says whether it's associated.
    assert link.state == "Connected"
    assert link.ipv4 == "192.0.2.20/24"
    assert link.counters.rx_bytes == 5000
    assert link.throughput is not None
    assert link.throughput.rx_bytes_per_second == 2000.0
    details = _details(link)
    assert details.no_reading_reason == "The Wi-Fi adapter gave no reading."
    assert _values(details) == {
        "ssid": None,
        "bssid": None,
        "frequency_mhz": None,
        "tx_bitrate_bps": None,
        "rx_bitrate_bps": None,
        "signal_dbm": None,
        "link_strength": None,
        "tx_failed": 11,
    }
    assert snapshot.last_sample_age_seconds == 2.0


def test_nl80211_failing_for_one_interface_is_no_reading_for_that_one_only() -> None:
    snapshot = _snapshot(
        _reading(_wifi(), _wifi("wlan1"), wifi=(_nl80211("wlan1", station=_station()),))
    )

    assert _details(_link(snapshot, "wlan0")).no_reading_reason == (
        "The Wi-Fi adapter gave no reading."
    )
    assert _details(_link(snapshot, "wlan1")).no_reading_reason is None


@pytest.mark.parametrize(
    "nl80211",
    [(), (_nl80211("wlan1", station=_station()),)],
    ids=["for every interface", "for one interface"],
)
def test_a_sample_nl80211_failed_in_is_not_a_fully_successful_sample(
    nl80211: tuple[WifiReading, ...],
) -> None:
    snapshot = _snapshot(
        _reading(_wifi(), _wifi("wlan1")),
        _reading(_wifi(), _wifi("wlan1"), wifi=nl80211),
    )

    # The last fully successful sample is the first one.
    assert snapshot.last_sample_age_seconds == 2.0


def test_a_wifi_reading_missing_while_associated_is_not_supported() -> None:
    snapshot = _snapshot(
        _reading(_wifi(), wifi=(_nl80211(station=_station(signal_dbm=None)),))
    )

    details = _details(snapshot.links[0])
    assert details.signal_dbm is None
    assert details.bssid is not None


def test_a_stale_wifi_link_keeps_only_its_failed_sends_total() -> None:
    monitor, clock = _fed(_reading(_wifi()))

    (link,) = monitor.snapshot(clock.tick(7.0)).links

    _blank_but_counters(link)
    assert _values(_details(link))["tx_failed"] == 11
    assert _values(_details(link))["signal_dbm"] is None


@pytest.mark.parametrize("mode", ["ap", "p2p"])
def test_a_separate_access_point_or_p2p_interface_is_not_a_network_link(
    mode: WifiMode,
) -> None:
    snapshot = _snapshot(
        _reading(
            _wifi(),
            _wifi("uap0"),
            wifi=(_nl80211(station=_station()), _nl80211("uap0", mode=mode)),
        )
    )

    assert [link.name for link in snapshot.links] == ["wlan0"]


def test_an_excluded_interface_stays_excluded_while_nl80211_fails() -> None:
    snapshot = _snapshot(
        _reading(
            _wifi(),
            _wifi("p2p0"),
            wifi=(_nl80211(station=_station()), _nl80211("p2p0", mode="p2p")),
        ),
        _reading(_wifi(), _wifi("p2p0"), wifi=None),
    )

    assert [link.name for link in snapshot.links] == ["wlan0"]


# ---------------------------------------------------------------------------
# Wi-Fi link strength: the verdict from the lower median of 10 s of dBm
# ---------------------------------------------------------------------------


def _at(dbm: int | None) -> HostReading:
    """An associated wlan0 whose station reads *dbm*."""
    return _reading(_wifi(), wifi=(_nl80211(station=_station(signal_dbm=dbm)),))


def _strength(snapshot: NetworkLinkSnapshot) -> Level | None:
    """wlan0's Wi-Fi link strength verdict; None when it has no value."""
    details = _details(_link(snapshot, "wlan0"))
    assert details.link_strength is not None, "Wi-Fi link strength not supported"
    return details.link_strength.value


@pytest.mark.parametrize(
    ("dbm", "expected"),
    [(-40, "Good"), (-67, "Good"), (-68, "Marginal"), (-77, "Marginal"), (-78, "Poor")],
)
def test_wifi_link_strength_is_judged_at_the_threshold_edges(
    dbm: int, expected: Level
) -> None:
    assert _strength(_snapshot(_at(dbm))) == expected


def test_a_lone_dip_does_not_flip_wifi_link_strength() -> None:
    assert _strength(_snapshot(_at(-55), _at(-55), _at(-85))) == "Good"


def test_wifi_link_strength_leans_weaker_on_a_tie() -> None:
    # Lower median of {-60, -70}: -70, Marginal.
    assert _strength(_snapshot(_at(-60), _at(-70))) == "Marginal"


def test_wifi_link_strength_follows_a_sustained_drop() -> None:
    assert _strength(_snapshot(_at(-55), _at(-85), _at(-85))) == "Poor"


def test_readings_older_than_10_s_leave_the_wifi_link_strength_window() -> None:
    # Five Poor readings, then five Good ones 2 s apart: the window is
    # the last 10 s, so only the Good ones count.
    snapshot = _snapshot(*[_at(-85)] * 5, *[_at(-55)] * 5)

    assert _strength(snapshot) == "Good"


@pytest.mark.parametrize(
    "gap",
    [
        pytest.param(_reading(_wifi(operstate="dormant")), id="disconnected"),
        pytest.param(_reading(_wifi(), wifi=None), id="nl80211 failed"),
        pytest.param(
            HostReading(
                interfaces=None,
                addresses=(WIFI_ADDR,),
                egress=EgressReading(source=None),
                wifi=(_nl80211(station=_station()),),
            ),
            id="sysfs failed",
        ),
        pytest.param(_at(None), id="no dBm"),
        pytest.param(
            _reading(_wifi(), wifi=(_nl80211(mode="ap"),)), id="setup access point"
        ),
    ],
)
def test_a_gap_has_no_wifi_link_strength_and_clears_the_window(
    gap: HostReading,
) -> None:
    monitor, clock = _fed(_at(-85), _at(-85), gap)
    assert _strength(monitor.snapshot(clock.now)) is None

    # The Poor readings before the gap no longer count.
    monitor.observe(_at(-55), clock.tick())
    assert _strength(monitor.snapshot(clock.now)) == "Good"


def test_a_stale_wifi_link_has_no_wifi_link_strength() -> None:
    monitor, clock = _fed(_at(-55))

    assert _strength(monitor.snapshot(clock.tick(7.0))) is None
