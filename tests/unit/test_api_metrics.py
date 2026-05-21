"""Unit tests for the /metrics API endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from sp_rtk_base_relay.core.status import (
    DestinationStatus,
    InputStatus,
    RelayStatus,
)

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import AppSettings
from sp_rtk_base.services import (
    get_config_service,
    get_metrics_service,
    get_relay_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.relay_service import RelayService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_relay_status() -> RelayStatus:
    return RelayStatus(
        running=True,
        uptime_seconds=60.0,
        input=InputStatus(
            connected=True,
            source_type="TCP",
            bytes_received=5000,
            messages_received=100,
            seconds_since_last_data=0.3,
            reconnect_attempts=1,
            reconnect_successes=1,
            connected_since=1000.0,
        ),
        destinations=[
            DestinationStatus(
                name="test-dest",
                destination_type="tcp_server",
                enabled=True,
                running=True,
                connected=True,
                filter_mode="pass_all",
                bytes_sent=2000,
                messages_sent=50,
                messages_dropped=0,
                messages_filtered=0,
                errors=0,
                last_error=None,
                queue_depth=2,
                connected_since=1000.0,
                uptime_seconds=55.0,
                connection_attempts=1,
                successful_connections=1,
            ),
        ],
        active_destination_count=1,
        total_destination_count=1,
        bytes_received=5000,
        chunks_distributed=100,
        frames_parsed=20,
        no_data_warnings=0,
    )


def _create_client(
    relay_mock: MagicMock,
    metrics_svc: MetricsService,
    config_svc: MagicMock | ConfigService | None = None,
) -> TestClient:
    """Create a TestClient with injected mocks."""
    app = create_api_app()
    app.dependency_overrides[get_relay_service] = lambda: relay_mock
    app.dependency_overrides[get_metrics_service] = lambda: metrics_svc
    if config_svc is not None:
        app.dependency_overrides[get_config_service] = lambda: config_svc
    else:
        # Default: metrics enabled
        mock_cfg = MagicMock(spec=ConfigService)
        mock_cfg.get_settings.return_value = AppSettings(metrics_enabled=True)
        app.dependency_overrides[get_config_service] = lambda: mock_cfg
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests: /metrics endpoint
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    """Tests for GET /metrics."""

    def test_returns_200(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_content_type_prometheus(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        assert "text/plain" in resp.headers["content-type"]

    def test_idle_when_relay_stopped(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        body = resp.text

        assert "sp_rtk_base_relay_running 0.0" in body
        assert "sp_rtk_base_relay_uptime_seconds 0.0" in body
        assert "sp_rtk_base_input_connected 0.0" in body

    def test_metrics_populated_when_running(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = True
        relay.get_status = AsyncMock(return_value=_make_relay_status())
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        body = resp.text

        assert "sp_rtk_base_relay_running 1.0" in body
        assert "sp_rtk_base_relay_uptime_seconds 60.0" in body
        assert "sp_rtk_base_input_connected 1.0" in body
        assert "sp_rtk_base_input_bytes_received 5000.0" in body
        assert "sp_rtk_base_active_destinations 1.0" in body
        assert "sp_rtk_base_chunks_distributed 100.0" in body

    def test_per_destination_labels(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = True
        relay.get_status = AsyncMock(return_value=_make_relay_status())
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        body = resp.text

        assert 'sp_rtk_base_dest_connected{destination="test-dest"} 1.0' in body
        assert 'sp_rtk_base_dest_bytes_sent{destination="test-dest"} 2000.0' in body

    def test_idle_when_status_returns_none(self) -> None:
        """When relay is 'running' but get_status returns None."""
        relay = MagicMock(spec=RelayService)
        relay.is_running = True
        relay.get_status = AsyncMock(return_value=None)
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        body = resp.text

        assert "sp_rtk_base_relay_running 0.0" in body

    def test_contains_help_text(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        client = _create_client(relay, MetricsService())

        resp = client.get("/metrics")
        body = resp.text

        assert "# HELP sp_rtk_base_relay_running" in body
        assert "# TYPE sp_rtk_base_relay_running gauge" in body


class TestMetricsDisabled:
    """Tests for GET /metrics when metrics_enabled=False."""

    def test_returns_404_when_disabled(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        cfg = MagicMock(spec=ConfigService)
        cfg.get_settings.return_value = AppSettings(metrics_enabled=False)
        client = _create_client(relay, MetricsService(), config_svc=cfg)

        resp = client.get("/metrics")
        assert resp.status_code == 404

    def test_disabled_returns_json_detail(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        cfg = MagicMock(spec=ConfigService)
        cfg.get_settings.return_value = AppSettings(metrics_enabled=False)
        client = _create_client(relay, MetricsService(), config_svc=cfg)

        resp = client.get("/metrics")
        assert resp.json()["detail"] == "Metrics are disabled"

    def test_enabled_returns_200(self) -> None:
        relay = MagicMock(spec=RelayService)
        relay.is_running = False
        cfg = MagicMock(spec=ConfigService)
        cfg.get_settings.return_value = AppSettings(metrics_enabled=True)
        client = _create_client(relay, MetricsService(), config_svc=cfg)

        resp = client.get("/metrics")
        assert resp.status_code == 200
