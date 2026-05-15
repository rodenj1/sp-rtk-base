"""Shared test fixtures for SP-Base test suite."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.services import (
    get_config_service,
    get_event_bridge,
    get_metrics_service,
    get_relay_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.event_bridge import EventBridge
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.relay_service import RelayService


@pytest.fixture()
def api_client() -> TestClient:
    """Create a FastAPI TestClient for API endpoint tests.

    Returns:
        TestClient instance configured with the SP-Base API app.
    """
    app = create_api_app()
    return TestClient(app)


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    """Provide a temp config file path for tests."""
    return tmp_path / "sp-rtk-base" / "config.yaml"


@pytest.fixture()
def mock_config_service(config_path: Path) -> ConfigService:
    """Create a ConfigService with a temp config path."""
    return ConfigService(config_path=config_path)


@pytest.fixture()
def mock_relay_service() -> MagicMock:
    """Create a mock RelayService."""
    mock = MagicMock(spec=RelayService)
    mock.is_running = False
    mock.engine = None
    mock.get_destination_names.return_value = []
    mock.get_recent_events.return_value = []
    mock.subscribe_events.return_value = None
    return mock


@pytest.fixture()
def mock_event_bridge() -> MagicMock:
    """Create a mock EventBridge."""
    mock = MagicMock(spec=EventBridge)
    mock.is_running = False
    return mock


@pytest.fixture()
def mock_metrics_service() -> MetricsService:
    """Create a MetricsService with a fresh registry for testing."""
    return MetricsService()


@pytest.fixture()
def api_client_with_services(
    mock_config_service: ConfigService,
    mock_relay_service: MagicMock,
    mock_event_bridge: MagicMock,
    mock_metrics_service: MetricsService,
) -> TestClient:
    """Create a TestClient with overridden service dependencies.

    Returns:
        TestClient with mock services injected.
    """
    app = create_api_app()

    app.dependency_overrides[get_config_service] = lambda: mock_config_service
    app.dependency_overrides[get_relay_service] = lambda: mock_relay_service
    app.dependency_overrides[get_event_bridge] = lambda: mock_event_bridge
    app.dependency_overrides[get_metrics_service] = lambda: mock_metrics_service

    return TestClient(app)
