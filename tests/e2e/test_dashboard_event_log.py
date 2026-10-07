"""End-to-end checks for the Dashboard Event log's live rows (issue #51).

The Event log streams relay events over ``/api/events/ws``.  Here
Playwright stands in for that WebSocket, so a test decides exactly which
events the Dashboard's own script receives and renders.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

import pytest
from playwright.sync_api import Locator, Page, WebSocketRoute, expect


def _serve_events(*events: Mapping[str, object]) -> Callable[[WebSocketRoute], None]:
    """A fake ``/api/events/ws`` that sends ``events`` once the page connects."""

    def serve(ws: WebSocketRoute) -> None:
        for event in events:
            ws.send(json.dumps({"timestamp": 1.0, "payload": {}, **event}))

    return serve


def _event_log(page: Page) -> Locator:
    return page.get_by_test_id("event-log")


@pytest.mark.e2e
def test_live_event_text_is_shown_as_text_not_markup(
    page: Page, base_url: str, clean_config: None
) -> None:
    """Markup in an event (a Bluetooth name, a caster's reply) is just text."""
    hostile = '<img src=x onerror="window.__eventLogInjected = true">'
    page.route_web_socket(
        "**/api/events/ws",
        _serve_events({"event_type": "<b>input.error</b>", "message": hostile}),
    )
    page.goto("/")

    log = _event_log(page)
    expect(log.get_by_text(hostile, exact=True)).to_be_visible()
    expect(log.get_by_text("<b>input.error</b>", exact=True)).to_be_visible()
    expect(log.locator("img, b")).to_have_count(0)
    assert page.evaluate("window.__eventLogInjected") is None


@pytest.mark.e2e
def test_live_events_keep_their_badge_colours_order_and_cap(
    page: Page, base_url: str, clean_config: None
) -> None:
    """Newest first, coloured by type, never more than 50 rows."""
    page.route_web_socket(
        "**/api/events/ws",
        _serve_events(
            *({"event_type": "hub.tick", "message": f"tick {n}"} for n in range(55)),
            {"event_type": "input.connected", "message": "connected"},
            {"event_type": "relay.warning", "message": "warned"},
            {"event_type": "input.error", "message": "failed"},
        ),
    )
    page.goto("/")

    log = _event_log(page)
    expect(log.get_by_text("failed", exact=True)).to_be_visible()
    rows = log.locator(":scope > div")
    expect(rows).to_have_count(50)
    expect(rows.first).to_contain_text("input.error")
    expect(rows.first).to_contain_text("failed")
    for event_type, colour in [
        ("input.error", "rgb(244, 67, 54)"),
        ("relay.warning", "rgb(255, 152, 0)"),
        ("input.connected", "rgb(76, 175, 80)"),
        ("hub.tick", "rgb(96, 125, 139)"),
    ]:
        badge = log.get_by_text(event_type, exact=True).first
        expect(badge).to_have_css("color", colour)
        expect(badge).to_have_css("border-color", colour)
