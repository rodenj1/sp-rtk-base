"""End-to-end checks for the single Start/Stop toggle button (v0.3.22).

The Dashboard previously rendered two buttons (Start + Stop) with the
inactive one greyed out.  v0.3.22 collapsed them into one toggle
whose text/icon/color swap with the relay's running state, and which
is **disabled** when the saved config can't satisfy the engine's
preconditions (no input source, or zero enabled destinations).

The pure decision logic lives in ``_compute_relay_control_state`` and
is exhaustively unit-tested in ``tests/unit/test_dashboard_formatters.py``;
the tests here drive the actual NiceGUI render path through a real
browser to catch regressions in how the button widget itself is wired.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect


def _put_tcp_input(api_base_url: str) -> None:
    """Configure a known TCP input source so the precondition is satisfied."""
    resp = httpx.put(
        f"{api_base_url}/api/input",
        json={
            "source": "tcp",
            "config": {"host": "127.0.0.1", "port": 5099},
        },
        timeout=5.0,
    )
    assert resp.status_code in (200, 201), resp.text


def _post_enabled_tcp_destination(api_base_url: str, name: str, port: int) -> None:
    """Add a single enabled TCP-server destination via API."""
    resp = httpx.post(
        f"{api_base_url}/api/destinations",
        json={
            "name": name,
            "type": "tcp_server",
            "enabled": True,
            "filter": {"mode": "pass_all", "message_ids": []},
            "config": {"host": "0.0.0.0", "port": port, "max_clients": 5},
        },
        timeout=5.0,
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.e2e
def test_dashboard_renders_single_primary_button(
    page: Page,
    base_url: str,
    api_base_url: str,
    clean_config: None,
) -> None:
    """With a complete config and stopped relay, only one button is rendered.

    Specifically: a "Start" button is visible; **no** "Stop" button
    exists in the DOM (regression guard against the old two-button bar
    where Stop was always present, just greyed out).
    """
    _put_tcp_input(api_base_url)
    _post_enabled_tcp_destination(api_base_url, "e2e-dash-btn-dest", 5097)

    page.goto(f"{base_url}/")
    expect(page.locator("text=Dashboard").first).to_be_visible(timeout=15_000)

    # Wait for the first status poll to land so the button has
    # rendered its computed text — the page initialises with "Start"
    # but the assertion below is more meaningful once the refresh
    # tick has run at least once.
    expect(page.get_by_role("button", name="Start")).to_be_visible(timeout=15_000)

    # The critical regression check: no "Stop" button anywhere in the
    # rendered DOM when the relay isn't running.  Old layout had it
    # permanently mounted (just greyed out).
    expect(page.get_by_role("button", name="Stop")).to_have_count(0)

    # And the Start button is actually clickable (not the disabled
    # variant).  Playwright's ``to_be_enabled`` checks aria-disabled
    # and the underlying input state.
    expect(page.get_by_role("button", name="Start")).to_be_enabled()


@pytest.mark.e2e
def test_dashboard_button_disabled_without_enabled_destinations(
    page: Page,
    base_url: str,
    api_base_url: str,
    clean_config: None,
) -> None:
    """Input set + zero enabled destinations → button disabled + helper text.

    ``clean_config`` wipes all destinations between tests, so after
    configuring just the input we should land in the "needs at least
    one destination" branch of ``_compute_relay_control_state``.
    """
    _put_tcp_input(api_base_url)
    # Deliberately do NOT add a destination.

    page.goto(f"{base_url}/")
    expect(page.locator("text=Dashboard").first).to_be_visible(timeout=15_000)

    # Wait for the refresh tick to render the button + the disabled
    # state.  We can't simply ``expect(...).to_be_disabled()`` on a
    # static locator because the page mounts the button enabled by
    # default — wait for the precondition-failed label to appear,
    # which only the refresh tick produces.
    expect(page.locator("text=Outputs page").first).to_be_visible(timeout=15_000)

    start_btn = page.get_by_role("button", name="Start")
    expect(start_btn).to_be_visible()
    expect(start_btn).to_be_disabled()

    # No second button.
    expect(page.get_by_role("button", name="Stop")).to_have_count(0)
