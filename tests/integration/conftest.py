# pyright: reportUnknownMemberType=false
"""Shared fixtures for sp-rtk-base integration tests.

Provides a fresh set of real services, a TCP RTCM source simulator,
and a TCP destination test client for each integration test.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.services import get_config_service, get_event_bridge, get_relay_service
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.event_bridge import EventBridge
from sp_rtk_base.services.relay_service import RelayService
from tests.fixtures.tcp_destination_client import TCPDestinationClient
from tests.fixtures.tcp_source_simulator import TCPSourceSimulator


@pytest.fixture()
def tmp_config_dir() -> Generator[Path, None, None]:
    """Provide a temporary directory for config files."""
    with tempfile.TemporaryDirectory(prefix="sp_rtk_base_integ_") as d:
        yield Path(d)


@pytest.fixture()
def services(
    tmp_config_dir: Path,
) -> Generator[tuple[RelayService, ConfigService, EventBridge], None, None]:
    """Create fresh, real service instances backed by a temp config file."""
    relay_svc = RelayService()
    config_svc = ConfigService(config_path=tmp_config_dir / "config.yaml")
    event_bridge = EventBridge()

    # Load config from the (empty) temp file — creates defaults
    config_svc.load_config()

    yield relay_svc, config_svc, event_bridge

    # Cleanup: stop relay if still running
    import asyncio

    if relay_svc.is_running:
        asyncio.get_event_loop().run_until_complete(relay_svc.stop_relay())
    event_bridge.stop()


@pytest.fixture()
def api_client(
    services: tuple[RelayService, ConfigService, EventBridge],
) -> Generator[TestClient, None, None]:
    """TestClient wired to real services (not mocks)."""
    relay_svc, config_svc, event_bridge = services

    app = create_api_app()
    app.dependency_overrides[get_relay_service] = lambda: relay_svc
    app.dependency_overrides[get_config_service] = lambda: config_svc
    app.dependency_overrides[get_event_bridge] = lambda: event_bridge

    with TestClient(app) as client:
        yield client


@pytest.fixture()
def source_sim() -> Generator[TCPSourceSimulator, None, None]:
    """Start a TCP source simulator on an auto-assigned port."""
    with TCPSourceSimulator(port=0, data_rate_bps=2000) as sim:
        yield sim


@pytest.fixture()
def dest_client() -> Generator[TCPDestinationClient, None, None]:
    """Provide a TCP destination client (not yet connected)."""
    client = TCPDestinationClient()
    yield client
    client.disconnect()
