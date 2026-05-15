"""Tests for GNSS constellation configuration feature.

Covers models, driver parsing/building, DeviceService wrappers,
and API endpoints for GNSS constellation selection.
"""

# pyright: reportPrivateUsage=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.models.device_models import (
    DeviceCapability,
    DeviceConnectionState,
    GnssConfig,
    GnssConstellation,
    GnssSystemConfig,
)
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.ublox import UbloxDriver


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestGnssModels:
    """Tests for GnssConstellation, GnssSystemConfig, GnssConfig."""

    def test_gnss_constellation_values(self) -> None:
        assert GnssConstellation.GPS.value == "gps"
        assert GnssConstellation.GLONASS.value == "glonass"
        assert GnssConstellation.GALILEO.value == "galileo"
        assert GnssConstellation.BEIDOU.value == "beidou"
        assert GnssConstellation.SBAS.value == "sbas"
        assert GnssConstellation.QZSS.value == "qzss"

    def test_gnss_system_config_defaults(self) -> None:
        cfg = GnssSystemConfig(constellation=GnssConstellation.GPS)
        assert cfg.enabled is True
        assert cfg.min_channels == 0
        assert cfg.max_channels == 0
        assert cfg.sig_cfg_mask == 0

    def test_gnss_config_empty(self) -> None:
        cfg = GnssConfig()
        assert cfg.systems == []
        assert cfg.enabled_constellations() == []

    def test_gnss_config_enabled_constellations(self) -> None:
        cfg = GnssConfig(systems=[
            GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=True),
            GnssSystemConfig(constellation=GnssConstellation.GLONASS, enabled=False),
            GnssSystemConfig(constellation=GnssConstellation.GALILEO, enabled=True),
        ])
        enabled = cfg.enabled_constellations()
        assert GnssConstellation.GPS in enabled
        assert GnssConstellation.GALILEO in enabled
        assert GnssConstellation.GLONASS not in enabled

    def test_gnss_config_serialization(self) -> None:
        cfg = GnssConfig(systems=[
            GnssSystemConfig(
                constellation=GnssConstellation.GPS,
                enabled=True,
                min_channels=8,
                max_channels=16,
                sig_cfg_mask=65537,
            ),
        ])
        data = cfg.model_dump()
        assert data["systems"][0]["constellation"] == "gps"
        assert data["systems"][0]["enabled"] is True
        assert data["systems"][0]["min_channels"] == 8

    def test_gnss_config_from_dict(self) -> None:
        data = {
            "systems": [
                {"constellation": "beidou", "enabled": False, "max_channels": 12}
            ]
        }
        cfg = GnssConfig(**data)
        assert cfg.systems[0].constellation == GnssConstellation.BEIDOU
        assert cfg.systems[0].enabled is False
        assert cfg.systems[0].max_channels == 12

    def test_gnss_select_capability_exists(self) -> None:
        assert DeviceCapability.GNSS_SELECT.value == "gnss_select"

    def test_gnss_system_config_validation(self) -> None:
        """min/max channels must be 0-255."""
        with pytest.raises(Exception):
            GnssSystemConfig(
                constellation=GnssConstellation.GPS,
                min_channels=300,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# UbloxDriver._parse_cfg_gnss() tests
# ---------------------------------------------------------------------------


def _make_cfg_gnss(**kwargs: object) -> SimpleNamespace:
    """Build a mock CFG-GNSS response message."""
    msg = SimpleNamespace(identity="CFG-GNSS", **kwargs)
    return msg


class TestUbloxParseCfgGnss:
    """Tests for UbloxDriver._parse_cfg_gnss class method."""

    def test_parse_single_system(self) -> None:
        msg = _make_cfg_gnss(
            numConfigBlocks=1,
            gnssId_01=0,    # GPS
            enable_01=1,
            resTrkCh_01=8,
            maxTrkCh_01=16,
            sigCfMask_01=65537,
        )
        config = UbloxDriver._parse_cfg_gnss(msg)  # pyright: ignore[reportPrivateUsage]
        assert len(config.systems) == 1
        assert config.systems[0].constellation == GnssConstellation.GPS
        assert config.systems[0].enabled is True
        assert config.systems[0].min_channels == 8
        assert config.systems[0].max_channels == 16

    def test_parse_multiple_systems(self) -> None:
        msg = _make_cfg_gnss(
            numConfigBlocks=3,
            gnssId_01=0,     # GPS (block 0)
            enable_01=1,
            resTrkCh_01=8,
            maxTrkCh_01=16,
            sigCfMask_01=65537,
            gnssId_02=6,     # GLONASS (block 1)
            enable_02=0,
            resTrkCh_02=4,
            maxTrkCh_02=14,
            sigCfMask_02=65537,
            gnssId_03=2,     # Galileo (block 2)
            enable_03=1,
            resTrkCh_03=4,
            maxTrkCh_03=12,
            sigCfMask_03=65537,
        )
        config = UbloxDriver._parse_cfg_gnss(msg)  # pyright: ignore[reportPrivateUsage]
        assert len(config.systems) == 3

        gps = config.systems[0]
        assert gps.constellation == GnssConstellation.GPS
        assert gps.enabled is True

        glonass = config.systems[1]
        assert glonass.constellation == GnssConstellation.GLONASS
        assert glonass.enabled is False

        galileo = config.systems[2]
        assert galileo.constellation == GnssConstellation.GALILEO
        assert galileo.enabled is True

    def test_parse_unknown_gnss_id_skipped(self) -> None:
        """Unknown gnssId (e.g. 99) should be silently skipped."""
        msg = _make_cfg_gnss(
            numConfigBlocks=2,
            gnssId_01=0,
            enable_01=1,
            resTrkCh_01=8,
            maxTrkCh_01=16,
            sigCfMask_01=65537,
            gnssId_02=99,
            enable_02=1,
            resTrkCh_02=4,
            maxTrkCh_02=8,
            sigCfMask_02=0,
        )
        config = UbloxDriver._parse_cfg_gnss(msg)  # pyright: ignore[reportPrivateUsage]
        assert len(config.systems) == 1  # Only GPS, 99 skipped

    def test_parse_fallback_flags_field(self) -> None:
        """When 'enable' attr missing, use flags bit 0."""
        msg = _make_cfg_gnss(
            numConfigBlocks=1,
            gnssId_01=0,
            # No 'enable_01' attribute
            flags_01=0x00010001,  # bit 0 = enabled
            resTrkCh_01=8,
            maxTrkCh_01=16,
            sigCfMask_01=65537,
        )
        config = UbloxDriver._parse_cfg_gnss(msg)  # pyright: ignore[reportPrivateUsage]
        assert config.systems[0].enabled is True

    def test_parse_empty_config(self) -> None:
        msg = _make_cfg_gnss(numConfigBlocks=0)
        config = UbloxDriver._parse_cfg_gnss(msg)  # pyright: ignore[reportPrivateUsage]
        assert config.systems == []


# ---------------------------------------------------------------------------
# UbloxDriver.get_gnss_config() / configure_gnss() tests
# ---------------------------------------------------------------------------


class TestUbloxGnssDriver:
    """Tests for get_gnss_config and configure_gnss on UbloxDriver."""

    @pytest.fixture()
    def connected_driver(self) -> UbloxDriver:
        driver = UbloxDriver()
        mock_serial = MagicMock()
        mock_serial.is_open = True
        mock_reader = MagicMock()
        driver._serial = mock_serial  # pyright: ignore[reportPrivateUsage]
        driver._reader = mock_reader  # pyright: ignore[reportPrivateUsage]
        return driver

    def test_get_gnss_config_success(self, connected_driver: UbloxDriver) -> None:
        msg = SimpleNamespace(
            identity="CFG-GNSS",
            numConfigBlocks=1,
            gnssId_01=0,
            enable_01=1,
            resTrkCh_01=8,
            maxTrkCh_01=16,
            sigCfMask_01=65537,
        )
        connected_driver._reader.read.return_value = (b"raw", msg)  # pyright: ignore[reportPrivateUsage]
        config = connected_driver.get_gnss_config()
        assert len(config.systems) == 1
        assert config.systems[0].constellation == GnssConstellation.GPS

    def test_get_gnss_config_no_response(self, connected_driver: UbloxDriver) -> None:
        connected_driver._reader.read.side_effect = Exception("timeout")  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(RuntimeError, match="No CFG-GNSS response"):
            connected_driver.get_gnss_config()

    def test_get_gnss_config_not_connected(self) -> None:
        driver = UbloxDriver()
        with pytest.raises(ConnectionError):
            driver.get_gnss_config()

    def test_configure_gnss_success(self, connected_driver: UbloxDriver) -> None:
        # Mock ACK response
        ack = SimpleNamespace(identity="ACK-ACK")
        connected_driver._reader.read.return_value = (b"raw", ack)  # pyright: ignore[reportPrivateUsage]

        config = GnssConfig(systems=[
            GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=True),
            GnssSystemConfig(constellation=GnssConstellation.GLONASS, enabled=False),
        ])
        connected_driver.configure_gnss(config)
        # Should have written something to serial
        connected_driver._serial.write.assert_called()  # pyright: ignore[reportPrivateUsage]

    def test_configure_gnss_nak(self, connected_driver: UbloxDriver) -> None:
        nak = SimpleNamespace(identity="ACK-NAK")
        connected_driver._reader.read.return_value = (b"raw", nak)  # pyright: ignore[reportPrivateUsage]

        config = GnssConfig(systems=[
            GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=True),
        ])
        with pytest.raises(RuntimeError, match="NAK"):
            connected_driver.configure_gnss(config)

    def test_configure_gnss_not_connected(self) -> None:
        driver = UbloxDriver()
        config = GnssConfig(systems=[
            GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=True),
        ])
        with pytest.raises(ConnectionError):
            driver.configure_gnss(config)

    def test_gnss_select_in_capabilities(self) -> None:
        driver = UbloxDriver()
        caps = driver.get_capabilities()
        assert DeviceCapability.GNSS_SELECT in caps


