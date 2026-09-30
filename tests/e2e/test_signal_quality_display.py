"""End-to-end: each page's Signal Quality display setting (#170).

Detailed card by default on both pages; either page can switch to a
compact chip beside its title, independently of the other.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect


@pytest.fixture
def restore_settings(api_base_url: str) -> Iterator[None]:
    original = httpx.get(f"{api_base_url}/api/settings", timeout=5.0).json()
    yield
    httpx.put(f"{api_base_url}/api/settings", json=original, timeout=5.0)


def _set(api_base_url: str, **display: str) -> None:
    resp = httpx.put(f"{api_base_url}/api/settings", json=display, timeout=5.0)
    assert resp.status_code == 200


@pytest.mark.e2e
def test_both_pages_default_to_the_detailed_card(
    page: Page, base_url: str, connected_gps: None
) -> None:
    for path in ("/survey", "/"):
        page.goto(f"{base_url}{path}")
        expect(page.get_by_test_id("signal-quality-card")).to_be_visible()
        expect(page.get_by_test_id("signal-quality-chip")).to_have_count(0)


@pytest.mark.e2e
def test_survey_can_switch_to_the_chip_while_the_dashboard_keeps_the_card(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    restore_settings: None,
) -> None:
    _set(api_base_url, survey_signal_display="compact")

    page.goto(f"{base_url}/survey")
    chip = page.get_by_test_id("signal-quality-chip")
    expect(chip).to_contain_text("Signal Good", timeout=10_000)
    expect(page.get_by_test_id("signal-quality-card")).to_have_count(0)
    chip.hover()
    expect(
        page.get_by_text("L1 52.0 dB-Hz · L2 51.0 dB-Hz · 28 satellites")
    ).to_be_visible()

    page.goto(f"{base_url}/")
    expect(page.get_by_test_id("signal-quality-card")).to_be_visible()
    expect(page.get_by_test_id("signal-quality-chip")).to_have_count(0)


@pytest.mark.e2e
def test_the_dashboard_chip_shows_why_there_is_no_data(
    page: Page, base_url: str, api_base_url: str, restore_settings: None
) -> None:
    _set(api_base_url, dashboard_signal_display="compact")

    page.goto(f"{base_url}/")
    chip = page.get_by_test_id("signal-quality-chip")
    expect(chip).to_contain_text("Signal: no data")
    chip.hover()
    expect(
        page.get_by_text("Relay is stopped. Signal Quality shows while it runs.")
    ).to_be_visible()


@pytest.mark.e2e
def test_the_settings_page_sets_each_page_s_signal_display(
    page: Page, base_url: str, api_base_url: str, restore_settings: None
) -> None:
    page.goto(f"{base_url}/settings")
    page.get_by_label("Dashboard signal display").click()
    page.get_by_role("option", name="Compact chip").click()
    page.get_by_role("button", name="Save Settings").click()
    expect(page.locator("text=Settings saved").first).to_be_visible(timeout=10_000)

    saved = httpx.get(f"{api_base_url}/api/settings", timeout=5.0).json()
    assert (saved["dashboard_signal_display"], saved["survey_signal_display"]) == (
        "compact",
        "detailed",
    )
