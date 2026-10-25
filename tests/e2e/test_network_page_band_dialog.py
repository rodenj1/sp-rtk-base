"""End-to-end: the Network page's Band dialog on a netplan-named profile.

Found on a real base (spec #147): the saved profile is
``netplan-wlan0-Cats`` and its SSID is ``Cats``. The page's saved list and
status card are redrawn on a poll, and a dialog opened from them used to
be deleted with them, so the Band dialog and its confirm closed by
themselves within seconds. And the scan was matched by profile name, so
the active network was never heard and its 2.4 and 5 GHz choices were
disabled.

The server's nmcli is a fake on its ``PATH`` that plays that base; the
page exists only in appliance mode, so this module runs its own server.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import running_server

PROFILE = "netplan-wlan0-Cats"

# The base's nmcli, answering the calls the Network page makes; anything
# else succeeds with no output. Each answer is what nmcli printed there.
FAKE_NMCLI = f"""#!/bin/sh
case "$*" in
  "-t -f TYPE,NAME connection show") echo "802-11-wireless:{PROFILE}" ;;
  "-t -f NAME connection show --active") echo "{PROFILE}" ;;
  "-g 802-11-wireless.ssid,802-11-wireless.band connection show id {PROFILE}")
    printf 'Cats\\n\\n' ;;
  "-t -f DEVICE,TYPE device") echo "wlan0:wifi" ;;
  "-g WIFI-PROPERTIES.5GHZ device show wlan0") echo "yes" ;;
  "-t -f SSID,SIGNAL,SECURITY,FREQ device wifi list --rescan yes")
    printf 'Cats:100:WPA2:2412 MHz\\nCats:84:WPA2:5180 MHz\\n' ;;
esac
exit 0
"""

# Longer than the saved list's 5 s poll and two of the status card's 2 s
# ones: a dialog still open after this has outlived a redraw of both.
PAST_A_POLL_MS = 6000


@pytest.fixture(scope="module")
def netplan_server(
    tmp_path_factory: pytest.TempPathFactory,
    fake_pypi_dir: Path,
    update_dir: Path,
    host_setup_file: Path,
    portal_agent_socket: Path,
    network_links_file: Path,
) -> Iterator[str]:
    fake_bin = tmp_path_factory.mktemp("fake-nmcli")
    nmcli = fake_bin / "nmcli"
    nmcli.write_text(FAKE_NMCLI)
    nmcli.chmod(0o755)
    net_config = tmp_path_factory.mktemp("net-config") / "net-provision.yaml"
    net_config.write_text("ap_password: sticker-secret\n")
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
        },
    ) as url:
        yield url


@pytest.mark.e2e
def test_the_band_dialog_hears_the_network_and_outlives_the_polls(
    page: Page, netplan_server: str
) -> None:
    page.goto(f"{netplan_server}/network")
    # The Band dialog's hints come from the page's scan: wait for it.
    expect(page.get_by_test_id("network-scan-bands")).to_have_text("2.4 GHz + 5 GHz")
    page.get_by_role("button", name="Band").click()

    dialog = page.get_by_role("dialog").filter(has_text="Wi-Fi band for 'Cats'")
    expect(dialog).to_be_visible()
    two_four = dialog.get_by_role("button", name="2.4 GHz")
    five = dialog.get_by_role("button", name="5 GHz")
    expect(dialog).to_contain_text("Heard now at 100%.")
    expect(dialog).to_contain_text("Heard now at 84%.")
    expect(two_four).to_be_enabled()
    expect(five).to_be_enabled()

    page.wait_for_timeout(PAST_A_POLL_MS)
    expect(dialog).to_be_visible()

    # The active network asks to confirm the reconnect: that dialog, too,
    # stays open across the polls.
    five.click()
    confirm = page.get_by_role("dialog").filter(has_text="Use 5 GHz for 'Cats'?")
    expect(confirm).to_be_visible()
    page.wait_for_timeout(PAST_A_POLL_MS)
    expect(confirm).to_be_visible()
    confirm.get_by_role("button", name="Cancel").click()
    expect(confirm).to_be_hidden()
