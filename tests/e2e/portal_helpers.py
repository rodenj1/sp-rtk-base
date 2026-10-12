"""Shared steps for the Portal prefix and sandboxed-frame e2e tests."""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
from playwright.sync_api import Page, WebSocket

#: The live event a Relay start emits once its TCP input connects.
RELAY_EVENT_TEXT = "input.connected"


def open_event_log(page: Page, navigate: Callable[[], object]) -> WebSocket:
    """Run ``navigate`` and wait until the Dashboard's live Event log stream
    is open: its first keepalive has arrived through whatever proxy is in
    front, so an event sent from now on reaches the page."""
    with page.expect_websocket(
        lambda ws: ws.url.endswith("/api/events/ws"), timeout=20_000
    ) as info:
        navigate()
    ws = info.value
    ws.wait_for_event("framereceived", timeout=20_000)
    return ws


@contextmanager
def relay_started_event(api_base_url: str) -> Iterator[str]:
    """Start the Relay from a TCP input (a listener here); yields the text of
    a live event that start sends to every open Event log."""
    source = socket.create_server(("127.0.0.1", 0))
    port = source.getsockname()[1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        out_port = probe.getsockname()[1]
    resp = httpx.put(
        f"{api_base_url}/api/input",
        json={"source": "tcp", "config": {"host": "127.0.0.1", "port": port}},
        timeout=5.0,
    )
    assert resp.status_code in (200, 201), resp.text
    resp = httpx.post(
        f"{api_base_url}/api/destinations",
        json={
            "name": "e2e-portal-dest",
            "type": "tcp_server",
            "enabled": True,
            "filter": {"mode": "pass_all", "message_ids": []},
            "config": {"host": "127.0.0.1", "port": out_port, "max_clients": 1},
        },
        timeout=5.0,
    )
    assert resp.status_code == 201, resp.text
    try:
        resp = httpx.post(f"{api_base_url}/api/relay/start", timeout=15.0)
        assert resp.status_code == 200, resp.text
        yield RELAY_EVENT_TEXT
    finally:
        httpx.post(f"{api_base_url}/api/relay/stop", timeout=15.0)
        source.close()
