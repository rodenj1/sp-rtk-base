"""Tests for sp_rtk_base.api.events — events REST endpoint and WebSocket."""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sp_rtk_base_relay.config import InputConfig

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import AppConfig
from sp_rtk_base.services import get_relay_service
from sp_rtk_base.services.relay_service import RelayService


class TestGetRecentEvents:
    """Tests for GET /api/events."""

    def test_empty_events(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
    ) -> None:
        """Returns empty list when no events."""
        mock_relay_service.get_recent_events.return_value = []
        resp = api_client_with_services.get("/api/events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["events"] == []

    def test_returns_events(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
    ) -> None:
        """Returns events from relay service."""
        mock_relay_service.get_recent_events.return_value = [
            {
                "event_type": "engine.started",
                "message": "Engine started",
                "timestamp": 100.0,
                "payload": {"destination_count": 1},
            },
            {
                "event_type": "destination.connected",
                "message": "Connected to rtk2go",
                "timestamp": 101.0,
                "payload": {"name": "rtk2go"},
            },
        ]
        resp = api_client_with_services.get("/api/events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert data["events"][0]["event_type"] == "engine.started"
        assert data["events"][1]["event_type"] == "destination.connected"

    def test_count_parameter(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
    ) -> None:
        """Passes count parameter to service."""
        mock_relay_service.get_recent_events.return_value = []
        api_client_with_services.get("/api/events?count=10")
        mock_relay_service.get_recent_events.assert_called_once_with(10)


class FakeStream:
    """Stands in for ``RelayService.stream_events()`` at the endpoint."""

    def __init__(self, *events: dict[str, Any]) -> None:
        self._events = list(events)
        self.closed = False

    async def get(self) -> dict[str, Any]:
        if self._events:
            return self._events.pop(0)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    def close(self) -> None:
        self.closed = True


class TestWebSocketEvents:
    """Tests for WS /api/events/ws WebSocket endpoint."""

    def test_websocket_receives_event(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
    ) -> None:
        """Each event on the client's stream goes out as one JSON message."""
        mock_relay_service.stream_events.return_value = FakeStream(
            {
                "event_type": "engine.started",
                "message": "Engine started",
                "timestamp": 100.0,
                "payload": {},
            }
        )

        with api_client_with_services.websocket_connect("/api/events/ws") as ws:
            data = ws.receive_json()
            assert data["event_type"] == "engine.started"
            assert data["message"] == "Engine started"

    def test_websocket_closes_its_stream_when_the_client_leaves(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
    ) -> None:
        """A client that disappears releases its stream, without crashing.

        v0.3.29: idle WebSocket connections produced RuntimeError
        tracebacks when the keepalive ping fired after the client was
        already gone; the handler checks ``WebSocketState`` and catches
        the RuntimeError from ``send_json`` so departure is a clean exit.
        """
        stream = FakeStream(
            {
                "event_type": "test.event",
                "message": "Test",
                "timestamp": 1.0,
                "payload": {},
            }
        )
        mock_relay_service.stream_events.return_value = stream

        with api_client_with_services.websocket_connect("/api/events/ws") as ws:
            ws.receive_json()
            ws.close()
        deadline = time.monotonic() + 5.0
        while not stream.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert stream.closed

    def test_websocket_streams_a_relay_started_outside_the_api(self) -> None:
        """Issue #49: a Dashboard-style start reaches an open Event log.

        Real ``RelayService`` and ``RelayEngine``; the input is a TCP
        input on a local listener.
        """
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        relay = RelayService(AppConfig)
        app = create_api_app()
        app.dependency_overrides[get_relay_service] = lambda: relay
        try:
            with (
                TestClient(app) as client,
                client.websocket_connect("/api/events/ws") as ws,
            ):
                portal = client.portal
                assert portal is not None
                portal.call(
                    relay.start_relay,
                    InputConfig(
                        source="tcp", config={"host": "127.0.0.1", "port": port}
                    ),
                    None,
                    "ui",
                )
                seen: list[str] = []
                while "engine.started" not in seen:
                    message = ws.receive_json()
                    seen.append(message.get("event_type", message.get("type")))
                portal.call(relay.stop_relay, "test")
        finally:
            srv.close()
