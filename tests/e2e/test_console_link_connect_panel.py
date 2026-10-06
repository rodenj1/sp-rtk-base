"""The Connect panel's Serial cable / Bluetooth toggle (rtk_development#43).

Against the fake GPS driver, whose Bluetooth link reaches the Input
profile's module as if already paired, on UART2 like the bench base. One
walk covers what the ticket asks of the browser: the toggle, the module
line from the Bluetooth Input profile, the set-up prompt when the Input
profile isn't Bluetooth, the toggle locking while connected, and the
panel reopening on the last kind used, on both Connection cards.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

MODULE = {
    "device_name": "RTK_BASE_TST",
    "mac_address": "98:D3:71:FE:FC:47",
    "pin": "1234",
}


def _put_input(api_base_url: str, source: str, config: dict[str, Any]) -> None:
    resp = httpx.put(
        f"{api_base_url}/api/input",
        json={"source": source, "config": config},
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text


@pytest.fixture()
def saved_input(api_base_url: str) -> Iterator[None]:
    """Put the saved Input profile back as it was after the test."""
    before = httpx.get(f"{api_base_url}/api/input", timeout=5.0).json()
    httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
    try:
        yield
    finally:
        httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
        if before.get("configured"):
            _put_input(api_base_url, before["source"], before["config"])


def _choose_fake_driver(page: Page) -> None:
    page.get_by_label("Driver").click()
    page.get_by_role("option", name="fake", exact=True).click()


@pytest.mark.e2e
def test_bluetooth_side_of_the_connect_panel(
    page: Page, base_url: str, api_base_url: str, saved_input: None
) -> None:
    kind = page.get_by_test_id("console-link-kind")
    serial_btn = kind.get_by_role("button", name="Serial cable")
    bluetooth_btn = kind.get_by_role("button", name="Bluetooth")
    connect_btn = page.get_by_role("button", name="Connect", exact=True)
    status = page.get_by_test_id("console-link-status")

    # No Bluetooth Input profile: the Bluetooth side prompts to set one up,
    # and Connect can't be pressed.
    _put_input(api_base_url, "tcp", {"host": "127.0.0.1", "port": 2101})
    page.goto(f"{base_url}/survey")
    serial_btn.click()
    expect(page.get_by_label("Serial Port")).to_be_visible(timeout=15_000)
    expect(page.get_by_label("Baud Rate")).to_be_visible()
    bluetooth_btn.click()
    expect(page.get_by_test_id("console-link-setup-prompt")).to_contain_text(
        "Set one up on the Input page"
    )
    expect(page.get_by_label("Serial Port")).to_be_hidden()
    expect(page.get_by_label("Baud Rate")).to_be_hidden()
    expect(connect_btn).to_be_disabled()

    # A Bluetooth Input profile: its module is shown, and Connect reaches it.
    _put_input(api_base_url, "bluetooth", MODULE)
    page.goto(f"{base_url}/survey")
    bluetooth_btn.click()
    expect(page.get_by_test_id("console-link-module")).to_contain_text(
        "RTK_BASE_TST · 98:D3:71:FE:FC:47"
    )
    expect(page.get_by_text("Change it on the Input page")).to_be_visible()
    _choose_fake_driver(page)
    connect_btn.click()

    expect(status).to_have_text(
        "Connected: Bluetooth · RTK_BASE_TST · console port UART2", timeout=15_000
    )
    expect(page.get_by_test_id("console-link-stages")).to_contain_text("Already paired")
    expect(page.get_by_test_id("console-link-module")).to_contain_text("Paired")
    # Locked while connected.
    expect(serial_btn).to_be_disabled()
    expect(bluetooth_btn).to_be_disabled()

    # The GPS config Connection card shows the same, also locked.
    page.goto(f"{base_url}/gps-config")
    expect(status).to_have_text(
        "Connected: Bluetooth · RTK_BASE_TST · console port UART2", timeout=15_000
    )
    expect(serial_btn).to_be_disabled()

    # Disconnected, the panel reopens on the last kind used, unlocked.
    page.get_by_role("button", name="Disconnect").click()
    expect(status).to_have_text("Disconnected", timeout=10_000)
    page.goto(f"{base_url}/survey")
    expect(page.get_by_test_id("console-link-module")).to_be_visible(timeout=15_000)
    expect(bluetooth_btn).to_be_enabled()

    # Back to a serial connect, so the saved kind is serial for the
    # tests that follow.
    serial_btn.click()
    page.get_by_label("Serial Port").fill("FAKE")
    page.get_by_label("Serial Port").press("Enter")
    _choose_fake_driver(page)
    connect_btn.click()
    expect(status).to_have_text(re.compile(r"Connected: FAKE @ \d+"), timeout=15_000)
    page.get_by_role("button", name="Disconnect").click()
    expect(status).to_have_text("Disconnected", timeout=10_000)
