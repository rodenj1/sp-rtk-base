"""Navigation smoke test — every nav link renders its target page.

This is the cheapest possible end-to-end signal: it verifies the FastAPI
+ NiceGUI server boots, the shared ``page_layout`` renders, and each
top-level page in ``NAVIGATION_SECTIONS`` mounts without raising.  If
any of these break, every other e2e test breaks too — so we run this
first.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

# (path, expected page title text rendered in the H4 header)
PAGES: list[tuple[str, str]] = [
    ("/", "Dashboard"),
    ("/input", "Input"),
    ("/outputs", "Destinations"),
    ("/survey", "Survey-In"),
    ("/settings", "Settings"),
    ("/gps-config", "Advanced GPS"),
]


@pytest.mark.e2e
@pytest.mark.parametrize(("path", "heading"), PAGES)
def test_page_renders(page: Page, base_url: str, path: str, heading: str) -> None:
    """Each top-level page returns 200 and renders its H4 heading."""
    page.goto(f"{base_url}{path}")

    # The shared header label "SP-Base" is always present once the
    # layout has mounted.  Wait for it explicitly so we don't race
    # against NiceGUI's WebSocket hydration.
    expect(page.locator("text=SP-Base").first).to_be_visible(timeout=15_000)

    # The page-specific H4 heading proves the per-page render path
    # actually executed (vs. a 500/redirect that still renders the
    # layout).  ``.first`` because some headings appear in both the
    # drawer label and the page body.
    expect(page.locator(f"text={heading}").first).to_be_visible(timeout=15_000)


@pytest.mark.e2e
def test_health_endpoint_via_browser(page: Page, base_url: str) -> None:
    """The ``/api/health`` JSON endpoint is reachable from the browser context."""
    resp = page.request.get(f"{base_url}/api/health")
    assert resp.ok, f"health endpoint returned {resp.status}"
    payload = resp.json()
    assert payload.get("status") == "ok"
    assert isinstance(payload.get("version"), str)
