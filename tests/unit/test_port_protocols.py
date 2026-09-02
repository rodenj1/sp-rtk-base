"""Tests for live port-protocol reads (issue #57).

Covers models, driver parsing/polling, DeviceService wrappers, and
API endpoints for ``GET /api/device/rtcm-ports`` and
``GET /api/device/port-protocols``.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.models.device_models import (
    DeviceConnectionState,
    PortId,
    PortProtocolConfig,
    RtcmPortConfig,
    RtcmRowId,
    UbxProtocol,
)
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.ublox import UbloxDriver, _protocol_key

# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestPortProtocolConfig:
    """Tests for the PortProtocolConfig model."""

    def test_default_empty(self) -> None:
        config = PortProtocolConfig()
        assert config.in_protocols == {}
        assert config.out_protocols == {}
        assert config.enabled_in(PortId.UART1) == []
        assert config.enabled_out(PortId.UART1) == []

    def test_enabled_in_and_out(self) -> None:
        config = PortProtocolConfig(
            in_protocols={
                PortId.UART1: [UbxProtocol.UBX, UbxProtocol.NMEA, UbxProtocol.RTCM3X],
                PortId.UART2: [UbxProtocol.UBX, UbxProtocol.RTCM3X],
            },
            out_protocols={
                PortId.UART1: [UbxProtocol.RTCM3X],
                PortId.UART2: [UbxProtocol.RTCM3X],
            },
        )
        assert config.enabled_in(PortId.UART1) == [
            UbxProtocol.UBX,
            UbxProtocol.NMEA,
            UbxProtocol.RTCM3X,
        ]
        assert config.enabled_in(PortId.UART2) == [UbxProtocol.UBX, UbxProtocol.RTCM3X]
        assert config.enabled_out(PortId.UART1) == [UbxProtocol.RTCM3X]
        # Port with no entry at all reports empty, not KeyError
        assert config.enabled_in(PortId.USB) == []

    def test_serialization_roundtrip(self) -> None:
        config = PortProtocolConfig(
            in_protocols={PortId.USB: [UbxProtocol.UBX]},
            out_protocols={PortId.USB: [UbxProtocol.UBX, UbxProtocol.NMEA]},
        )
        data = config.model_dump()
        restored = PortProtocolConfig(**data)
        assert restored == config


class TestPortIdAndUbxProtocol:
    """Tests for the PortId and UbxProtocol enums."""

    def test_port_id_members(self) -> None:
        ports = list(PortId)
        assert len(ports) == 3
        assert PortId.UART1 in ports
        assert PortId.UART2 in ports
        assert PortId.USB in ports

    def test_ubx_protocol_members(self) -> None:
        protocols = list(UbxProtocol)
        assert len(protocols) == 3
        assert UbxProtocol.UBX in protocols
        assert UbxProtocol.NMEA in protocols
        assert UbxProtocol.RTCM3X in protocols


# ---------------------------------------------------------------------------
# Driver key mapping tests
# ---------------------------------------------------------------------------


class TestProtocolKeyMapping:
    """Tests for the _protocol_key helper."""

    def test_uart_keys(self) -> None:
        assert (
            _protocol_key(PortId.UART1, "IN", UbxProtocol.UBX) == "CFG_UART1INPROT_UBX"
        )
        assert (
            _protocol_key(PortId.UART2, "OUT", UbxProtocol.RTCM3X)
            == "CFG_UART2OUTPROT_RTCM3X"
        )

    def test_usb_keys_have_no_numeric_suffix(self) -> None:
        assert _protocol_key(PortId.USB, "IN", UbxProtocol.NMEA) == "CFG_USBINPROT_NMEA"
        assert _protocol_key(PortId.USB, "OUT", UbxProtocol.UBX) == "CFG_USBOUTPROT_UBX"


# ---------------------------------------------------------------------------
# Driver parse tests
# ---------------------------------------------------------------------------


class _FakeValget:
    """Fake parsed CFG-VALGET with configurable attribute values."""

    def __init__(self, values: dict[str, int] | None = None) -> None:
        self._values = values or {}
        self.identity = "CFG-VALGET"

    def __getattr__(self, name: str) -> int:
        return self._values.get(name, 0)


class TestParsePortProtocolsValget:
    """Tests for UbloxDriver._parse_port_protocols_valget."""

    def test_parse_all_zero(self) -> None:
        config = UbloxDriver._parse_port_protocols_valget({})
        assert isinstance(config, PortProtocolConfig)
        for port in PortId:
            assert config.enabled_in(port) == []
            assert config.enabled_out(port) == []

    def test_parse_reference_receiver_uart_profile(self) -> None:
        """Acceptance criterion: UART1 in [UBX, NMEA, RTCM3] / out
        [RTCM3], UART2 in [UBX, RTCM3] / out [RTCM3]."""
        values = {
            "CFG_UART1INPROT_UBX": 1,
            "CFG_UART1INPROT_NMEA": 1,
            "CFG_UART1INPROT_RTCM3X": 1,
            "CFG_UART1OUTPROT_UBX": 0,
            "CFG_UART1OUTPROT_NMEA": 0,
            "CFG_UART1OUTPROT_RTCM3X": 1,
            "CFG_UART2INPROT_UBX": 1,
            "CFG_UART2INPROT_NMEA": 0,
            "CFG_UART2INPROT_RTCM3X": 1,
            "CFG_UART2OUTPROT_UBX": 0,
            "CFG_UART2OUTPROT_NMEA": 0,
            "CFG_UART2OUTPROT_RTCM3X": 1,
        }
        config = UbloxDriver._parse_port_protocols_valget(values)

        assert config.enabled_in(PortId.UART1) == [
            UbxProtocol.UBX,
            UbxProtocol.NMEA,
            UbxProtocol.RTCM3X,
        ]
        assert config.enabled_out(PortId.UART1) == [UbxProtocol.RTCM3X]
        assert config.enabled_in(PortId.UART2) == [UbxProtocol.UBX, UbxProtocol.RTCM3X]
        assert config.enabled_out(PortId.UART2) == [UbxProtocol.RTCM3X]

    def test_parse_usb_covered(self) -> None:
        values = {
            "CFG_USBINPROT_UBX": 1,
            "CFG_USBINPROT_NMEA": 1,
            "CFG_USBINPROT_RTCM3X": 1,
            "CFG_USBOUTPROT_UBX": 1,
            "CFG_USBOUTPROT_NMEA": 0,
            "CFG_USBOUTPROT_RTCM3X": 0,
        }
        config = UbloxDriver._parse_port_protocols_valget(values)
        assert config.enabled_in(PortId.USB) == [
            UbxProtocol.UBX,
            UbxProtocol.NMEA,
            UbxProtocol.RTCM3X,
        ]
        assert config.enabled_out(PortId.USB) == [UbxProtocol.UBX]

    def test_all_ports_present_even_when_disabled(self) -> None:
        config = UbloxDriver._parse_port_protocols_valget({})
        assert set(config.in_protocols.keys()) == set(PortId)
        assert set(config.out_protocols.keys()) == set(PortId)


# ---------------------------------------------------------------------------
# Driver get_port_protocols read tests
# ---------------------------------------------------------------------------


class TestUbloxGetPortProtocols:
    """Tests for UbloxDriver.get_port_protocols."""

    @pytest.fixture()
    def connected_driver(self) -> UbloxDriver:
        driver = UbloxDriver()
        mock_serial = MagicMock()
        mock_serial.is_open = True
        mock_reader = MagicMock()
        driver._serial = mock_serial
        driver._reader = mock_reader
        return driver

    def test_get_port_protocols_success(self, connected_driver: UbloxDriver) -> None:
        response = _FakeValget(
            {
                "CFG_UART1OUTPROT_RTCM3X": 1,
                "CFG_UART1INPROT_UBX": 1,
            }
        )
        connected_driver._reader.read.return_value = (b"", response)
        config = connected_driver.get_port_protocols()
        assert isinstance(config, PortProtocolConfig)
        assert config.enabled_out(PortId.UART1) == [UbxProtocol.RTCM3X]
        assert UbxProtocol.UBX in config.enabled_in(PortId.UART1)

    def test_get_port_protocols_no_response(
        self, connected_driver: UbloxDriver
    ) -> None:
        connected_driver._reader.read.side_effect = Exception("timeout")
        with pytest.raises(RuntimeError, match="No CFG-VALGET response"):
            connected_driver.get_port_protocols()

    def test_get_port_protocols_retries_on_timeout_then_succeeds(
        self, connected_driver: UbloxDriver
    ) -> None:
        """A CFG-VALGET reply missed once (busy receiver) doesn't fail
        the whole read — the poll is re-issued (issue #119)."""
        with patch.object(
            UbloxDriver, "_read_cfg_keys_locked", autospec=True
        ) as mock_read:
            mock_read.side_effect = [
                RuntimeError("No CFG-VALGET response for config keys"),
                {},
            ]
            config = connected_driver.get_port_protocols()
            assert isinstance(config, PortProtocolConfig)
            assert mock_read.call_count == 2

    def test_get_port_protocols_gives_up_after_three_attempts(
        self, connected_driver: UbloxDriver
    ) -> None:
        with patch.object(
            UbloxDriver, "_read_cfg_keys_locked", autospec=True
        ) as mock_read:
            mock_read.side_effect = RuntimeError(
                "No CFG-VALGET response for config keys"
            )
            with pytest.raises(RuntimeError, match="No CFG-VALGET response"):
                connected_driver.get_port_protocols()
            assert mock_read.call_count == 3

    def test_get_port_protocols_nak_is_not_retried(
        self, connected_driver: UbloxDriver
    ) -> None:
        """A genuine NAK is a distinct, definitive rejection — it must
        propagate immediately, not be masked by the timeout retry."""
        with patch.object(
            UbloxDriver, "_read_cfg_keys_locked", autospec=True
        ) as mock_read:
            mock_read.side_effect = RuntimeError(
                "Device rejected CFG-VALGET poll for [...] (NAK)"
            )
            with pytest.raises(RuntimeError, match="NAK"):
                connected_driver.get_port_protocols()
            assert mock_read.call_count == 1

    def test_get_port_protocols_not_connected(self) -> None:
        driver = UbloxDriver()
        with pytest.raises(ConnectionError):
            driver.get_port_protocols()


# ---------------------------------------------------------------------------
# DeviceService wrapper tests
# ---------------------------------------------------------------------------


class TestDeviceServicePortProtocols:
    """Tests for DeviceService.get_port_protocols / get_rtcm_port_config."""

    @pytest.fixture()
    def service_with_driver(self) -> DeviceService:
        svc = DeviceService()
        mock_driver = MagicMock()
        mock_driver.is_connected = True
        svc._driver = mock_driver
        svc._state = DeviceConnectionState.CONNECTED
        return svc

    @pytest.mark.asyncio
    async def test_get_port_protocols(self, service_with_driver: DeviceService) -> None:
        expected = PortProtocolConfig(
            in_protocols={PortId.UART1: [UbxProtocol.UBX]},
            out_protocols={PortId.UART1: [UbxProtocol.RTCM3X]},
        )
        service_with_driver._driver.get_port_protocols.return_value = expected
        result = await service_with_driver.get_port_protocols()
        assert result == expected
        service_with_driver._driver.get_port_protocols.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_port_protocols_not_connected(self) -> None:
        svc = DeviceService()
        with pytest.raises(RuntimeError):
            await svc.get_port_protocols()

    @pytest.mark.asyncio
    async def test_get_rtcm_port_config_passthrough(
        self, service_with_driver: DeviceService
    ) -> None:
        expected = RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"USB": 1}})
        service_with_driver._driver.get_rtcm_port_config.return_value = expected
        result = await service_with_driver.get_rtcm_port_config()
        assert result == expected


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestPortProtocolsAndRtcmPortsApi:
    """Tests for GET /api/device/rtcm-ports and GET /api/device/port-protocols."""

    @pytest.fixture()
    def mock_device_service(self) -> MagicMock:
        return MagicMock(spec=DeviceService)

    @pytest.fixture()
    def client(self, mock_device_service: MagicMock) -> TestClient:
        from sp_rtk_base.app import create_api_app
        from sp_rtk_base.services import get_device_service

        app = create_api_app()
        app.dependency_overrides[get_device_service] = lambda: mock_device_service
        return TestClient(app)

    def test_get_rtcm_ports(
        self, client: TestClient, mock_device_service: MagicMock
    ) -> None:
        expected = RtcmPortConfig(messages={RtcmRowId.RTCM_1005: {"USB": 1}})
        mock_device_service.get_rtcm_port_config = AsyncMock(return_value=expected)

        resp = client.get("/api/device/rtcm-ports")
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"]["1005"]["USB"] == 1

    def test_get_rtcm_ports_not_connected(
        self, client: TestClient, mock_device_service: MagicMock
    ) -> None:
        mock_device_service.get_rtcm_port_config = AsyncMock(
            side_effect=RuntimeError("Device not connected")
        )
        resp = client.get("/api/device/rtcm-ports")
        assert resp.status_code == 409

    def test_get_port_protocols(
        self, client: TestClient, mock_device_service: MagicMock
    ) -> None:
        expected = PortProtocolConfig(
            in_protocols={PortId.UART1: [UbxProtocol.UBX, UbxProtocol.NMEA]},
            out_protocols={PortId.UART1: [UbxProtocol.RTCM3X]},
        )
        mock_device_service.get_port_protocols = AsyncMock(return_value=expected)

        resp = client.get("/api/device/port-protocols")
        assert resp.status_code == 200
        data = resp.json()
        assert data["in_protocols"]["UART1"] == ["UBX", "NMEA"]
        assert data["out_protocols"]["UART1"] == ["RTCM3X"]

    def test_get_port_protocols_not_connected(
        self, client: TestClient, mock_device_service: MagicMock
    ) -> None:
        mock_device_service.get_port_protocols = AsyncMock(
            side_effect=RuntimeError("Device not connected")
        )
        resp = client.get("/api/device/port-protocols")
        assert resp.status_code == 409
