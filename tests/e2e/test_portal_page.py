"""The local Portal page (ticket #83, spec #82).

The e2e server talks to a real, idle Portal agent on a socket in a temp dir
(see ``portal_agent_socket`` in conftest), so an unclaimed base shows
"Not connected" and offers "Connect to Portal".
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_an_unclaimed_base_is_not_connected(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/portal")

    expect(page.get_by_test_id("portal-headline")).to_have_text(
        "Not connected", timeout=15_000
    )
    expect(page.get_by_role("button", name="Connect to Portal")).to_be_visible()


@pytest.mark.e2e
def test_the_drawer_links_to_the_portal_page(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/")
    expect(page.locator("text=SP-Base").first).to_be_visible(timeout=15_000)

    # The drawer starts closed (behavior=mobile); the header's menu opens it.
    page.locator(".q-header button").first.click()
    page.locator(".q-drawer a[href='/portal']").click()

    expect(page).to_have_url(f"{base_url}/portal")
    expect(page.get_by_test_id("portal-headline")).to_have_text(
        "Not connected", timeout=15_000
    )
