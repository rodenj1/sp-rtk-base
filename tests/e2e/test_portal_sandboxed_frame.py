"""sp-rtk-base inside the Portal's sandboxed frame (spec #82, ADR 0004).

The Portal shows a base in ``<iframe sandbox="allow-scripts allow-forms
allow-downloads">`` with no ``allow-same-origin``, so the base runs with
an opaque origin: no cookies, and reading ``sessionStorage`` or
``localStorage`` throws.  Each test runs in Chromium and Firefox against
the real server behind the stand-in Portal proxy.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import (
    Browser,
    Frame,
    FrameLocator,
    Page,
    Playwright,
    expect,
)

from tests.e2e.portal_helpers import open_event_log, relay_started_event
from tests.fixtures.portal_proxy import PortalProxy

PAGES: list[tuple[str, str]] = [
    ("", "Dashboard"),
    ("input", "Input"),
    ("outputs", "Destinations"),
    ("survey", "Survey-In"),
    ("settings", "Settings"),
    ("gps-config", "Advanced GPS"),
]

SOCKET_CONNECTED = "() => !!(window.socket && window.socket.connected)"


@pytest.fixture(scope="module")
def portal(base_url: str) -> Iterator[PortalProxy]:
    with PortalProxy(base_url) as proxy:
        yield proxy


@pytest.fixture(scope="module", params=["chromium", "firefox"])
def engine(request: pytest.FixtureRequest, playwright: Playwright) -> Iterator[Browser]:
    browser = getattr(playwright, request.param).launch()
    yield browser
    browser.close()


@pytest.fixture()
def top(engine: Browser) -> Iterator[Page]:
    """The Portal's top-level page; the base runs in its frame."""
    context = engine.new_context(
        viewport={"width": 1300, "height": 850}, accept_downloads=True
    )
    page = context.new_page()
    yield page
    context.close()


def _base(top: Page) -> FrameLocator:
    return top.frame_locator("#base")


def _frame(top: Page) -> Frame:
    handle = top.locator("#base").element_handle()
    assert handle is not None
    frame = handle.content_frame()
    assert frame is not None
    return frame


def _wait_connected(top: Page) -> Frame:
    frame = _frame(top)
    frame.wait_for_function(SOCKET_CONNECTED, timeout=15_000)
    return frame


@pytest.mark.e2e
@pytest.mark.parametrize(("path", "heading"), PAGES)
def test_every_page_renders_in_the_frame(
    top: Page, portal: PortalProxy, path: str, heading: str
) -> None:
    top.goto(portal.framed(path))

    base = _base(top)
    expect(base.locator("text=SP-Base").first).to_be_visible(timeout=15_000)
    expect(base.locator(f"text={heading}").first).to_be_visible(timeout=15_000)
    frame = _wait_connected(top)
    assert frame.evaluate("() => self.origin") == "null"


@pytest.mark.e2e
def test_storage_stand_in_works_in_the_frame(top: Page, portal: PortalProxy) -> None:
    top.goto(portal.framed("/"))
    frame = _wait_connected(top)

    for name in ("sessionStorage", "localStorage"):
        value = frame.evaluate(
            f"() => {{ {name}.setItem('k', 'v'); return {name}.getItem('k'); }}"
        )
        assert value == "v", name


@pytest.mark.e2e
def test_socket_stays_connected_and_live_events_arrive(
    top: Page, portal: PortalProxy, api_base_url: str, clean_config: None
) -> None:
    open_event_log(top, lambda: top.goto(portal.framed("/")))
    frame = _wait_connected(top)
    frame.evaluate(
        "() => { window.__disconnects = 0;"
        " window.socket.on('disconnect', () => window.__disconnects++); }"
    )

    with relay_started_event(api_base_url) as text:
        log = _base(top).get_by_test_id("event-log")
        expect(log.get_by_text(text).first).to_be_visible(timeout=15_000)

    top.wait_for_timeout(6_000)  # past a socket.io ping and an Event log keepalive
    assert frame.evaluate(SOCKET_CONNECTED)
    assert frame.evaluate("() => window.__disconnects") == 0


@pytest.mark.e2e
def test_drawer_link_navigates_the_frame(top: Page, portal: PortalProxy) -> None:
    top.goto(portal.framed("/"))
    _wait_connected(top)
    base = _base(top)

    base.locator("button:has(i:text('menu'))").first.click()
    base.locator(f'a[href="{portal.prefix}/survey"]').first.click()

    _frame(top).wait_for_url(f"{portal.url}{portal.prefix}/survey", timeout=15_000)
    expect(base.locator("text=Survey-In").first).to_be_visible(timeout=15_000)
    _wait_connected(top)
    assert top.url == portal.framed("/")


@pytest.mark.e2e
def test_programmatic_navigation_moves_the_frame(
    top: Page, portal: PortalProxy, connected_gps: None
) -> None:
    """``ui.navigate.to`` (the Fixed position card's survey link) moves the
    frame, never the Portal page around it."""
    top.goto(portal.framed("gps-config"))
    _wait_connected(top)
    card = _base(top).locator(".fixed-position-card")
    expect(card).to_be_visible(timeout=15_000)

    card.locator(".fixed-position-survey-link").click()

    frame = _frame(top)
    frame.wait_for_url(f"{portal.url}{portal.prefix}/survey", timeout=15_000)
    _wait_connected(top)
    assert top.url == portal.framed("gps-config")


@pytest.mark.e2e
def test_profile_export_downloads(
    top: Page, portal: PortalProxy, api_base_url: str, connected_gps: None
) -> None:
    name = "e2e-portal-export"
    profile = {
        "name": name,
        "version": 1,
        "hardware": "any",
        "data_link_port": ["UART1"],
        "rtcm_stream": {"matrix": {"1005": {"UART1": True}}},
    }
    resp = httpx.post(f"{api_base_url}/api/profiles", json=profile, timeout=5.0)
    assert resp.status_code in (201, 409), resp.text
    try:
        top.goto(portal.framed("gps-config"))
        _wait_connected(top)
        base = _base(top)
        expect(base.locator(".rtcm-cell-1005-UART1")).to_have_text("✓", timeout=15_000)
        base.locator(".profile-picker").click()
        base.locator(f".profile-option-{name}").click()
        export = base.locator(".profile-export-icon")
        expect(export).to_be_visible(timeout=10_000)

        with top.expect_download(timeout=15_000) as info:
            export.click()

        download = info.value
        assert download.suggested_filename == f"{name}.json"
        assert name in download.path().read_text()
    finally:
        httpx.delete(f"{api_base_url}/api/profiles/{name}", timeout=5.0)


@pytest.mark.e2e
def test_direct_load_uses_the_real_storage(page: Page, base_url: str) -> None:
    """Opened directly (as on ``:8080``) the stand-in stays out of the way."""
    page.goto(f"{base_url}/")
    page.wait_for_function(SOCKET_CONNECTED, timeout=15_000)

    assert page.evaluate(
        "() => window.sessionStorage instanceof Storage"
        " && window.localStorage instanceof Storage"
    )
