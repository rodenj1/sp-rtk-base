"""Tests for Network API endpoints (issues #22-24)."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import AppConfig, DeploymentConfig
from sp_rtk_base.models.net_provision_models import (
    BandChangeSaved,
    NetProvisionConfig,
    SavedWifiConnection,
    WifiBand,
    WifiNetwork,
)
from sp_rtk_base.models.network_link_models import (
    AddressReading,
    Counters,
    EgressReading,
    HostReading,
    InterfaceReading,
    StationReading,
    WifiReading,
)
from sp_rtk_base.services import (
    get_config_service,
    get_network_link_service,
    get_network_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.net_provision.nmcli_adapter import NmcliAdapter
from sp_rtk_base.services.network_link.service import NetworkLinkService
from sp_rtk_base.services.network_service import (
    ApFallbackInfo,
    NetworkNotConfiguredError,
    NetworkService,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ETH0 = InterfaceReading(
    name="eth0",
    kind="ethernet",
    operstate="up",
    carrier=True,
    speed_mbps=100,
    full_duplex=True,
    counters=Counters(rx_bytes=5000),
)
WLAN0 = InterfaceReading(name="wlan0", kind="wifi", operstate="up")
ADDRESSES = (
    AddressReading(interface="eth0", address="192.0.2.10", prefixlen=24),
    AddressReading(interface="wlan0", address="192.0.2.20", prefixlen=24),
    AddressReading(interface="wg0", address="10.8.0.2", prefixlen=24),
)


def _associated(signal_dbm: int) -> WifiReading:
    return WifiReading(
        interface="wlan0",
        mode="station",
        wiphy=0,
        frequency_mhz=5745,
        ssid="SiteWiFi",
        station=StationReading(bssid="02:00:00:00:00:01", signal_dbm=signal_dbm),
    )


def _reading(
    *interfaces: InterfaceReading,
    source: str | None = "192.0.2.10",
    wifi: tuple[WifiReading, ...] | None = (),
) -> HostReading:
    return HostReading(
        interfaces=interfaces,
        addresses=ADDRESSES,
        egress=EgressReading(source=source),
        wifi=wifi,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeHost:
    """Serves one reading per sample."""

    def __init__(self, *readings: HostReading) -> None:
        self.readings = list(readings)

    def read(self) -> HostReading:
        return self.readings.pop(0)

    def close(self) -> None:
        pass


async def _sampled(*readings: HostReading) -> NetworkLinkService:
    """The real Network link sampler after one sample per reading, 2 s apart."""
    clock = _Clock()
    sampler = NetworkLinkService(_FakeHost(*readings), clock=clock)
    for _ in readings:
        clock.now += 2.0
        await sampler.sample_once()
    return sampler


def _status(
    network: NetworkService, config: ConfigService, sampler: NetworkLinkService
) -> dict[str, Any]:
    """GET /api/network/status against *sampler*'s snapshot."""
    app = create_api_app()
    app.dependency_overrides[get_network_service] = lambda: network
    app.dependency_overrides[get_config_service] = lambda: config
    app.dependency_overrides[get_network_link_service] = lambda: sampler
    resp = TestClient(app).get("/api/network/status")
    assert resp.status_code == 200
    data: dict[str, Any] = resp.json()
    return data


