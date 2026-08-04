"""Tests for Network API endpoints (issues #22-24)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import AppConfig, DeploymentConfig
from sp_rtk_base.models.net_provision_models import (
    ActiveLink,
    LinkType,
    SavedWifiConnection,
    WifiNetwork,
)
from sp_rtk_base.services import get_config_service, get_network_service
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.network_service import (
    ApFallbackInfo,
    NetworkNotConfiguredError,
    NetworkService,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_network_service() -> NetworkService:
    """Create a mock NetworkService."""
    svc = MagicMock(spec=NetworkService)
    svc.get_active_link = AsyncMock(return_value=None)
    svc.scan_networks = AsyncMock(return_value=[])
    svc.get_ap_fallback_info = AsyncMock(
        return_value=ApFallbackInfo(
            ap_ssid="sp-rtk-base-setup", fallback_window_seconds=300.0
        )
    )
    svc.connect_to_network = AsyncMock(return_value=None)
    svc.list_saved_networks = AsyncMock(return_value=[])
    svc.switch_to_network = AsyncMock(return_value=None)
    svc.forget_network = AsyncMock(return_value=None)
    return svc


def _mock_config_service(mode: str) -> ConfigService:
    """A mock ConfigService pinned to a given deployment mode."""
    svc = MagicMock(spec=ConfigService)
    svc.get_config.return_value = AppConfig(deployment=DeploymentConfig(mode=mode))  # type: ignore[arg-type]
    return svc


@pytest.fixture()
def mock_config_service() -> ConfigService:
    """Deployment mode defaults to 'appliance' — the mode under test for
    every existing test in this file, which predates deployment modes
    (issues #22-24) and exercises the network console's normal behavior."""
    return _mock_config_service("appliance")


@pytest.fixture()
def client(
    mock_network_service: NetworkService, mock_config_service: ConfigService
) -> TestClient:
    """Create a test client with the network + config service dependencies
    overridden."""
    app = create_api_app()
    app.dependency_overrides[get_network_service] = lambda: mock_network_service
    app.dependency_overrides[get_config_service] = lambda: mock_config_service
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/network/status
# ---------------------------------------------------------------------------


class TestGetNetworkStatus:
    def test_not_configured(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.get_active_link = AsyncMock(
            side_effect=NetworkNotConfiguredError("no config file"),
        )
        resp = client.get("/api/network/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is False
        assert data["link"] is None

    def test_configured_no_active_link(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.get_active_link = AsyncMock(return_value=None)
        resp = client.get("/api/network/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["link"] is None

    def test_wired_link(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.get_active_link = AsyncMock(
            return_value=ActiveLink(
                link_type=LinkType.WIRED,
                name="Wired connection 1",
                ip_address="192.168.1.50",
            ),
        )
        resp = client.get("/api/network/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["link"]["link_type"] == "wired"
        assert data["link"]["name"] == "Wired connection 1"
        assert data["link"]["ip_address"] == "192.168.1.50"
        assert data["link"]["signal"] is None

    def test_wifi_link(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.get_active_link = AsyncMock(
            return_value=ActiveLink(
                link_type=LinkType.WIFI,
                name="SiteWiFi",
                ip_address="192.168.1.60",
                signal=77,
            ),
        )
        resp = client.get("/api/network/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["link"]["link_type"] == "wifi"
        assert data["link"]["signal"] == 77

    def test_unexpected_error_returns_502(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.get_active_link = AsyncMock(
            side_effect=RuntimeError("nmcli exploded"),
        )
        resp = client.get("/api/network/status")
        assert resp.status_code == 502
        assert "nmcli exploded" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/network/scan
# ---------------------------------------------------------------------------


class TestScanNetworks:
    def test_returns_scan_results(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.scan_networks = AsyncMock(
            return_value=[
                WifiNetwork(ssid="SiteWiFi", signal=80, security="WPA2"),
                WifiNetwork(ssid="Guest", signal=40, security=""),
            ],
        )
        resp = client.get("/api/network/scan")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["ssid"] == "SiteWiFi"
        assert data[0]["signal"] == 80
        assert data[1]["security"] == ""

    def test_empty_scan(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.scan_networks = AsyncMock(return_value=[])
        resp = client.get("/api/network/scan")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_not_configured_returns_409(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.scan_networks = AsyncMock(
            side_effect=NetworkNotConfiguredError("no config file"),
        )
        resp = client.get("/api/network/scan")
        assert resp.status_code == 409

    def test_unexpected_error_returns_502(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.scan_networks = AsyncMock(
            side_effect=RuntimeError("nmcli exploded"),
        )
        resp = client.get("/api/network/scan")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# GET /api/network/fallback-info
# ---------------------------------------------------------------------------


class TestGetFallbackInfo:
    def test_returns_ap_ssid_and_window(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.get_ap_fallback_info = AsyncMock(
            return_value=ApFallbackInfo(
                ap_ssid="sp-rtk-base-setup", fallback_window_seconds=300.0
            )
        )
        resp = client.get("/api/network/fallback-info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ap_ssid"] == "sp-rtk-base-setup"
        assert data["fallback_window_seconds"] == 300.0

    def test_not_configured_returns_409(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.get_ap_fallback_info = AsyncMock(
            side_effect=NetworkNotConfiguredError("no config file"),
        )
        resp = client.get("/api/network/fallback-info")
        assert resp.status_code == 409

    def test_unexpected_error_returns_502(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.get_ap_fallback_info = AsyncMock(
            side_effect=RuntimeError("nmcli exploded"),
        )
        resp = client.get("/api/network/fallback-info")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /api/network/connect
# ---------------------------------------------------------------------------


class TestConnectNetwork:
    def test_accepts_and_forwards_to_service(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        resp = client.post(
            "/api/network/connect",
            json={"ssid": "SiteWiFi", "password": "hunter22"},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert "SiteWiFi" in data["message"]
        mock_network_service.connect_to_network.assert_awaited_once_with(
            "SiteWiFi", "hunter22", hidden=False
        )

    def test_hidden_network_is_forwarded(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        resp = client.post(
            "/api/network/connect",
            json={"ssid": "HiddenNet", "password": "hunter22", "hidden": True},
        )
        assert resp.status_code == 202
        mock_network_service.connect_to_network.assert_awaited_once_with(
            "HiddenNet", "hunter22", hidden=True
        )

    def test_open_network_defaults_password_to_empty(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        resp = client.post("/api/network/connect", json={"ssid": "OpenNet"})
        assert resp.status_code == 202
        mock_network_service.connect_to_network.assert_awaited_once_with(
            "OpenNet", "", hidden=False
        )

    def test_not_configured_returns_409(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.connect_to_network = AsyncMock(
            side_effect=NetworkNotConfiguredError("no config file"),
        )
        resp = client.post(
            "/api/network/connect",
            json={"ssid": "SiteWiFi", "password": "hunter22"},
        )
        assert resp.status_code == 409

    def test_missing_ssid_is_rejected(self, client: TestClient) -> None:
        resp = client.post("/api/network/connect", json={"password": "hunter22"})
        assert resp.status_code == 422

    def test_unexpected_error_returns_502(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.connect_to_network = AsyncMock(
            side_effect=RuntimeError("nmcli exploded"),
        )
        resp = client.post(
            "/api/network/connect",
            json={"ssid": "SiteWiFi", "password": "hunter22"},
        )
        assert resp.status_code == 502

    def test_allowed_while_on_ethernet(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        """Configuring WiFi while wired (so the cable can later be
        removed) is not gated on the current link type — the endpoint
        doesn't consult status at all."""
        mock_network_service.get_active_link = AsyncMock(
            return_value=ActiveLink(
                link_type=LinkType.WIRED, name="Wired connection 1"
            ),
        )
        resp = client.post(
            "/api/network/connect",
            json={"ssid": "SiteWiFi", "password": "hunter22"},
        )
        assert resp.status_code == 202
        mock_network_service.connect_to_network.assert_awaited_once_with(
            "SiteWiFi", "hunter22", hidden=False
        )


# ---------------------------------------------------------------------------
# GET /api/network/saved
# ---------------------------------------------------------------------------


class TestListSavedNetworks:
    def test_returns_saved_connections(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.list_saved_networks = AsyncMock(
            return_value=[
                SavedWifiConnection(name="SiteWiFi", active=True),
                SavedWifiConnection(name="OldWiFi", active=False),
            ],
        )
        resp = client.get("/api/network/saved")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["name"] == "SiteWiFi"
        assert data[0]["active"] is True
        assert data[1]["active"] is False

    def test_empty_list(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.list_saved_networks = AsyncMock(return_value=[])
        resp = client.get("/api/network/saved")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_not_configured_returns_409(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.list_saved_networks = AsyncMock(
            side_effect=NetworkNotConfiguredError("no config file"),
        )
        resp = client.get("/api/network/saved")
        assert resp.status_code == 409

    def test_unexpected_error_returns_502(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.list_saved_networks = AsyncMock(
            side_effect=RuntimeError("nmcli exploded"),
        )
        resp = client.get("/api/network/saved")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /api/network/saved/{name}/activate
# ---------------------------------------------------------------------------


class TestSwitchNetwork:
    def test_accepts_and_forwards_to_service(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        resp = client.post("/api/network/saved/SiteWiFi/activate")
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert "SiteWiFi" in data["message"]
        mock_network_service.switch_to_network.assert_awaited_once_with("SiteWiFi")

    def test_not_configured_returns_409(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.switch_to_network = AsyncMock(
            side_effect=NetworkNotConfiguredError("no config file"),
        )
        resp = client.post("/api/network/saved/SiteWiFi/activate")
        assert resp.status_code == 409

    def test_unexpected_error_returns_502(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.switch_to_network = AsyncMock(
            side_effect=RuntimeError("nmcli exploded"),
        )
        resp = client.post("/api/network/saved/SiteWiFi/activate")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# DELETE /api/network/saved/{name}
# ---------------------------------------------------------------------------


class TestForgetNetwork:
    def test_accepts_and_forwards_to_service(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        resp = client.delete("/api/network/saved/OldWiFi")
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert "OldWiFi" in data["message"]
        mock_network_service.forget_network.assert_awaited_once_with("OldWiFi")

    def test_forgetting_the_active_network_is_accepted_the_same_way(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        """Forgetting the active network is fire-and-acknowledge like any
        other forget — the endpoint doesn't special-case it, since the
        outcome (session drop or not) isn't knowable synchronously."""
        resp = client.delete("/api/network/saved/SiteWiFi")
        assert resp.status_code == 202
        mock_network_service.forget_network.assert_awaited_once_with("SiteWiFi")

    def test_not_configured_returns_409(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.forget_network = AsyncMock(
            side_effect=NetworkNotConfiguredError("no config file"),
        )
        resp = client.delete("/api/network/saved/SiteWiFi")
        assert resp.status_code == 409

    def test_unexpected_error_returns_502(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.forget_network = AsyncMock(
            side_effect=RuntimeError("nmcli exploded"),
        )
        resp = client.delete("/api/network/saved/SiteWiFi")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# managed-host mode disables the whole /api/network/* surface (issue #28)
# ---------------------------------------------------------------------------


class TestManagedHostModeDisablesNetworkApi:
    """In managed-host mode something else owns the network stack, so
    every /api/network/* route must 404 — not just return an empty/
    disabled-looking result."""

    @pytest.fixture()
    def client(self, mock_network_service: NetworkService) -> TestClient:
        app = create_api_app()
        app.dependency_overrides[get_network_service] = lambda: mock_network_service
        app.dependency_overrides[get_config_service] = lambda: _mock_config_service(
            "managed-host"
        )
        return TestClient(app)

    def test_status_is_404(self, client: TestClient) -> None:
        assert client.get("/api/network/status").status_code == 404

    def test_scan_is_404(self, client: TestClient) -> None:
        assert client.get("/api/network/scan").status_code == 404

    def test_fallback_info_is_404(self, client: TestClient) -> None:
        assert client.get("/api/network/fallback-info").status_code == 404

    def test_connect_is_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/network/connect", json={"ssid": "SiteWiFi", "password": "x"}
        )
        assert resp.status_code == 404

    def test_saved_list_is_404(self, client: TestClient) -> None:
        assert client.get("/api/network/saved").status_code == 404

    def test_activate_is_404(self, client: TestClient) -> None:
        resp = client.post("/api/network/saved/SiteWiFi/activate")
        assert resp.status_code == 404

    def test_forget_is_404(self, client: TestClient) -> None:
        assert client.delete("/api/network/saved/SiteWiFi").status_code == 404

    def test_network_service_is_never_reached(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        """The 404 gate must short-circuit before the network service is
        touched at all."""
        client.get("/api/network/status")
        mock_network_service.get_active_link.assert_not_called()
