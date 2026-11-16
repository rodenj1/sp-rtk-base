"""End-to-end: the Network page's polls update it in place (rtk_development#178).

Each element NiceGUI creates and later deletes leaves a little memory
behind in the browser (see #177, where the dashboard grew this way). The
Network page polls its Uplink card every 2 s and its saved networks
every 5 s, so a page that redrew them on every poll would grow without
bound while it stayed on screen. So, once the page has loaded, a poll
changes text, colours and visibility, and creates or deletes elements
only when the page's structure changes (a saved network appears or goes).

The server's nmcli is a fake on its ``PATH`` that reads the saved and
active profiles from files the tests write; the Uplink comes from the fake
Network link host (``SP_RTK_BASE_FAKE_NETWORK_LINKS``). The page exists
only in appliance mode, so this module runs its own server.
"""

from __future__ import annotations

import os
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

#: The polls' intervals, in ms: the Uplink card's and the saved list's.
STATUS_POLL_MS = 2000
SAVED_POLL_MS = 5000

#: Every NiceGUI element's id on the page (``c<N>``).
_ELEMENT_IDS_JS = """() => Array.from(document.querySelectorAll('[id]'))
    .map(e => e.id).filter(i => /^c\\d+$/.test(i))"""

# The calls the Network page makes, answered from files in $NMCLI_STATE;
# anything else succeeds with no output.
FAKE_NMCLI = """#!/bin/sh
case "$*" in
  "-t -f TYPE,NAME connection show")
    sed 's/^/802-11-wireless:/' "$NMCLI_STATE/saved" ;;
  "-t -f NAME connection show --active") cat "$NMCLI_STATE/active" ;;
  "-g 802-11-wireless.ssid,802-11-wireless.band connection show id "*)
    printf '%s\\n\\n' "${*##* id }" ;;
  "-t -f DEVICE,TYPE device") echo "wlan0:wifi" ;;
  "-g WIFI-PROPERTIES.5GHZ device show wlan0") echo "yes" ;;
  "-t -f SSID,SIGNAL,SECURITY,FREQ device wifi list --rescan yes")
    printf 'Cats:100:WPA2:2412 MHz\\nCats:84:WPA2:5180 MHz\\n' ;;
esac
exit 0
"""

ETH0 = InterfaceReading(name="eth0", kind="ethernet", operstate="up", carrier=True)
WLAN0 = InterfaceReading(name="wlan0", kind="wifi", operstate="up")
WLAN0_ASSOCIATED = WifiReading(
    interface="wlan0",
    mode="station",
    wiphy=0,
    frequency_mhz=5745,
    ssid="Cats",
    station=StationReading(bssid="02:00:00:00:00:01", signal_dbm=-55),
)
ADDRESSES = (
    AddressReading(interface="eth0", address="192.0.2.10", prefixlen=24),
    AddressReading(interface="wlan0", address="192.0.2.20", prefixlen=24),
)


#: The address the kernel picks for outbound traffic on each interface.
_SOURCE = {"eth0": "192.0.2.10", "wlan0": "192.0.2.20"}


def _write_uplink(path: Path, source: str | None, signal_dbm: int = -55) -> None:
    wifi = WLAN0_ASSOCIATED.model_copy(
        update={
            "station": StationReading(bssid="02:00:00:00:00:01", signal_dbm=signal_dbm)
        }
    )
    reading = HostReading(
        interfaces=(ETH0, WLAN0),
        addresses=ADDRESSES,
        egress=EgressReading(source=_SOURCE.get(source) if source else None),
        wifi=(wifi,),
    )
    path.write_text(reading.model_dump_json())


