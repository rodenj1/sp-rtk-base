"""End-to-end interaction test for the Settings page.

This test exercises a **real** button click in the live NiceGUI UI:

1. Navigate to /settings
2. Click "Save Settings"
3. Wait for the Quasar notification "Settings saved" to appear

If the UI's reactive bindings or NiceGUI's WebSocket layer breaks, the
notification will never appear and this test will fail.  That's
exactly the kind of regression API-only tests can't catch.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_save_settings_button_emits_notification(
    page: Page,
    base_url: str,
) -> None:
    """Clicking ``Save Settings`` raises a positive Quasar notification."""
    page.goto(f"{base_url}/settings")

    # Wait for the page heading so we know NiceGUI has fully mounted
    # and the WebSocket handshake completed.
    expect(page.locator("text=Settings").first).to_be_visible(timeout=15_000)

    # The Save Settings button — Quasar renders the icon + label as
    # siblings inside the .q-btn span; the "Save Settings" label is
    # unique enough for a stable text-based selector.
    save_btn = page.get_by_role("button", name="Save Settings")
    expect(save_btn).to_be_visible(timeout=10_000)
    save_btn.click()

    # Quasar's notification plugin renders the toast in a fixed
    # overlay container.  Wait for the "Settings saved" text.
    expect(page.locator("text=Settings saved").first).to_be_visible(timeout=10_000)


@pytest.mark.e2e
def test_settings_page_shows_version_card(page: Page, base_url: str) -> None:
    """The Version Information card lists SP-Base and Python versions."""
    page.goto(f"{base_url}/settings")

    expect(page.locator("text=Version Information").first).to_be_visible(timeout=15_000)
    expect(page.locator("text=SP-Base").first).to_be_visible()
    expect(page.locator("text=Python").first).to_be_visible()


@pytest.mark.e2e
def test_saving_from_the_page_keeps_settings_it_did_not_change(
    page: Page, base_url: str, api_base_url: str
) -> None:
    """Saving the Settings page updates only its fields (#165)."""
    import httpx

    httpx.put(
        f"{api_base_url}/api/settings",
        json={"auto_start": True, "status_poll_interval": 2.0},
        timeout=5.0,
    )
    try:
        page.goto(f"{base_url}/settings")
        interval = page.get_by_label("Status poll interval (seconds)")
        expect(interval).to_be_visible(timeout=15_000)
        interval.fill("5")
        page.get_by_role("button", name="Save Settings").click()
        expect(page.locator("text=Settings saved").first).to_be_visible(timeout=10_000)

        saved = httpx.get(f"{api_base_url}/api/settings", timeout=5.0).json()
        assert saved == {"auto_start": True, "status_poll_interval": 5.0}
    finally:
        httpx.put(
            f"{api_base_url}/api/settings",
            json={"auto_start": False, "status_poll_interval": 2.0},
            timeout=5.0,
        )
