"""End-to-end: the dashboard's Network card.

The server's Network link sampler reads a fake host
(``SP_RTK_BASE_FAKE_NETWORK_LINKS``): the test writes a raw host reading
and the real monitor makes the snapshot the card shows, within a 2 s
sample and a 2 s refresh.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from sp_rtk_base.models.network_link_models import (
    AddressReading,
    Counters,
    EgressReading,
    HostReading,
    InterfaceReading,
)

# Two samples and a refresh, with room to spare.
SOON_MS = 8000
# Past the 6 s stale limit, plus a sample and a refresh.
STALE_MS = 15000

ETH0 = InterfaceReading(
    name="eth0",
    kind="ethernet",
    operstate="up",
    carrier=True,
    speed_mbps=100,
    full_duplex=True,
    counters=Counters(
        rx_bytes=5000, rx_errors=2, tx_errors=1, rx_drops=1200, tx_drops=34
    ),
)
WLAN0 = InterfaceReading(name="wlan0", kind="wifi", operstate="up")
ADDRESSES = (
    AddressReading(interface="eth0", address="192.0.2.10", prefixlen=24),
    AddressReading(interface="eth0", address="2001:db8::10", prefixlen=64),
    AddressReading(interface="wlan0", address="192.0.2.20", prefixlen=24),
)


def _write(path: Path, reading: HostReading) -> None:
    path.write_text(reading.model_dump_json())


@pytest.mark.e2e
def test_both_links_with_the_wifi_uplink_listed_first(
    page: Page, base_url: str, network_links: Path
) -> None:
    _write(
        network_links,
        HostReading(
            interfaces=(ETH0, WLAN0),
            addresses=ADDRESSES,
            egress=EgressReading(source="192.0.2.20"),
        ),
    )
    page.goto(f"{base_url}/")

    card = page.get_by_test_id("network-card")
    expect(card.get_by_test_id("network-uplink")).to_have_text(
        "Wi-Fi (wlan0)", timeout=SOON_MS
    )
    expect(card).to_contain_text(
        "Link health only: whether the caster and Portal are reachable "
        "shows under Destinations."
    )
    expect(card.get_by_test_id("network-freshness")).to_contain_text("Updated")
    expect(card.get_by_test_id("network-link-wlan0-uplink")).to_have_text("Uplink")
    expect(card.get_by_test_id("network-link-eth0-uplink")).to_have_count(0)
    wlan0 = card.get_by_test_id("network-link-wlan0")
    eth0 = card.get_by_test_id("network-link-eth0")
    # The Uplink comes first.
    order = card.locator(
        "[data-testid=network-link-wlan0], [data-testid=network-link-eth0]"
    ).evaluate_all("columns => columns.map(c => c.dataset.testid)")
    assert order == ["network-link-wlan0", "network-link-eth0"]
    expect(wlan0).to_contain_text("Wi-Fi · wlan0")
    expect(wlan0.get_by_test_id("network-link-wlan0-state")).to_have_text("Connected")
    expect(wlan0.get_by_test_id("network-link-wlan0-address")).to_contain_text(
        "192.0.2.20/24"
    )
    expect(wlan0.get_by_test_id("network-link-wlan0-speed")).to_have_count(0)
    expect(eth0).to_contain_text("Ethernet · eth0")
    expect(eth0.get_by_test_id("network-link-eth0-state")).to_have_text("Connected")
    expect(eth0.get_by_test_id("network-link-eth0-speed")).to_contain_text("100 Mbit/s")
    expect(eth0.get_by_test_id("network-link-eth0-speed")).to_contain_text(
        "Full duplex"
    )
    expect(eth0.get_by_test_id("network-link-eth0-address")).to_contain_text(
        "2001:db8::10/64"
    )
    expect(eth0.get_by_test_id("network-link-eth0-traffic")).to_contain_text(
        "↓0 B/s ↑0 B/s", timeout=SOON_MS
    )
    expect(eth0.get_by_test_id("network-link-eth0-counters")).to_contain_text(
        "3 / 1,234"
    )
    expect(eth0.get_by_test_id("network-link-eth0-counters")).to_contain_text(
        "0 link changes"
    )


@pytest.mark.e2e
def test_no_uplink_when_links_are_up_but_nothing_routes(
    page: Page, base_url: str, network_links: Path
) -> None:
    _write(
        network_links,
        HostReading(
            interfaces=(ETH0, WLAN0),
            addresses=ADDRESSES,
            egress=EgressReading(source=None),
        ),
    )
    page.goto(f"{base_url}/")

    card = page.get_by_test_id("network-card")
    expect(card.get_by_test_id("network-uplink")).to_have_text(
        "No Uplink", timeout=SOON_MS
    )
    expect(card.get_by_test_id("network-link-eth0-state")).to_have_text("Connected")
    expect(card.get_by_test_id("network-link-wlan0-state")).to_have_text("Connected")
    expect(card.get_by_text("Uplink", exact=True)).to_have_count(0)


@pytest.mark.e2e
def test_a_stale_sampler_shows_every_value_as_a_dash_except_the_counters(
    page: Page, base_url: str, network_links: Path
) -> None:
    _write(
        network_links,
        HostReading(
            interfaces=(ETH0,),
            addresses=ADDRESSES,
            egress=EgressReading(source="192.0.2.10"),
        ),
    )
    page.goto(f"{base_url}/")
    card = page.get_by_test_id("network-card")
    expect(card.get_by_test_id("network-link-eth0-traffic")).to_contain_text(
        "↓0 B/s", timeout=SOON_MS
    )

    # The readings stop.
    network_links.unlink()

    expect(card.get_by_test_id("network-freshness")).to_contain_text(
        "No reading for", timeout=STALE_MS
    )
    expect(card.get_by_test_id("network-uplink")).to_have_text("No reading")
    expect(card.get_by_test_id("network-link-eth0-state")).to_contain_text("No reading")
    for field in ("speed", "address", "traffic"):
        metric = card.get_by_test_id(f"network-link-eth0-{field}")
        expect(metric).to_contain_text("—")
    expect(card.get_by_test_id("network-link-eth0-speed")).not_to_contain_text("Mbit/s")
    expect(card.get_by_test_id("network-link-eth0-address")).not_to_contain_text(
        "192.0.2.10"
    )
    expect(card.get_by_test_id("network-link-eth0-counters")).to_contain_text(
        "3 / 1,234"
    )