# ---------------------------------------------------------------------------
# DeviceService GNSS wrappers
# ---------------------------------------------------------------------------


class TestDeviceServiceGnss:
    """Tests for DeviceService.get_gnss_config / configure_gnss."""

    @pytest.fixture()
    def service_with_driver(self) -> DeviceService:
        svc = DeviceService()
        mock_driver = MagicMock()
        mock_driver.get_capabilities.return_value = {DeviceCapability.GNSS_SELECT}
        mock_driver.is_connected = True
        svc._driver = mock_driver  # pyright: ignore[reportPrivateUsage]
        svc._state = DeviceConnectionState.CONNECTED  # pyright: ignore[reportPrivateUsage]
        return svc

    @pytest.mark.asyncio()
    async def test_get_gnss_config(self, service_with_driver: DeviceService) -> None:
        expected = GnssConfig(systems=[
            GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=True),
        ])
        service_with_driver._driver.get_gnss_config.return_value = expected  # pyright: ignore[reportPrivateUsage]
        result = await service_with_driver.get_gnss_config()
        assert result == expected

    @pytest.mark.asyncio()
    async def test_get_gnss_config_not_connected(self) -> None:
        svc = DeviceService()
        with pytest.raises(RuntimeError):
            await svc.get_gnss_config()

    @pytest.mark.asyncio()
    async def test_configure_gnss(self, service_with_driver: DeviceService) -> None:
        config = GnssConfig(systems=[
            GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=True),
        ])
        await service_with_driver.configure_gnss(config)
        service_with_driver._driver.configure_gnss.assert_called_once_with(config)  # pyright: ignore[reportPrivateUsage]
        assert service_with_driver.state == DeviceConnectionState.CONNECTED

    @pytest.mark.asyncio()
    async def test_configure_gnss_error_restores_state(
        self, service_with_driver: DeviceService
    ) -> None:
        service_with_driver._driver.configure_gnss.side_effect = RuntimeError("fail")  # pyright: ignore[reportPrivateUsage]
        config = GnssConfig(systems=[
            GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=True),
        ])
        with pytest.raises(RuntimeError, match="fail"):
            await service_with_driver.configure_gnss(config)
        # State should be restored to CONNECTED (not stuck in CONFIGURING)
        assert service_with_driver.state == DeviceConnectionState.CONNECTED


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestGnssApiEndpoints:
    """Tests for GET/PUT /api/device/gnss endpoints."""

    @pytest.fixture()
    def mock_device_service(self) -> MagicMock:
        mock_svc = MagicMock(spec=DeviceService)
        mock_svc.state = DeviceConnectionState.CONNECTED
        mock_svc.is_connected = True
        mock_svc._driver = MagicMock()  # pyright: ignore[reportPrivateUsage]
        mock_svc.capabilities = {DeviceCapability.GNSS_SELECT}
        return mock_svc

    @pytest.fixture()
    def client(self, mock_device_service: MagicMock) -> TestClient:
        from sp_rtk_base.app import create_api_app

        app = create_api_app()

        from sp_rtk_base.services import get_device_service

        app.dependency_overrides[get_device_service] = lambda: mock_device_service
        return TestClient(app)

    def test_get_gnss_config(
        self, client: TestClient, mock_device_service: MagicMock
    ) -> None:
        expected = GnssConfig(systems=[
            GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=True),
        ])
        mock_device_service.get_gnss_config = AsyncMock(return_value=expected)

        resp = client.get("/api/device/gnss")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["systems"]) == 1
        assert data["systems"][0]["constellation"] == "gps"

    def test_get_gnss_config_not_connected(
        self, client: TestClient, mock_device_service: MagicMock
    ) -> None:
        mock_device_service.get_gnss_config = AsyncMock(
            side_effect=RuntimeError("Device not connected")
        )
        resp = client.get("/api/device/gnss")
        assert resp.status_code == 409

    def test_put_gnss_config(
        self, client: TestClient, mock_device_service: MagicMock
    ) -> None:
        mock_device_service.configure_gnss = AsyncMock()
        payload = {
            "systems": [
                {"constellation": "gps", "enabled": True},
                {"constellation": "glonass", "enabled": False},
            ]
        }
        resp = client.put("/api/device/gnss", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_device_service.configure_gnss.assert_called_once()

    def test_put_gnss_config_not_connected(
        self, client: TestClient, mock_device_service: MagicMock
    ) -> None:
        mock_device_service.configure_gnss = AsyncMock(
            side_effect=RuntimeError("Device not connected")
        )
        payload = {"systems": [{"constellation": "gps", "enabled": True}]}
        resp = client.put("/api/device/gnss", json=payload)
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# GNSS ID mapping tests
# ---------------------------------------------------------------------------


class TestUbloxGnssIdMapping:
    """Verify the u-blox gnssId ↔ GnssConstellation mapping."""

    def test_forward_map_covers_all_constellations(self) -> None:
        mapped = set(UbloxDriver._GNSS_ID_MAP.values())  # pyright: ignore[reportPrivateUsage]
        assert GnssConstellation.GPS in mapped
        assert GnssConstellation.GLONASS in mapped
        assert GnssConstellation.GALILEO in mapped
        assert GnssConstellation.BEIDOU in mapped
        assert GnssConstellation.SBAS in mapped
        assert GnssConstellation.QZSS in mapped

    def test_reverse_map_roundtrip(self) -> None:
        for gnss_id, constellation in UbloxDriver._GNSS_ID_MAP.items():  # pyright: ignore[reportPrivateUsage]
            assert UbloxDriver._GNSS_ID_REVERSE[constellation] == gnss_id  # pyright: ignore[reportPrivateUsage]

    def test_known_ublox_ids(self) -> None:
        m = UbloxDriver._GNSS_ID_MAP  # pyright: ignore[reportPrivateUsage]
        assert m[0] == GnssConstellation.GPS
        assert m[1] == GnssConstellation.SBAS
        assert m[2] == GnssConstellation.GALILEO
        assert m[3] == GnssConstellation.BEIDOU
        assert m[5] == GnssConstellation.QZSS
        assert m[6] == GnssConstellation.GLONASS
