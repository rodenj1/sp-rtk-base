"""Tests for Device API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.device_models import (
    BaseMode,
    CurrentBaseConfig,
    DeviceCapability,
    DeviceConnectionState,
    DeviceInfo,
    DeviceStatus,
    SurveyInProgress,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.relay_service import RelayService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_device_service() -> DeviceService:
    """Create a mock DeviceService."""
    svc = MagicMock(spec=DeviceService)
    svc.is_available = False
    svc.is_connected = False
    svc.capabilities = set()
    svc.device_info = None
    svc.get_status.return_value = DeviceStatus()
    return svc


@pytest.fixture()
def mock_relay_service() -> RelayService:
    """Create a mock RelayService for handoff tests."""
    svc = MagicMock(spec=RelayService)
    svc.is_running = False
    svc.start_relay = AsyncMock()
    return svc


@pytest.fixture()
def mock_config_service() -> ConfigService:
    """Create a mock ConfigService for handoff tests."""
    svc = MagicMock(spec=ConfigService)
    svc.get_destinations.return_value = []
    return svc


@pytest.fixture()
def client(mock_device_service: DeviceService) -> TestClient:
    """Create a test client with the device service dependency overridden."""
    from sp_rtk_base.services import get_device_service

    app = create_api_app()
    app.dependency_overrides[get_device_service] = lambda: mock_device_service
    return TestClient(app)


@pytest.fixture()
def handoff_client(
    mock_device_service: DeviceService,
    mock_relay_service: RelayService,
    mock_config_service: ConfigService,
) -> TestClient:
    """Create a test client with all three dependencies overridden."""
    from sp_rtk_base.services import (
        get_config_service,
        get_device_service,
        get_relay_service,
    )

    app = create_api_app()
    app.dependency_overrides[get_device_service] = lambda: mock_device_service
    app.dependency_overrides[get_relay_service] = lambda: mock_relay_service
    app.dependency_overrides[get_config_service] = lambda: mock_config_service
    return TestClient(app)


# ---------------------------------------------------------------------------
# Serial port discovery
# ---------------------------------------------------------------------------


class TestListPorts:
    """Tests for GET /api/device/ports."""

    @patch("sp_rtk_base.services.drivers.base.GpsReceiverDriver.list_serial_ports")
    def test_list_ports(self, mock_list: MagicMock, client: TestClient) -> None:
        from sp_rtk_base.models.device_models import SerialPortInfo

        mock_list.return_value = [
            SerialPortInfo(
                port="/dev/ttyUSB0",
                description="FTDI",
                manufacturer="FTDI",
                vid=0x0403,
                pid=0x6001,
                is_gps=True,
            ),
        ]
        resp = client.get("/api/device/ports")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["port"] == "/dev/ttyUSB0"
        assert data[0]["is_gps"] is True

    @patch("sp_rtk_base.services.drivers.base.GpsReceiverDriver.list_serial_ports")
    def test_list_ports_empty(self, mock_list: MagicMock, client: TestClient) -> None:
        mock_list.return_value = []
        resp = client.get("/api/device/ports")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class TestConnect:
    """Tests for POST /api/device/connect."""

    @patch("sp_rtk_base.api.device.create_driver")
    def test_connect_success(
        self,
        mock_create: MagicMock,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_device_service.connect = AsyncMock(
            return_value=DeviceInfo(vendor="u-blox", model="ZED-F9P"),
        )

        resp = client.post(
            "/api/device/connect",
            json={
                "vendor": "ublox",
                "port": "/dev/ttyUSB0",
                "baud_rate": 57600,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "ZED-F9P" in data["message"]
        mock_device_service.set_driver.assert_called_once_with(mock_driver)

    @patch("sp_rtk_base.api.device.create_driver")
    def test_connect_unknown_vendor(
        self,
        mock_create: MagicMock,
        client: TestClient,
    ) -> None:
        mock_create.side_effect = ValueError("Unknown GPS driver 'bad'")
        resp = client.post(
            "/api/device/connect",
            json={
                "vendor": "bad",
                "port": "/dev/ttyUSB0",
            },
        )
        assert resp.status_code == 400
        assert "Unknown GPS driver" in resp.json()["detail"]

    @patch("sp_rtk_base.api.device.create_driver")
    def test_connect_already_connected(
        self,
        mock_create: MagicMock,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_create.return_value = MagicMock()
        mock_device_service.set_driver.side_effect = RuntimeError(
            "Cannot change driver while connected"
        )
        resp = client.post(
            "/api/device/connect",
            json={
                "vendor": "ublox",
                "port": "/dev/ttyUSB0",
            },
        )
        assert resp.status_code == 409

    @patch("sp_rtk_base.api.device.create_driver")
    def test_connect_relay_running(
        self,
        mock_create: MagicMock,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_create.return_value = MagicMock()
        mock_device_service.connect = AsyncMock(
            side_effect=RuntimeError("Cannot connect while relay is running"),
        )
        resp = client.post(
            "/api/device/connect",
            json={
                "vendor": "ublox",
                "port": "/dev/ttyUSB0",
            },
        )
        assert resp.status_code == 409

    @patch("sp_rtk_base.api.device.create_driver")
    def test_connect_serial_error(
        self,
        mock_create: MagicMock,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_create.return_value = MagicMock()
        mock_device_service.connect = AsyncMock(
            side_effect=ConnectionError("Failed to open /dev/ttyUSB0"),
        )
        resp = client.post(
            "/api/device/connect",
            json={
                "vendor": "ublox",
                "port": "/dev/ttyUSB0",
            },
        )
        assert resp.status_code == 502
        assert "Failed to open" in resp.json()["detail"]


class TestDisconnect:
    """Tests for POST /api/device/disconnect."""

    def test_disconnect_success(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.is_available = True
        mock_device_service.disconnect = AsyncMock()
        resp = client.post("/api/device/disconnect")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_disconnect_no_device(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.is_available = False
        resp = client.post("/api/device/disconnect")
        assert resp.status_code == 409
        assert "No device" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Status & capabilities
# ---------------------------------------------------------------------------


class TestStatus:
    """Tests for GET /api/device/status."""

    def test_status_disconnected(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_status.return_value = DeviceStatus(
            state=DeviceConnectionState.DISCONNECTED,
        )
        resp = client.get("/api/device/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "disconnected"

    def test_status_connected(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_status.return_value = DeviceStatus(
            state=DeviceConnectionState.CONNECTED,
            port="/dev/ttyUSB0",
            baud_rate=57600,
            info=DeviceInfo(vendor="u-blox", model="ZED-F9P"),
            capabilities=[DeviceCapability.SURVEY_IN],
        )
        resp = client.get("/api/device/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "connected"
        assert data["port"] == "/dev/ttyUSB0"
        assert data["info"]["model"] == "ZED-F9P"


class TestCapabilities:
    """Tests for GET /api/device/capabilities."""

    def test_capabilities_with_driver(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.capabilities = {
            DeviceCapability.SURVEY_IN,
            DeviceCapability.FIXED_BASE,
        }
        resp = client.get("/api/device/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert "survey_in" in data
        assert "fixed_base" in data

    def test_capabilities_no_driver(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.capabilities = set()
        resp = client.get("/api/device/capabilities")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Configuration endpoints
# ---------------------------------------------------------------------------


class TestConfigureSurveyIn:
    """Tests for POST /api/device/configure/survey-in."""

    def test_survey_in_success(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.configure_survey_in = AsyncMock()
        resp = client.post(
            "/api/device/configure/survey-in",
            json={
                "min_duration_seconds": 300,
                "accuracy_limit_mm": 40000,
            },
        )
        assert resp.status_code == 200
        assert "Survey-in configured" in resp.json()["message"]

    def test_survey_in_not_connected(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.configure_survey_in = AsyncMock(
            side_effect=RuntimeError("Device not connected"),
        )
        resp = client.post(
            "/api/device/configure/survey-in",
            json={
                "min_duration_seconds": 120,
                "accuracy_limit_mm": 50000,
            },
        )
        assert resp.status_code == 409


class TestConfigureFixedBase:
    """Tests for POST /api/device/configure/fixed-base."""

    def test_fixed_base_success(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.configure_fixed_base = AsyncMock()
        resp = client.post(
            "/api/device/configure/fixed-base",
            json={
                "latitude": 47.3977,
                "longitude": 8.5456,
                "altitude_m": 408.0,
                "accuracy_mm": 500,
            },
        )
        assert resp.status_code == 200
        assert "Fixed base configured" in resp.json()["message"]

    def test_fixed_base_validation_error(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/api/device/configure/fixed-base",
            json={
                "latitude": 200.0,  # invalid
                "longitude": 8.5,
                "altitude_m": 100.0,
            },
        )
        assert resp.status_code == 422  # Pydantic validation


class TestConfigureRtcm:
    """Tests for POST /api/device/configure/rtcm."""

    def test_rtcm_success(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.configure_rtcm_messages = AsyncMock()
        resp = client.post(
            "/api/device/configure/rtcm",
            json={
                "message_ids": [1005, 1077, 1087],
                "rate_hz": 1,
            },
        )
        assert resp.status_code == 200
        assert "RTCM messages configured" in resp.json()["message"]


class TestSaveToFlash:
    """Tests for POST /api/device/save."""

    def test_save_success(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.save_to_flash = AsyncMock()
        resp = client.post("/api/device/save")
        assert resp.status_code == 200
        assert "saved to flash" in resp.json()["message"]

    def test_save_not_connected(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.save_to_flash = AsyncMock(
            side_effect=RuntimeError("Device not connected"),
        )
        resp = client.post("/api/device/save")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Survey-in polling
# ---------------------------------------------------------------------------


class TestSurveyInPolling:
    """Tests for GET /api/device/survey-in."""

    def test_survey_in_active(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_survey_in_status = AsyncMock(
            return_value=SurveyInProgress(
                active=True,
                valid=False,
                duration_seconds=120,
                mean_accuracy_mm=3500.0,
                observations=120,
            ),
        )
        resp = client.get("/api/device/survey-in")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert data["duration_seconds"] == 120
        assert data["mean_accuracy_mm"] == 3500.0

    def test_survey_in_not_connected(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_survey_in_status = AsyncMock(
            side_effect=RuntimeError("Device not connected"),
        )
        resp = client.get("/api/device/survey-in")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Base config polling
# ---------------------------------------------------------------------------


class TestBaseConfig:
    """Tests for GET /api/device/base-config."""

    def test_base_config_fixed(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_base_config = AsyncMock(
            return_value=CurrentBaseConfig(
                mode=BaseMode.FIXED,
                latitude=47.123456,
                longitude=-122.654321,
                altitude_m=100.5,
                accuracy_mm=500,
            ),
        )
        resp = client.get("/api/device/base-config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "fixed"
        assert data["latitude"] == pytest.approx(47.123456)
        assert data["longitude"] == pytest.approx(-122.654321)
        assert data["altitude_m"] == pytest.approx(100.5)
        assert data["accuracy_mm"] == 500

    def test_base_config_disabled(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_base_config = AsyncMock(
            return_value=CurrentBaseConfig(mode=BaseMode.DISABLED),
        )
        resp = client.get("/api/device/base-config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "disabled"
        assert data["latitude"] == 0.0

    def test_base_config_survey_in(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_base_config = AsyncMock(
            return_value=CurrentBaseConfig(mode=BaseMode.SURVEY_IN),
        )
        resp = client.get("/api/device/base-config")
        assert resp.status_code == 200
        assert resp.json()["mode"] == "survey_in"

    def test_base_config_not_connected(
        self,
        client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_base_config = AsyncMock(
            side_effect=RuntimeError("Device not connected"),
        )
        resp = client.get("/api/device/base-config")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Handoff — device → relay
# ---------------------------------------------------------------------------


class TestHandoff:
    """Tests for POST /api/device/handoff."""

    def test_handoff_success(
        self,
        handoff_client: TestClient,
        mock_device_service: MagicMock,
        mock_relay_service: MagicMock,
        mock_config_service: MagicMock,
    ) -> None:
        """Handoff disconnects device, saves config, starts relay."""
        mock_device_service.is_connected = True
        mock_device_service.get_status.return_value = DeviceStatus(
            state=DeviceConnectionState.CONNECTED,
            port="/dev/ttyUSB0",
            baud_rate=115200,
        )
        mock_device_service.driver = MagicMock()
        mock_device_service.driver.vendor_name = "u-blox"
        mock_device_service.disconnect = AsyncMock()

        resp = handoff_client.post("/api/device/handoff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "/dev/ttyUSB0" in data["message"]

        # Verify sequence: save device profile, disconnect, save input, start relay
        mock_config_service.save_device_profile.assert_called_once()
        profile = mock_config_service.save_device_profile.call_args[0][0]
        assert profile.port == "/dev/ttyUSB0"
        assert profile.baud_rate == 115200
        assert profile.vendor == "u-blox"

        mock_device_service.disconnect.assert_awaited_once()

        mock_config_service.save_input_config.assert_called_once()
        input_cfg = mock_config_service.save_input_config.call_args[0][0]
        assert input_cfg.source == "usb_serial"
        assert input_cfg.config["port"] == "/dev/ttyUSB0"
        assert input_cfg.config["baudrate"] == 115200

        mock_relay_service.start_relay.assert_awaited_once()

    def test_handoff_not_connected(
        self,
        handoff_client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        """Handoff returns 409 when device is not connected."""
        mock_device_service.is_connected = False
        resp = handoff_client.post("/api/device/handoff")
        assert resp.status_code == 409
        assert "not connected" in resp.json()["detail"]

    def test_handoff_relay_already_running(
        self,
        handoff_client: TestClient,
        mock_device_service: MagicMock,
        mock_relay_service: MagicMock,
    ) -> None:
        """Handoff returns 409 when relay is already running."""
        mock_device_service.is_connected = True
        mock_relay_service.is_running = True
        resp = handoff_client.post("/api/device/handoff")
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"]

    def test_handoff_relay_start_fails(
        self,
        handoff_client: TestClient,
        mock_device_service: MagicMock,
        mock_relay_service: MagicMock,
        mock_config_service: MagicMock,
    ) -> None:
        """Handoff returns 500 if relay engine fails to start."""
        mock_device_service.is_connected = True
        mock_device_service.get_status.return_value = DeviceStatus(
            state=DeviceConnectionState.CONNECTED,
            port="/dev/ttyUSB0",
            baud_rate=115200,
        )
        mock_device_service.driver = MagicMock()
        mock_device_service.driver.vendor_name = "u-blox"
        mock_device_service.disconnect = AsyncMock()
        mock_relay_service.start_relay = AsyncMock(
            side_effect=RuntimeError("Engine failed"),
        )

        resp = handoff_client.post("/api/device/handoff")
        assert resp.status_code == 500
        assert "Relay start failed" in resp.json()["detail"]

    def test_handoff_with_destinations(
        self,
        handoff_client: TestClient,
        mock_device_service: MagicMock,
        mock_relay_service: MagicMock,
        mock_config_service: MagicMock,
    ) -> None:
        """Handoff passes enabled destinations to relay start."""
        from sp_rtk_base.models.config_models import DestinationProfile

        mock_device_service.is_connected = True
        mock_device_service.get_status.return_value = DeviceStatus(
            state=DeviceConnectionState.CONNECTED,
            port="/dev/ttyUSB0",
            baud_rate=115200,
        )
        mock_device_service.driver = MagicMock()
        mock_device_service.driver.vendor_name = "u-blox"
        mock_device_service.disconnect = AsyncMock()

        mock_config_service.get_destinations.return_value = [
            DestinationProfile(
                name="tcp1",
                type="tcp_server",
                enabled=True,
                config={"host": "0.0.0.0", "port": 5016},
            ),
            DestinationProfile(
                name="disabled",
                type="tcp_server",
                enabled=False,
                config={"host": "0.0.0.0", "port": 5017},
            ),
        ]

        resp = handoff_client.post("/api/device/handoff")
        assert resp.status_code == 200

        # Only enabled destinations should be passed
        call_args = mock_relay_service.start_relay.call_args
        relay_dests = call_args[0][1]  # second positional arg
        assert len(relay_dests) == 1
        assert relay_dests[0].name == "tcp1"
