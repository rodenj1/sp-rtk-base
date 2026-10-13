"""Tunnel opens and closes in the Event log (ticket #92).

The app reads the Portal agent's status over its control socket (faked
here) and raises an Event log entry for each tunnel open and close: in the
backfill (``GET /api/events``) and live (``/api/events/ws``), whether or
not the Relay is running.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import AppConfig
from sp_rtk_base.portal_agent.client import read_status
from sp_rtk_base.services import get_relay_service
from sp_rtk_base.services.portal_events import PortalEvents
from sp_rtk_base.services.relay_service import RelayService
from tests.fixtures.fake_agent_socket import FakeAgentSocket

OPENED = {"id": "a1", "event": "opened", "at": "2026-01-01T12:00:20+00:00"}
CLOSED = {"id": "a2", "event": "closed", "at": "2026-01-01T12:05:00+00:00"}


def _connected(*events: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": "connected",
        "base_id": "k3mnp4qrst",
        "account": "op@example.com",
        "tunnel_events": list(events),
    }


@pytest.fixture()
def agent() -> Iterator[FakeAgentSocket]:
    with tempfile.TemporaryDirectory(prefix="agent-") as d:
        with FakeAgentSocket(Path(d) / "control.sock") as fake:
            yield fake


@pytest.fixture()
def relay() -> RelayService:
    return RelayService(AppConfig)  # never started


@pytest.fixture()
def client(agent: FakeAgentSocket, relay: RelayService) -> Iterator[TestClient]:
    app = create_api_app()
    app.dependency_overrides[get_relay_service] = lambda: relay
    events = PortalEvents(
        lambda: read_status(agent.path), relay.publish_app_event, interval=0.05
    )
    with TestClient(app) as c:
        assert c.portal is not None
        c.portal.call(events.start)
        try:
            yield c
        finally:
            c.portal.call(events.stop)


def _until(check: Any, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(0.02)
    raise AssertionError("never happened")


def _event_types(client: TestClient) -> list[str]:
    return [e["event_type"] for e in client.get("/api/events").json()["events"]]


def test_tunnel_opens_and_closes_are_in_the_backfill_with_the_relay_stopped(
    agent: FakeAgentSocket, client: TestClient
) -> None:
    agent.status = _connected(OPENED, CLOSED)

    _until(lambda: len(_event_types(client)) == 2)

    opened, closed = client.get("/api/events").json()["events"]
    assert opened["event_type"] == "portal.tunnel_opened"
    assert "opened" in opened["message"]
    assert opened["timestamp"] == 1767268820.0
    assert closed["event_type"] == "portal.tunnel_closed"


def test_each_event_is_raised_once_however_often_the_status_is_read(
    agent: FakeAgentSocket, client: TestClient
) -> None:
    agent.status = _connected(OPENED)
    _until(lambda: _event_types(client))
    agent.status = _connected(OPENED, CLOSED)

    _until(lambda: len(_event_types(client)) == 2)
    time.sleep(0.2)

    assert _event_types(client) == ["portal.tunnel_opened", "portal.tunnel_closed"]


def test_a_tunnel_open_is_streamed_live(
    agent: FakeAgentSocket, client: TestClient
) -> None:
    with client.websocket_connect("/api/events/ws") as ws:
        agent.status = _connected(OPENED)

        event = ws.receive_json()

    assert event["event_type"] == "portal.tunnel_opened"


def test_no_agent_means_no_events(client: TestClient, agent: FakeAgentSocket) -> None:
    agent.path.unlink()  # as if the agent isn't running
    time.sleep(0.2)

    assert _event_types(client) == []
