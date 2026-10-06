"""The GPS config page's baud fields over a Bluetooth Console link (rtk_development#47).

Against the fake GPS driver, whose Bluetooth link reaches the Input
profile's module on UART2 at 115200, like the bench base. The module
can't follow a baud change on its UART, so the page disables that UART's
baud field and says why, naming the rate. The other UART stays editable,
and over a serial cable nothing is locked.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

MODULE = {
    "device_name": "RTK_BASE_TST",
    "mac_address": "98:D3:71:FE:FC:47",
    "pin": "1234",
}


_DISABLED = re.compile(r"\bq-field--disabled\b")


def _put_input(api_base_url: str, source: str, config: dict[str, Any]) -> None:
    resp = httpx.put(
        f"{api_base_url}/api/input",
        json={"source": source, "config": config},
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text


def _connect(api_base_url: str, link: dict[str, Any]) -> None:
    resp = httpx.post(
        f"{api_base_url}/api/device/connect",
        json={"vendor": "fake", "link": link},
        timeout=15.0,
    )
    assert resp.status_code == 200, resp.text


@pytest.fixture()
def bluetooth_input(api_base_url: str) -> Iterator[None]:
    """A Bluetooth Input profile, and the saved one put back afterwards."""
    before = httpx.get(f"{api_base_url}/api/input", timeout=5.0).json()
    httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
    _put_input(api_base_url, "bluetooth", MODULE)
    try:
        yield
    finally:
        httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
        if before.get("configured"):
            _put_input(api_base_url, before["source"], before["config"])


def _goto_gps_config(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/gps-config")
    expect(page.locator(".sync-badge")).to_have_text("In sync", timeout=15_000)


def _expect_disabled(select: Locator, *, disabled: bool) -> None:
    """A Quasar ``q-select`` is disabled by class: no native control to ask."""
    if disabled:
        expect(select).to_have_class(_DISABLED)
    else:
        expect(select).not_to_have_class(_DISABLED)


@pytest.mark.e2e
def test_over_bluetooth_the_module_uarts_baud_is_locked_with_its_rate(
    page: Page, base_url: str, api_base_url: str, bluetooth_input: None
) -> None:
    _connect(api_base_url, {"kind": "bluetooth"})

    _goto_gps_config(page, base_url)

    uart1 = page.locator(".hw-field-baud-uart1")
    uart2 = page.locator(".hw-field-baud-uart2")
    expect(uart2).to_contain_text("115200")
    _expect_disabled(uart2, disabled=True)
    _expect_disabled(uart1, disabled=False)
    note = page.locator(".hw-field-baud-lock-note")
    expect(note).to_contain_text("UART2 baud is locked at 115200")
    expect(note).to_contain_text("serial cable")


@pytest.mark.e2e
def test_over_a_serial_cable_no_baud_is_locked(
    page: Page, base_url: str, api_base_url: str, bluetooth_input: None
) -> None:
    _connect(api_base_url, {"kind": "serial", "port": "FAKE", "baud_rate": 115200})

    _goto_gps_config(page, base_url)

    _expect_disabled(page.locator(".hw-field-baud-uart1"), disabled=False)
    _expect_disabled(page.locator(".hw-field-baud-uart2"), disabled=False)
    expect(page.locator(".hw-field-baud-lock-note")).to_have_count(0)
