"""Tests for GPS position feature (Phase 3.4).

Covers:
- GpsPosition and GpsFixType model validation
- UbloxDriver.get_position() and _parse_nav_pvt()
- DeviceService.get_position() async wrapper
- API GET /api/device/position endpoint
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sp_base.app import create_api_app
from sp_base.models.device_models import (
    DeviceConnectionState,
    GpsFixType,
    GpsPosition,
)
from sp_base.services.device_service import DeviceService
from sp_base.services.drivers.ublox import UbloxDriver


# ---------------------------------------------------------------------------
# Helpers — Mock NAV-PVT responses
# ---------------------------------------------------------------------------


def _make_nav_pvt(
    fix_type: int = 3,
    carr_soln: int = 0,
    lat: int = 476062000,  # 47.6062° * 1e7
    lon: int = -1223321000,  # -122.3321° * 1e7
    height: int = 100500,  # 100.5m in mm
    h_msl: int = 95200,  # 95.2m in mm
    h_acc: int = 1500,  # 1.5m in mm
    v_acc: int = 2500,  # 2.5m in mm
    num_sv: int = 18,
    g_speed: int = 500,  # 0.5 m/s in mm/s
    head_mot: int = 18000000,  # 180.0° * 1e5
    p_dop: int = 150,  # 1.50 * 100
    year: int = 2026,
    month: int = 4,
    day: int = 14,
    hour: int = 12,
    minute: int = 30,
    second: int = 45,
    nano: int = 500000000,  # 500ms in ns
) -> SimpleNamespace:
    """Create a mock NAV-PVT parsed response."""
    return SimpleNamespace(
        identity="NAV-PVT",
        fixType=fix_type,
        carrSoln=carr_soln,
        lat=lat,
        lon=lon,
        height=height,
        hMSL=h_msl,
        hAcc=h_acc,
        vAcc=v_acc,
        numSV=num_sv,
        gSpeed=g_speed,
        headMot=head_mot,
        pDOP=p_dop,
        year=year,
        month=month,
        day=day,
        hour=hour,
        min=minute,
        second=second,
        nano=nano,
    )




# ---------------------------------------------------------------------------
# GpsPosition / GpsFixType model tests
# ---------------------------------------------------------------------------


class TestGpsPositionModel:
    """Tests for the GpsPosition Pydantic model."""

    def test_default_values(self) -> None:
        pos = GpsPosition()
        assert pos.fix_type == GpsFixType.NO_FIX
        assert pos.rtk_status == "none"
        assert pos.latitude == 0.0
        assert pos.longitude == 0.0
        assert pos.altitude_m == 0.0
        assert pos.num_satellites == 0
        assert pos.timestamp is None

    def test_full_construction(self) -> None:
        ts = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
        pos = GpsPosition(
            fix_type=GpsFixType.FIX_3D,
            rtk_status="fixed",
            latitude=47.6062,
            longitude=-122.3321,
            altitude_m=100.5,
            altitude_msl_m=95.2,
            horizontal_accuracy_m=0.015,
            vertical_accuracy_m=0.025,
            num_satellites=24,
            speed_m_s=0.5,
            heading_deg=180.0,
            pdop=1.2,
            timestamp=ts,
        )
        assert pos.fix_type == GpsFixType.FIX_3D
        assert pos.rtk_status == "fixed"
        assert pos.latitude == 47.6062
        assert pos.longitude == -122.3321
        assert pos.num_satellites == 24
        assert pos.timestamp == ts

    def test_json_serialization(self) -> None:
        pos = GpsPosition(fix_type=GpsFixType.FIX_2D, latitude=45.0)
        data = pos.model_dump()
        assert data["fix_type"] == "2d"
        assert data["latitude"] == 45.0

    def test_negative_accuracy_rejected(self) -> None:
        with pytest.raises(Exception):
            GpsPosition(horizontal_accuracy_m=-1.0)

    def test_negative_satellites_rejected(self) -> None:
        with pytest.raises(Exception):
            GpsPosition(num_satellites=-1)


class TestGpsFixType:
    """Tests for the GpsFixType enum."""

    def test_all_values(self) -> None:
        assert GpsFixType.NO_FIX == "no_fix"
        assert GpsFixType.DEAD_RECKONING == "dead_reckoning"
        assert GpsFixType.FIX_2D == "2d"
        assert GpsFixType.FIX_3D == "3d"
        assert GpsFixType.GNSS_DR == "gnss_dr"
        assert GpsFixType.TIME_ONLY == "time_only"

    def test_string_comparison(self) -> None:
        assert GpsFixType.FIX_3D == "3d"
        assert GpsFixType("3d") == GpsFixType.FIX_3D


# ---------------------------------------------------------------------------
# UbloxDriver.get_position() tests
# ---------------------------------------------------------------------------


class TestUbloxGetPosition:
    """Tests for UbloxDriver.get_position() and _parse_nav_pvt()."""

    @pytest.fixture()
    def connected_driver(self) -> UbloxDriver:
        """Create a driver with mocked serial connection."""
        driver = UbloxDriver()
        mock_serial = MagicMock()
        mock_serial.is_open = True
        mock_reader = MagicMock()
        driver._serial = mock_serial  # pyright: ignore[reportPrivateUsage]
        driver._reader = mock_reader  # pyright: ignore[reportPrivateUsage]
        return driver

    def test_get_position_3d_fix(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(fix_type=3, num_sv=18)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()

        assert pos.fix_type == GpsFixType.FIX_3D
        assert pos.rtk_status == "none"
        assert abs(pos.latitude - 47.6062) < 0.001
        assert abs(pos.longitude - (-122.3321)) < 0.001
        assert abs(pos.altitude_m - 100.5) < 0.1
        assert pos.num_satellites == 18

    def test_get_position_rtk_fixed(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(fix_type=3, carr_soln=2)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.rtk_status == "fixed"

    def test_get_position_rtk_float(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(fix_type=3, carr_soln=1)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.rtk_status == "float"

    def test_get_position_no_fix(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(fix_type=0, num_sv=0, lat=0, lon=0, height=0)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.fix_type == GpsFixType.NO_FIX
        assert pos.num_satellites == 0

    def test_get_position_2d_fix(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(fix_type=2)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.fix_type == GpsFixType.FIX_2D

    def test_get_position_accuracy(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(h_acc=1500, v_acc=2500)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert abs(pos.horizontal_accuracy_m - 1.5) < 0.01
        assert abs(pos.vertical_accuracy_m - 2.5) < 0.01

    def test_get_position_speed_heading(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(g_speed=1500, head_mot=9000000)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert abs(pos.speed_m_s - 1.5) < 0.01
        assert abs(pos.heading_deg - 90.0) < 0.01

    def test_get_position_pdop(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(p_dop=250)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert abs(pos.pdop - 2.5) < 0.01

    def test_get_position_timestamp(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(
            year=2026, month=4, day=14,
            hour=12, minute=30, second=45,
            nano=500000000,
        )
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.timestamp is not None
        assert pos.timestamp.year == 2026
        assert pos.timestamp.month == 4
        assert pos.timestamp.day == 14
        assert pos.timestamp.hour == 12
        assert pos.timestamp.minute == 30
        assert pos.timestamp.second == 45

    def test_get_position_timestamp_invalid_year(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(year=0)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.timestamp is None

    def test_get_position_msl_altitude(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(h_msl=95200)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert abs(pos.altitude_msl_m - 95.2) < 0.1

    def test_get_position_no_response(self, connected_driver: UbloxDriver) -> None:
        """If no NAV-PVT response, return default GpsPosition."""
        connected_driver._reader.read.side_effect = Exception("timeout")  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.fix_type == GpsFixType.NO_FIX
        assert pos.latitude == 0.0

    def test_get_position_skips_other_messages(self, connected_driver: UbloxDriver) -> None:
        """Non-NAV-PVT messages should be skipped."""
        other_msg = SimpleNamespace(identity="NAV-SVIN", active=1)
        nav_pvt = _make_nav_pvt()

        connected_driver._reader.read.side_effect = [  # type: ignore[union-attr]
            (b"raw", other_msg),
            (b"raw", nav_pvt),
        ]

        pos = connected_driver.get_position()
        assert pos.fix_type == GpsFixType.FIX_3D

    def test_get_position_not_connected(self) -> None:
        driver = UbloxDriver()
        with pytest.raises(ConnectionError):
            driver.get_position()

    def test_fix_type_dead_reckoning(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(fix_type=1)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.fix_type == GpsFixType.DEAD_RECKONING

    def test_fix_type_gnss_dr(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(fix_type=4)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.fix_type == GpsFixType.GNSS_DR

    def test_fix_type_time_only(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(fix_type=5)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.fix_type == GpsFixType.TIME_ONLY

    def test_fix_type_unknown_defaults_no_fix(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(fix_type=99)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.fix_type == GpsFixType.NO_FIX

    def test_negative_speed_clamped(self, connected_driver: UbloxDriver) -> None:
        nav_pvt = _make_nav_pvt(g_speed=-100)
        connected_driver._reader.read.return_value = (b"raw", nav_pvt)  # type: ignore[union-attr]

        pos = connected_driver.get_position()
        assert pos.speed_m_s == 0.0


# ---------------------------------------------------------------------------
# DeviceService.get_position() tests
# ---------------------------------------------------------------------------


class TestDeviceServiceGetPosition:
    """Tests for DeviceService.get_position()."""

    @pytest.fixture()
    def service_with_driver(self) -> DeviceService:
        """Create a DeviceService with a connected mock driver."""
        svc = DeviceService()
        mock_driver = MagicMock()
        mock_driver.vendor_name = "test"
        mock_driver.is_connected = True
        mock_driver.get_position.return_value = GpsPosition(
            fix_type=GpsFixType.FIX_3D,
            latitude=47.6,
            longitude=-122.3,
            num_satellites=15,
        )
        svc._driver = mock_driver  # pyright: ignore[reportPrivateUsage]
        svc._state = DeviceConnectionState.CONNECTED  # pyright: ignore[reportPrivateUsage]
        return svc

    @pytest.mark.asyncio()
    async def test_get_position_success(self, service_with_driver: DeviceService) -> None:
        pos = await service_with_driver.get_position()
        assert pos.fix_type == GpsFixType.FIX_3D
        assert pos.latitude == 47.6
        assert pos.num_satellites == 15

    @pytest.mark.asyncio()
    async def test_get_position_not_connected(self) -> None:
        svc = DeviceService()
        with pytest.raises(RuntimeError, match="No GPS driver loaded"):
            await svc.get_position()

    @pytest.mark.asyncio()
    async def test_get_position_disconnected_state(self) -> None:
        svc = DeviceService()
        svc._driver = MagicMock()  # pyright: ignore[reportPrivateUsage]
        svc._state = DeviceConnectionState.DISCONNECTED  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(RuntimeError, match="Device not connected"):
            await svc.get_position()


# ---------------------------------------------------------------------------
# API GET /api/device/position tests
# ---------------------------------------------------------------------------


class TestPositionAPI:
    """Tests for GET /api/device/position."""

    @pytest.fixture()
    def mock_device_service(self) -> DeviceService:
        svc = MagicMock(spec=DeviceService)
        svc.is_available = True
        svc.is_connected = True
        svc.get_position = AsyncMock(return_value=GpsPosition(
            fix_type=GpsFixType.FIX_3D,
            rtk_status="fixed",
            latitude=47.6062,
            longitude=-122.3321,
            altitude_m=100.5,
            horizontal_accuracy_m=0.015,
            vertical_accuracy_m=0.025,
            num_satellites=24,
        ))
        return svc

    @pytest.fixture()
    def client(self, mock_device_service: DeviceService) -> TestClient:
        from sp_base.services import get_device_service

        app = create_api_app()
        app.dependency_overrides[get_device_service] = lambda: mock_device_service
        return TestClient(app)

    def test_get_position_success(self, client: TestClient) -> None:
        resp = client.get("/api/device/position")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fix_type"] == "3d"
        assert data["rtk_status"] == "fixed"
        assert abs(data["latitude"] - 47.6062) < 0.001
        assert abs(data["longitude"] - (-122.3321)) < 0.001
        assert data["num_satellites"] == 24

    def test_get_position_not_connected(self, client: TestClient, mock_device_service: DeviceService) -> None:
        mock_device_service.get_position = AsyncMock(  # type: ignore[assignment]
            side_effect=RuntimeError("Device not connected"),
        )
        resp = client.get("/api/device/position")
        assert resp.status_code == 409
        assert "not connected" in resp.json()["detail"]

    def test_get_position_no_driver(self, client: TestClient, mock_device_service: DeviceService) -> None:
        mock_device_service.get_position = AsyncMock(  # type: ignore[assignment]
            side_effect=RuntimeError("No GPS driver loaded"),
        )
        resp = client.get("/api/device/position")
        assert resp.status_code == 409

    def test_get_position_no_fix(self, client: TestClient, mock_device_service: DeviceService) -> None:
        mock_device_service.get_position = AsyncMock(  # type: ignore[assignment]
            return_value=GpsPosition(),
        )
        resp = client.get("/api/device/position")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fix_type"] == "no_fix"
        assert data["num_satellites"] == 0