@pytest.fixture(scope="module")
def nmcli_state(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("nmcli-state")


def _saved(state: Path, *profiles: str, active: str = "Cats") -> None:
    (state / "saved").write_text("".join(f"{p}\n" for p in profiles))
    (state / "active").write_text(f"{active}\n")


@pytest.fixture(scope="module")
def nmcli_server(
    tmp_path_factory: pytest.TempPathFactory,
    fake_pypi_dir: Path,
    update_dir: Path,
    host_setup_file: Path,
    portal_agent_socket: Path,
    network_links_file: Path,
    nmcli_state: Path,
) -> Iterator[str]:
    fake_bin = tmp_path_factory.mktemp("fake-nmcli")
    nmcli = fake_bin / "nmcli"
    nmcli.write_text(FAKE_NMCLI)
    nmcli.chmod(0o755)
    net_config = tmp_path_factory.mktemp("net-config") / "net-provision.yaml"
    net_config.write_text("ap_password: sticker-secret\n")
    _saved(nmcli_state, "Cats", "Dogs")
    with running_server(
        tmp_path_factory,
        fake_pypi_dir,
        update_dir,
        host_setup_file,
        portal_agent_socket,
        network_links_file=network_links_file,
        deployment_mode="appliance",
        extra_env={
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "SP_RTK_BASE_NET_CONFIG": str(net_config),
            "NMCLI_STATE": str(nmcli_state),
        },
    ) as url:
        yield url


@pytest.fixture()
def server(
    nmcli_server: str, nmcli_state: Path, network_links_file: Path
) -> Iterator[str]:
    _saved(nmcli_state, "Cats", "Dogs")
    _write_uplink(network_links_file, "wlan0")
    yield nmcli_server
    _saved(nmcli_state, "Cats", "Dogs")
    _write_uplink(network_links_file, "wlan0")


def _element_ids(page: Page) -> set[str]:
    return set(page.evaluate(_ELEMENT_IDS_JS))


def _settled_ids(page: Page) -> set[str]:
    """The page's element ids once a saved-list poll goes by unchanged."""
    previous = _element_ids(page)
    for _ in range(10):
        page.wait_for_timeout(SAVED_POLL_MS + 500)
        current = _element_ids(page)
        if current == previous:
            return current
        previous = current
    return previous


def _assert_stable_for(page: Page, ms: int) -> None:
    baseline = _settled_ids(page)
    for i in range(ms // STATUS_POLL_MS):
        page.wait_for_timeout(STATUS_POLL_MS)
        current = _element_ids(page)
        assert current == baseline, (
            f"poll {i + 1}: {len(current - baseline)} elements created, "
            f"{len(baseline - current)} deleted"
        )


def _open(page: Page, url: str) -> None:
    page.goto(f"{url}/network")
    expect(page.get_by_test_id("network-page-uplink")).to_have_text("Wi-Fi (wlan0)")
    expect(page.get_by_text("Dogs", exact=True)).to_be_visible()


@pytest.mark.e2e
def test_polls_keep_the_same_elements_while_the_readings_change(
    page: Page, server: str, network_links_file: Path
) -> None:
    _open(page, server)
    strength = page.get_by_test_id("network-page-strength")
    expect(strength).to_be_visible()
    before = strength.inner_text()

    baseline = _settled_ids(page)
    _write_uplink(network_links_file, "wlan0", signal_dbm=-80)
    for i in range(6):  # two saved-list polls, six of the Uplink card's
        page.wait_for_timeout(STATUS_POLL_MS)
        current = _element_ids(page)
        assert current == baseline, (
            f"poll {i + 1}: {len(current - baseline)} elements created, "
            f"{len(baseline - current)} deleted"
        )

    # The readings did change: the page was updated, not frozen.
    expect(strength).not_to_have_text(before)


@pytest.mark.e2e
def test_the_uplink_changing_shows_without_a_reload(
    page: Page, server: str, network_links_file: Path
) -> None:
    _open(page, server)
    expect(page.get_by_test_id("network-page-band")).to_be_visible()

    _write_uplink(network_links_file, "eth0")
    expect(page.get_by_test_id("network-page-uplink")).to_have_text(
        "Ethernet (eth0)", timeout=10_000
    )
    expect(page.get_by_test_id("network-page-band")).to_be_hidden()
    _assert_stable_for(page, 3 * STATUS_POLL_MS)

    _write_uplink(network_links_file, None)
    expect(page.get_by_test_id("network-page-uplink")).to_have_text(
        "No Uplink", timeout=10_000
    )
    _assert_stable_for(page, 3 * STATUS_POLL_MS)

    _write_uplink(network_links_file, "wlan0")
    expect(page.get_by_test_id("network-page-uplink")).to_have_text(
        "Wi-Fi (wlan0)", timeout=10_000
    )
    expect(page.get_by_test_id("network-page-band")).to_be_visible()
    nav = page.evaluate("performance.getEntriesByType('navigation')[0].type")
    assert nav != "reload"


@pytest.mark.e2e
def test_a_saved_network_coming_and_going_keeps_the_other_rows(
    page: Page, server: str, nmcli_state: Path
) -> None:
    _open(page, server)
    cats = page.get_by_test_id("network-saved-row").filter(has_text="Cats")
    expect(cats).to_have_count(1)
    cats_id = cats.get_attribute("id")

    _saved(nmcli_state, "Cats", "Dogs", "Fish")
    expect(page.get_by_text("Fish", exact=True)).to_be_visible(timeout=10_000)
    expect(cats).to_have_attribute("id", cats_id or "")

    _saved(nmcli_state, "Cats", "Fish")
    expect(page.get_by_text("Dogs", exact=True)).to_have_count(0, timeout=10_000)
    expect(cats).to_have_attribute("id", cats_id or "")

    _saved(nmcli_state)
    expect(page.get_by_text("No saved WiFi networks.")).to_be_visible(timeout=10_000)
    _saved(nmcli_state, "Cats")
    expect(page.get_by_text("No saved WiFi networks.")).to_be_hidden(timeout=10_000)
    expect(page.get_by_test_id("network-saved-row")).to_have_count(1)
