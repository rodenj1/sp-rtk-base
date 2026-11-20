"""Tunnel opens and closes (ticket #92), Wakes and Remote sessions (ticket
#203) in the Event log.

The app reads the Portal agent's status over its control socket (faked
here) and raises an Event log entry for each tunnel open and close, Wake,
and Remote session start and end: in the backfill (``GET /api/events``) and
live (``/api/events/ws``), whether or not the Relay is running.
"""

from __future__ import annotations

import asyncio
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


# --- Wakes and Remote sessions (ticket #203) --------------------------------------------

WAKE = {"id": "k3/wake/1", "event": "wake", "at": "2026-01-01T12:00:20+00:00"}
STARTED = {
    "id": "k3/session/8/started",
    "event": "session_started",
    "at": "2026-01-01T12:00:00+00:00",
    "who": "sam@example.com",
    "shared": True,
}
ENDED = {
    "id": "k3/session/8/ended",
    "event": "session_ended",
    "at": "2026-01-01T12:09:00+00:00",
    "who": "sam@example.com",
    "shared": True,
    "reason": "revoked",
}


def test_wakes_and_remote_sessions_are_in_the_event_log_in_time_order(
    agent: FakeAgentSocket, client: TestClient
) -> None:
    agent.status = _connected(OPENED, CLOSED) | {
        "remote_events": [WAKE, STARTED, ENDED]
    }

    _until(lambda: len(_event_types(client)) == 5)

    events = client.get("/api/events").json()["events"]
    assert [e["event_type"] for e in events] == [
        "portal.remote_session_started",
        "portal.wake",  # before the tunnel open it caused (same time)
        "portal.tunnel_opened",
        "portal.tunnel_closed",
        "portal.remote_session_ended",
    ]
    started, wake, _, _, ended = events
    assert wake["payload"] == {}
    assert wake["timestamp"] == 1767268820.0
    assert started["payload"] == {"who": "sam@example.com", "role": "shared_user"}
    assert started["message"] == "Remote session started: sam@example.com (Shared user)"
    assert ended["payload"] == {
        "who": "sam@example.com",
        "role": "shared_user",
        "reason": "revoked",
    }
    assert "sam@example.com" in ended["message"] and "revoked" in ended["message"]


def test_an_operators_session_and_any_end_reason_read_as_words(
    agent: FakeAgentSocket, client: TestClient
) -> None:
    operator = {"who": "op@example.com", "shared": False}
    agent.status = _connected() | {
        "remote_events": [
            STARTED | operator | {"id": "s"},
            ENDED | operator | {"id": "e1", "reason": "idle"},
            ENDED | operator | {"id": "e2", "reason": "something_new"},
        ]
    }

    _until(lambda: len(_event_types(client)) == 3)

    started, idle, new = client.get("/api/events").json()["events"]
    assert started["message"] == "Remote session started: op@example.com (Operator)"
    assert started["payload"]["role"] == "operator"
    assert "nobody had the Session bar open" in idle["message"]
    assert new["message"].endswith("something_new")


def test_the_live_remote_sessions_are_kept_from_the_last_read(
    agent: FakeAgentSocket, relay: RelayService
) -> None:
    events = PortalEvents(lambda: read_status(agent.path), relay.publish_app_event)
    session = {
        "who": "sam@example.com",
        "since": STARTED["at"],
        "shared": True,
        "id": "8",
    }
    agent.status = _connected() | {"sessions": [session]}

    asyncio.run(events.check())
    [live] = events.live_sessions
    assert (live.who, live.shared, live.id) == ("sam@example.com", True, "8")

    agent.path.unlink()  # the agent went away
    asyncio.run(events.check())
    assert events.live_sessions == []
