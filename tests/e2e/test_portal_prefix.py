"""sp-rtk-base behind the Portal's path prefix (spec #82, ADR 0004).

The Portal serves a base at ``/b/<id>/s/<token>/…`` and tells it so with
``X-Forwarded-Prefix``.  Here a stand-in proxy does that in front of the
real server, and the browser loads the prefixed URLs top-level.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.portal_helpers import open_event_log, relay_started_event
from tests.fixtures.portal_proxy import PortalProxy

PAGES: list[tuple[str, str]] = [
    ("/", "Dashboard"),
    ("/input", "Input"),
    ("/outputs", "Destinations"),
    ("/survey", "Survey-In"),
    ("/settings", "Settings"),
    ("/gps-config", "Advanced GPS"),
]


@pytest.fixture(scope="module")
def portal(base_url: str) -> Iterator[PortalProxy]:
    with PortalProxy(base_url) as proxy:
        yield proxy


@pytest.mark.e2e
@pytest.mark.parametrize(("path", "heading"), PAGES)
def test_every_page_renders_behind_the_prefix(
    page: Page, portal: PortalProxy, path: str, heading: str
) -> None:
    page.goto(f"{portal.url}{portal.prefix}{path}")

    expect(page.locator("text=SP-Base").first).to_be_visible(timeout=15_000)
    expect(page.locator(f"text={heading}").first).to_be_visible(timeout=15_000)
    page.wait_for_function("() => window.socket && window.socket.connected")


@pytest.mark.e2e
def test_live_event_log_streams_behind_the_prefix(
    page: Page, portal: PortalProxy, api_base_url: str, clean_config: None
) -> None:
    open_event_log(page, lambda: page.goto(f"{portal.url}{portal.prefix}/"))
    log = page.get_by_test_id("event-log")

    with relay_started_event(api_base_url) as text:
        expect(log.get_by_text(text).first).to_be_visible(timeout=15_000)


@pytest.mark.e2e
def test_drawer_link_stays_behind_the_prefix(page: Page, portal: PortalProxy) -> None:
    page.goto(f"{portal.url}{portal.prefix}/")
    expect(page.locator("text=Dashboard").first).to_be_visible(timeout=15_000)

    page.locator("button:has(i:text('menu'))").first.click()
    page.locator(f'a[href="{portal.prefix}/survey"]').first.click()

    expect(page).to_have_url(f"{portal.url}{portal.prefix}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)
    page.wait_for_function("() => window.socket && window.socket.connected")