@pytest.fixture()
def mock_network_service() -> NetworkService:
    """Create a mock NetworkService."""
    svc = MagicMock(spec=NetworkService)
    svc.is_configured = AsyncMock(return_value=True)
    svc.scan_networks = AsyncMock(return_value=[])
    svc.has_5ghz = AsyncMock(return_value=True)
    svc.get_ap_fallback_info = AsyncMock(
        return_value=ApFallbackInfo(
            ap_ssid="sp-rtk-base-setup", fallback_window_seconds=300.0
        )
    )
    svc.connect_to_network = AsyncMock(return_value=None)
    svc.list_saved_networks = AsyncMock(return_value=[])
    svc.switch_to_network = AsyncMock(return_value=None)
    svc.forget_network = AsyncMock(return_value=None)
    svc.change_band = AsyncMock(
        return_value=BandChangeSaved(name="Barn-AP", band=WifiBand.GHZ_2_4)
    )
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
    """The active-link block reads the Network link snapshot, never nmcli."""

    @pytest.mark.asyncio()
    async def test_an_ethernet_uplink_is_reported_with_its_address(
        self, mock_network_service: NetworkService, mock_config_service: ConfigService
    ) -> None:
        sampler = await _sampled(_reading(ETH0))
        data = _status(mock_network_service, mock_config_service, sampler)
        assert data["uplink"] == {
            "state": "Uplink",
            "interface": "eth0",
            "kind": "ethernet",
            "ipv4": "192.0.2.10/24",
            "wifi_network": None,
            "wifi_link_strength": None,
            "wifi_link_strength_dbm": None,
        }

    @pytest.mark.asyncio()
    async def test_a_wifi_uplink_carries_its_link_strength_and_dbm(
        self, mock_network_service: NetworkService, mock_config_service: ConfigService
    ) -> None:
        sampler = await _sampled(
            _reading(WLAN0, source="192.0.2.20", wifi=(_associated(-71),))
        )
        data = _status(mock_network_service, mock_config_service, sampler)
        assert data["uplink"] == {
            "state": "Uplink",
            "interface": "wlan0",
            "kind": "wifi",
            "ipv4": "192.0.2.20/24",
            "wifi_network": "SiteWiFi",
            "wifi_link_strength": "Marginal",
            "wifi_link_strength_dbm": -71,
        }

    @pytest.mark.asyncio()
    async def test_a_vpn_uplink_is_not_a_network_link(
        self, mock_network_service: NetworkService, mock_config_service: ConfigService
    ) -> None:
        sampler = await _sampled(_reading(ETH0, source="10.8.0.2"))
        uplink = _status(mock_network_service, mock_config_service, sampler)["uplink"]
        assert (uplink["state"], uplink["interface"], uplink["kind"]) == (
            "Uplink",
            "wg0",
            None,
        )

    @pytest.mark.asyncio()
    async def test_no_uplink_when_the_network_is_unreachable(
        self, mock_network_service: NetworkService, mock_config_service: ConfigService
    ) -> None:
        sampler = await _sampled(_reading(ETH0, source=None))
        uplink = _status(mock_network_service, mock_config_service, sampler)["uplink"]
        assert uplink["state"] == "No Uplink"
        assert uplink["interface"] is None

    @pytest.mark.asyncio()
    async def test_no_reading_before_the_first_sample(
        self, mock_network_service: NetworkService, mock_config_service: ConfigService
    ) -> None:
        sampler = await _sampled()
        uplink = _status(mock_network_service, mock_config_service, sampler)["uplink"]
        assert uplink["state"] == "No reading"
        assert uplink["interface"] is None

    @pytest.mark.asyncio()
    async def test_a_wifi_uplink_without_a_current_reading_has_no_strength(
        self, mock_network_service: NetworkService, mock_config_service: ConfigService
    ) -> None:
        """nl80211 failed: the Uplink still shows, its strength never stale."""
        sampler = await _sampled(
            _reading(WLAN0, source="192.0.2.20", wifi=(_associated(-55),)),
            _reading(WLAN0, source="192.0.2.20", wifi=None),
        )
        uplink = _status(mock_network_service, mock_config_service, sampler)["uplink"]
        assert uplink["interface"] == "wlan0"
        assert uplink["wifi_link_strength"] is None
        assert uplink["wifi_link_strength_dbm"] is None

    @pytest.mark.asyncio()
    async def test_the_uplink_shows_without_net_provisioning_config(
        self, mock_network_service: MagicMock, mock_config_service: ConfigService
    ) -> None:
        """The Uplink needs no net-provisioning config, so it agrees with
        the dashboard even on a box that was never provisioned."""
        mock_network_service.is_configured = AsyncMock(return_value=False)
        sampler = await _sampled(_reading(ETH0))
        data = _status(mock_network_service, mock_config_service, sampler)
        assert data["configured"] is False
        assert data["uplink"]["interface"] == "eth0"

    @pytest.mark.asyncio()
    async def test_the_status_runs_no_nmcli_and_so_no_wifi_scan(
        self, mock_config_service: ConfigService
    ) -> None:
        nmcli_calls: list[list[str]] = []

        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            nmcli_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        network = NetworkService(
            config_loader=lambda: NetProvisionConfig(ap_password="sticker-secret"),
            adapter_factory=lambda config: NmcliAdapter(config, runner=runner),
        )
        sampler = await _sampled(
            _reading(WLAN0, source="192.0.2.20", wifi=(_associated(-55),))
        )
        data = _status(network, mock_config_service, sampler)
        assert data["configured"] is True
        assert data["uplink"]["wifi_link_strength"] == "Good"
        assert nmcli_calls == []


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

    def test_returns_the_signal_on_each_band(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.scan_networks = AsyncMock(
            return_value=[
                WifiNetwork(
                    ssid="FarmShop",
                    signal=82,
                    security="WPA2",
                    band_signals={WifiBand.GHZ_2_4: 82, WifiBand.GHZ_5: 41},
                ),
                WifiNetwork(
                    ssid="Neighbour-5G",
                    signal=55,
                    security="WPA2",
                    band_signals={WifiBand.GHZ_5: 55},
                ),
            ],
        )
        resp = client.get("/api/network/scan")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["band_signals"] == {"2.4ghz": 82, "5ghz": 41}
        assert data[1]["band_signals"] == {"5ghz": 55}

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
# GET /api/network/capabilities
# ---------------------------------------------------------------------------


class TestGetCapabilities:
    @pytest.mark.parametrize("answer", [True, False, None])
    def test_reports_whether_the_adapter_has_5ghz(
        self,
        client: TestClient,
        mock_network_service: MagicMock,
        answer: bool | None,
    ) -> None:
        mock_network_service.has_5ghz = AsyncMock(return_value=answer)
        resp = client.get("/api/network/capabilities")
        assert resp.status_code == 200
        assert resp.json() == {"has_5ghz": answer}

    def test_not_configured_returns_409(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.has_5ghz = AsyncMock(
            side_effect=NetworkNotConfiguredError("no config file"),
        )
        assert client.get("/api/network/capabilities").status_code == 409

    def test_unexpected_error_returns_502(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.has_5ghz = AsyncMock(
            side_effect=RuntimeError("nmcli exploded"),
        )
        assert client.get("/api/network/capabilities").status_code == 502


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

    def test_each_saved_network_carries_its_band(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.list_saved_networks = AsyncMock(
            return_value=[
                SavedWifiConnection(name="FarmShop", active=True),
                SavedWifiConnection(
                    name="Barn-AP", active=False, band=WifiBand.GHZ_2_4
                ),
                SavedWifiConnection(
                    name="Office-5G", active=False, band=WifiBand.GHZ_5
                ),
            ],
        )
        resp = client.get("/api/network/saved")
        assert [c["band"] for c in resp.json()] == ["auto", "2.4ghz", "5ghz"]

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
# PUT /api/network/saved/{name}/band
# ---------------------------------------------------------------------------


class TestChangeBand:
    def test_a_network_that_isnt_active_returns_200_with_saved(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        resp = client.put("/api/network/saved/Barn-AP/band", json={"band": "2.4ghz"})
        assert resp.status_code == 200
        assert resp.json() == {
            "outcome": "saved",
            "name": "Barn-AP",
            "band": "2.4ghz",
        }
        mock_network_service.change_band.assert_awaited_once_with(
            "Barn-AP", WifiBand.GHZ_2_4
        )

    def test_an_unknown_band_is_rejected_before_the_service(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        resp = client.put("/api/network/saved/Barn-AP/band", json={"band": "6ghz"})
        assert resp.status_code == 422
        mock_network_service.change_band.assert_not_awaited()

    def test_not_configured_returns_409(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.change_band = AsyncMock(
            side_effect=NetworkNotConfiguredError("no config file"),
        )
        resp = client.put("/api/network/saved/Barn-AP/band", json={"band": "auto"})
        assert resp.status_code == 409

    def test_unexpected_error_returns_502(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        mock_network_service.change_band = AsyncMock(
            side_effect=RuntimeError("nmcli exploded"),
        )
        resp = client.put("/api/network/saved/Barn-AP/band", json={"band": "5ghz"})
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

    def test_capabilities_is_404(self, client: TestClient) -> None:
        assert client.get("/api/network/capabilities").status_code == 404

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

    def test_band_change_is_404(self, client: TestClient) -> None:
        resp = client.put("/api/network/saved/SiteWiFi/band", json={"band": "auto"})
        assert resp.status_code == 404

    def test_network_service_is_never_reached(
        self, client: TestClient, mock_network_service: MagicMock
    ) -> None:
        """The 404 gate must short-circuit before the network service is
        touched at all."""
        client.get("/api/network/status")
        mock_network_service.is_configured.assert_not_called()
