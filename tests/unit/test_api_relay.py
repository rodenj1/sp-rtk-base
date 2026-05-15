"""Tests for sp_rtk_base.api.relay — relay control API endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from sp_rtk_base.models.config_models import DestinationProfile, InputProfile
from sp_rtk_base.services.config_service import ConfigService


@dataclass(frozen=True)
class _MockInputStatus:
    connected: bool = True
    source_type: str = "tcp"
    bytes_received: int = 5000
    messages_received: int = 100
    seconds_since_last_data: float = 0.5
    reconnect_attempts: int = 0
    reconnect_successes: int = 0
    connected_since: float | None = 100.0


@dataclass(frozen=True)
class _MockRelayStatus:
    running: bool = True
    uptime_seconds: float | None = 60.0
    input: _MockInputStatus = _MockInputStatus()
    destinations: list[Any] = ()  # type: ignore[assignment]
    active_destination_count: int = 0
    total_destination_count: int = 0
    bytes_received: int = 5000
    chunks_distributed: int = 100
    frames_parsed: int = 50
    no_data_warnings: int = 0


class TestGetRelayStatus:
    """Tests for GET /api/relay/status."""

    def test_status_when_not_running(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
    ) -> None:
        """Returns running=False when engine is not running."""
        mock_relay_service.get_status = AsyncMock(return_value=None)
        resp = api_client_with_services.get("/api/relay/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False

    def test_status_when_running(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
    ) -> None:
        """Returns full status when engine is running."""
        mock_relay_service.get_status = AsyncMock(
            return_value=_MockRelayStatus()
        )
        resp = api_client_with_services.get("/api/relay/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert data["bytes_received"] == 5000


class TestStartRelay:
    """Tests for POST /api/relay/start."""

    def test_start_when_already_running(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
    ) -> None:
        """Returns 409 when relay is already running."""
        mock_relay_service.is_running = True
        resp = api_client_with_services.post("/api/relay/start")
        assert resp.status_code == 409

    def test_start_without_input_config(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
        mock_config_service: ConfigService,
    ) -> None:
        """Returns 400 when no input source is configured."""
        mock_relay_service.is_running = False
        # Config has no input by default
        resp = api_client_with_services.post("/api/relay/start")
        assert resp.status_code == 400
        assert "input source" in resp.json()["message"].lower()

    def test_start_without_destinations(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
        mock_config_service: ConfigService,
    ) -> None:
        """Returns 400 when no destinations are configured."""
        mock_relay_service.is_running = False
        # Add input but no destinations
        mock_config_service.save_input_config(
            InputProfile(source="tcp", config={"host": "127.0.0.1", "port": 5015})
        )
        resp = api_client_with_services.post("/api/relay/start")
        assert resp.status_code == 400
        assert "destinations" in resp.json()["message"].lower()

    def test_start_success(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
        mock_config_service: ConfigService,
        mock_event_bridge: MagicMock,
    ) -> None:
        """Returns 200 and starts relay when properly configured."""
        mock_relay_service.is_running = False
        mock_relay_service.start_relay = AsyncMock()
        mock_event_bridge.is_running = False

        # Configure input and destination
        mock_config_service.save_input_config(
            InputProfile(source="tcp", config={"host": "127.0.0.1", "port": 5015})
        )
        mock_config_service.save_destination(
            DestinationProfile(
                name="test-tcp",
                type="tcp_server",
                config={"port": 9000},
            )
        )

        resp = api_client_with_services.post("/api/relay/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_relay_service.start_relay.assert_called_once()


class TestStopRelay:
    """Tests for POST /api/relay/stop."""

    def test_stop_when_not_running(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
    ) -> None:
        """Returns 409 when relay is not running."""
        mock_relay_service.is_running = False
        resp = api_client_with_services.post("/api/relay/stop")
        assert resp.status_code == 409

    def test_stop_success(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
        mock_event_bridge: MagicMock,
    ) -> None:
        """Returns 200 and stops relay."""
        mock_relay_service.is_running = True
        mock_relay_service.stop_relay = AsyncMock()

        resp = api_client_with_services.post("/api/relay/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_relay_service.stop_relay.assert_called_once()
        mock_event_bridge.stop.assert_called_once()

    def test_stop_failure_returns_500(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
        mock_event_bridge: MagicMock,
    ) -> None:
        """Returns 500 when stop raises an exception."""
        mock_relay_service.is_running = True
        mock_relay_service.stop_relay = AsyncMock(
            side_effect=RuntimeError("Engine crash")
        )

        resp = api_client_with_services.post("/api/relay/stop")
        assert resp.status_code == 500
        assert "Engine crash" in resp.json()["message"]


class TestStartRelayErrorBranches:
    """Additional error branch tests for POST /api/relay/start."""

    def test_start_engine_failure_returns_500(
        self,
        api_client_with_services: TestClient,
        mock_relay_service: MagicMock,
        mock_config_service: ConfigService,
        mock_event_bridge: MagicMock,
    ) -> None:
        """Returns 500 when the relay engine fails to start."""
        mock_relay_service.is_running = False
        mock_relay_service.start_relay = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )
        mock_event_bridge.is_running = False

        mock_config_service.save_input_config(
            InputProfile(source="tcp", config={"host": "127.0.0.1", "port": 5015})
        )
        mock_config_service.save_destination(
            DestinationProfile(
                name="test-tcp",
                type="tcp_server",
                config={"port": 9000},
            )
        )

        resp = api_client_with_services.post("/api/relay/start")
        assert resp.status_code == 500
        assert "Connection refused" in resp.json()["message"]
