"""End-to-end: the Network page's Uplink block.

The block reads the same Network link snapshot as the dashboard's Network
card: the server's sampler reads a fake host
(``SP_RTK_BASE_FAKE_NETWORK_LINKS``) the test writes, so it never runs
nmcli for this block.

The Network page exists only in appliance mode, so this module runs its
own server in that mode.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from sp_rtk_base.models.network_link_models import (
    AddressReading,
    EgressReading,
    HostReading,
    InterfaceReading,
    StationReading,
    WifiReading,
)
from tests.e2e.conftest import running_server

# Two samples and a refresh, with room to spare.
SOON_MS = 8000

ETH0 = InterfaceReading(name="eth0", kind="ethernet", operstate="up", carrier=True)
WLAN0 = InterfaceReading(name="wlan0", kind="wifi", operstate="up")
WLAN0_ASSOCIATED = WifiReading(
    interface="wlan0",
    mode="station",
    wiphy=0,
    frequency_mhz=5745,
    ssid="Test",
    station=StationReading(bssid="02:00:00:00:00:01", signal_dbm=-55),
)
ADDRESSES = (
    AddressReading(interface="eth0", address="192.0.2.10", prefixlen=24),
    AddressReading(interface="wlan0", address="192.0.2.20", prefixlen=24),
)


@pytest.fixture(scope="module")
def appliance_server(
    tmp_path_factory: pytest.TempPathFactory,
    fake_pypi_dir: Path,
    update_dir: Path,
    host_setup_file: Path,
    portal_agent_socket: Path,
    network_links_file: Path,
) -> Iterator[str]:
    with running_server(
        tmp_path_factory,
        fake_pypi_dir,
        update_dir,
        host_setup_file,
        portal_agent_socket,
        network_links_file=network_links_file,
        deployment_mode="appliance",
    ) as url:
        yield url


def _write(path: Path, source: str | None) -> None:
    reading = HostReading(
        interfaces=(ETH0, WLAN0),
        addresses=ADDRESSES,
        egress=EgressReading(source=source),
        wifi=(WLAN0_ASSOCIATED,),
    )
    path.write_text(reading.model_dump_json())


@pytest.mark.e2e
def test_a_wifi_uplink_shows_its_link_strength_even_with_a_cable_in(
    page: Page, appliance_server: str, network_links: Path
) -> None:
    """Traffic leaves by Wi-Fi, so a plugged-in cable doesn't win."""
    _write(network_links, source="192.0.2.20")
    page.goto(f"{appliance_server}/network")

    expect(page.get_by_test_id("network-page-uplink")).to_have_text(
        "Wi-Fi (wlan0)", timeout=SOON_MS
    )
    expect(page.get_by_test_id("network-page-strength")).to_have_text(
        "Wi-Fi link: Good (-55 dBm)"
    )
    expect(page.get_by_test_id("network-page-network")).to_have_text("Network: Test")
    expect(page.get_by_test_id("network-page-address")).to_have_text(
        "IP: 192.0.2.20/24"
    )
    expect(page.get_by_text("Signal:")).to_have_count(0)


@pytest.mark.e2e
def test_an_ethernet_uplink_has_no_link_strength(
    page: Page, appliance_server: str, network_links: Path
) -> None:
    _write(network_links, source="192.0.2.10")
    page.goto(f"{appliance_server}/network")

    expect(page.get_by_test_id("network-page-uplink")).to_have_text(
        "Ethernet (eth0)", timeout=SOON_MS
    )
    expect(page.get_by_test_id("network-page-strength")).to_have_count(0)


@pytest.mark.e2e
def test_no_uplink_when_nothing_routes(
    page: Page, appliance_server: str, network_links: Path
) -> None:
    _write(network_links, source=None)
    page.goto(f"{appliance_server}/network")

    expect(page.get_by_test_id("network-page-uplink")).to_have_text(
        "No Uplink", timeout=SOON_MS
    )
