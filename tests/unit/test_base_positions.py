"""Tests for Phase 3.2+3.3 — Survey-In Auto-Promote & Named Position Profiles.

Covers:
- ECEF→LLH conversion in UbloxDriver
- ConfigService base position CRUD
- API: promote-survey-in, base-positions CRUD, restore
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import BaseStationPosition
from sp_rtk_base.models.device_models import (
    DeviceCapability,
    DeviceConnectionState,
    DeviceStatus,
    SurveyInProgress,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.ublox import UbloxDriver

# ---------------------------------------------------------------------------
# ECEF → LLH conversion
# ---------------------------------------------------------------------------


class TestEcefToLlh:
    """Tests for UbloxDriver._extract_svin_position()."""

    def test_known_position_zurich(self) -> None:
        """Convert ECEF for Zurich (approx 47.38°N, 8.54°E, 408m)."""
        # ECEF for Zurich: X≈4283000m, Y≈637000m, Z≈4671000m
        # NAV-SVIN stores in cm, HP in 0.1mm
        parsed = MagicMock()
        parsed.meanX = 428300000  # cm (4283000.00 m)
        parsed.meanY = 63700000  # cm (637000.00 m)
        parsed.meanZ = 467100000  # cm (4671000.00 m)
        parsed.meanXHP = 0
        parsed.meanYHP = 0
        parsed.meanZHP = 0

        lat, lon, alt = UbloxDriver.extract_svin_position(parsed)

        assert abs(lat - 47.38) < 0.5  # Within 0.5°
        assert abs(lon - 8.54) < 0.5
        # Altitude from rough ECEF is approximate
        assert alt > -1000 and alt < 10000

    def test_known_position_equator(self) -> None:
        """Convert ECEF for a point on the equator at 0°N 0°E."""
        # On the equator at prime meridian: X≈6378137m, Y=0, Z=0
        parsed = MagicMock()
        parsed.meanX = 637813700  # cm
        parsed.meanY = 0
        parsed.meanZ = 0
        parsed.meanXHP = 0
        parsed.meanYHP = 0
        parsed.meanZHP = 0

        lat, lon, alt = UbloxDriver.extract_svin_position(parsed)

        assert abs(lat) < 0.01  # Near 0° latitude
        assert abs(lon) < 0.01  # Near 0° longitude
        assert abs(alt) < 100  # Near sea level

    def test_high_precision_offset(self) -> None:
        """HP fields add sub-mm precision."""
        parsed = MagicMock()
        parsed.meanX = 637813700  # cm
        parsed.meanY = 0
        parsed.meanZ = 0
        parsed.meanXHP = 5000  # 0.5m offset in HP
        parsed.meanYHP = 0
        parsed.meanZHP = 0

        lat, lon, _alt = UbloxDriver.extract_svin_position(parsed)
        # Should still be roughly the same position
        assert abs(lat) < 0.01
        assert abs(lon) < 0.01


# ---------------------------------------------------------------------------
# ConfigService base position CRUD
# ---------------------------------------------------------------------------


class TestConfigServiceBasePositions:
    """Tests for ConfigService base position CRUD methods."""

    @pytest.fixture()
    def svc(self, tmp_path: object) -> ConfigService:
        from pathlib import Path

        return ConfigService(config_path=Path(str(tmp_path)) / "config.yaml")

    def test_get_base_positions_empty(self, svc: ConfigService) -> None:
        assert svc.get_base_positions() == []

    def test_save_and_get_base_position(self, svc: ConfigService) -> None:
        pos = BaseStationPosition(
            name="Office_Roof",
            latitude=47.397742,
            longitude=8.545594,
            altitude_m=408.123,
            accuracy_mm=2500.0,
        )
        svc.save_base_position(pos)

        positions = svc.get_base_positions()
        assert len(positions) == 1
        assert positions[0].name == "Office_Roof"
        assert positions[0].latitude == 47.397742

    def test_get_base_position_by_name(self, svc: ConfigService) -> None:
        pos = BaseStationPosition(
            name="Site_A",
            latitude=46.0,
            longitude=7.0,
            altitude_m=500.0,
        )
        svc.save_base_position(pos)

        result = svc.get_base_position("Site_A")
        assert result is not None
        assert result.name == "Site_A"

    def test_get_base_position_not_found(self, svc: ConfigService) -> None:
        assert svc.get_base_position("Nonexistent") is None

    def test_save_replaces_existing(self, svc: ConfigService) -> None:
        pos1 = BaseStationPosition(
            name="Site_A", latitude=46.0, longitude=7.0, altitude_m=500.0
        )
        svc.save_base_position(pos1)

        pos2 = BaseStationPosition(
            name="Site_A", latitude=47.0, longitude=8.0, altitude_m=600.0
        )
        svc.save_base_position(pos2)

        positions = svc.get_base_positions()
        assert len(positions) == 1
        assert positions[0].latitude == 47.0

    def test_delete_base_position(self, svc: ConfigService) -> None:
        pos = BaseStationPosition(
            name="Delete_Me", latitude=46.0, longitude=7.0, altitude_m=100.0
        )
        svc.save_base_position(pos)
        assert svc.delete_base_position("Delete_Me") is True
        assert svc.get_base_positions() == []

    def test_delete_not_found(self, svc: ConfigService) -> None:
        assert svc.delete_base_position("Nonexistent") is False

    def test_multiple_positions(self, svc: ConfigService) -> None:
        for i in range(3):
            svc.save_base_position(
                BaseStationPosition(
                    name=f"Site_{i}",
                    latitude=46.0 + i,
                    longitude=7.0 + i,
                    altitude_m=100.0 * i,
                )
            )
        assert len(svc.get_base_positions()) == 3
        svc.delete_base_position("Site_1")
        assert len(svc.get_base_positions()) == 2

    def test_persists_to_yaml(self, svc: ConfigService) -> None:
        pos = BaseStationPosition(
            name="Persist_Test",
            latitude=47.5,
            longitude=8.5,
            altitude_m=400.0,
            accuracy_mm=1500.0,
            source="survey_in",
        )
        svc.save_base_position(pos)

        # Create a new instance pointing to the same file
        svc2 = ConfigService(config_path=svc.config_path)
        result = svc2.get_base_position("Persist_Test")
        assert result is not None
        assert result.latitude == 47.5

    def test_save_screenshot_values_persists_to_disk(self, svc: ConfigService) -> None:
        """Regression: exact values from the 2026-05-26 Save Position bug report.

        The survey UI's Save Position dialog was silently failing for these
        values.  The root cause was an ``async`` handler nested inside a
        ``with ui.row():`` slot context manager that swallowed all
        exceptions.  This test pins the data-model + persistence path so
        any future regression in the YAML side is caught at the unit level.
        """
        pos = BaseStationPosition(
            name="test",
            latitude=32.7329015,
            longitude=-117.2362788,
            altitude_m=27.940,
            accuracy_mm=47308.0,
            source="survey_in",
        )
        svc.save_base_position(pos)

        # In-memory list reflects the save
        positions = svc.get_base_positions()
        assert len(positions) == 1
        assert positions[0].name == "test"
        assert positions[0].latitude == 32.7329015
        assert positions[0].longitude == -117.2362788
        assert positions[0].altitude_m == 27.940
        assert positions[0].accuracy_mm == 47308.0
        assert positions[0].source == "survey_in"

        # YAML file actually exists on disk
        assert svc.config_path.exists()
        raw = svc.config_path.read_text(encoding="utf-8")
        assert "test" in raw
        assert "32.7329015" in raw
        assert "-117.2362788" in raw

        # Fresh ConfigService instance reads it back identically
        svc2 = ConfigService(config_path=svc.config_path)
        roundtrip = svc2.get_base_position("test")
        assert roundtrip is not None
        assert roundtrip.latitude == 32.7329015
        assert roundtrip.longitude == -117.2362788
        assert roundtrip.altitude_m == 27.940
        assert roundtrip.accuracy_mm == 47308.0


# ---------------------------------------------------------------------------
# API: promote-survey-in
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_device_service() -> DeviceService:
    svc = MagicMock(spec=DeviceService)
    svc.is_available = True
    svc.is_connected = True
    svc.capabilities = {DeviceCapability.SURVEY_IN, DeviceCapability.FIXED_BASE}
    svc.get_status.return_value = DeviceStatus(
        state=DeviceConnectionState.CONNECTED,
    )
    return svc


@pytest.fixture()
def mock_config_service() -> ConfigService:
    svc = MagicMock(spec=ConfigService)
    svc.get_base_positions.return_value = []
    svc.get_base_position.return_value = None
    svc.get_destinations.return_value = []
    return svc


@pytest.fixture()
def promote_client(
    mock_device_service: DeviceService,
    mock_config_service: ConfigService,
) -> TestClient:
    from sp_rtk_base.services import get_config_service, get_device_service

    app = create_api_app()
    app.dependency_overrides[get_device_service] = lambda: mock_device_service
    app.dependency_overrides[get_config_service] = lambda: mock_config_service
    return TestClient(app)


class TestPromoteSurveyIn:
    """Tests for POST /api/device/promote-survey-in."""

    def test_promote_success(
        self,
        promote_client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_survey_in_status = AsyncMock(
            return_value=SurveyInProgress(
                active=False,
                valid=True,
                duration_seconds=300,
                mean_accuracy_mm=2500.0,
                observations=300,
                latitude=47.397742,
                longitude=8.545594,
                altitude_m=408.123,
            ),
        )
        mock_device_service.configure_fixed_base = AsyncMock()
        mock_device_service.save_to_flash = AsyncMock()

        resp = promote_client.post("/api/device/promote-survey-in")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "47.3977420" in data["message"]
        assert "fixed base" in data["message"].lower()

        # Verify fixed-base was configured with surveyed coords
        mock_device_service.configure_fixed_base.assert_awaited_once()
        call_args = mock_device_service.configure_fixed_base.call_args[0][0]
        assert abs(call_args.latitude - 47.397742) < 0.0001
        assert abs(call_args.longitude - 8.545594) < 0.0001
        assert abs(call_args.altitude_m - 408.123) < 0.01

        # Verify save-to-flash was called
        mock_device_service.save_to_flash.assert_awaited_once()

    def test_promote_not_valid(
        self,
        promote_client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_survey_in_status = AsyncMock(
            return_value=SurveyInProgress(active=True, valid=False),
        )
        resp = promote_client.post("/api/device/promote-survey-in")
        assert resp.status_code == 409
        assert "valid=False" in resp.json()["detail"]

    def test_promote_not_connected(
        self,
        promote_client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_survey_in_status = AsyncMock(
            side_effect=RuntimeError("Device not connected"),
        )
        resp = promote_client.post("/api/device/promote-survey-in")
        assert resp.status_code == 409

    def test_promote_missing_position(
        self,
        promote_client: TestClient,
        mock_device_service: MagicMock,
    ) -> None:
        mock_device_service.get_survey_in_status = AsyncMock(
            return_value=SurveyInProgress(
                active=False,
                valid=True,
                latitude=None,
                longitude=None,
                altitude_m=None,
            ),
        )
        resp = promote_client.post("/api/device/promote-survey-in")
        assert resp.status_code == 500
        assert "position data is missing" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# API: base-positions CRUD
# ---------------------------------------------------------------------------


class TestBasePositionsApi:
    """Tests for /api/device/base-positions endpoints."""

    def test_list_empty(
        self,
        promote_client: TestClient,
        mock_config_service: MagicMock,
    ) -> None:
        mock_config_service.get_base_positions.return_value = []
        resp = promote_client.get("/api/device/base-positions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_positions(
        self,
        promote_client: TestClient,
        mock_config_service: MagicMock,
    ) -> None:
        mock_config_service.get_base_positions.return_value = [
            BaseStationPosition(
                name="Office",
                latitude=47.0,
                longitude=8.0,
                altitude_m=400.0,
            ),
        ]
        resp = promote_client.get("/api/device/base-positions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Office"

    def test_save_position(
        self,
        promote_client: TestClient,
        mock_config_service: MagicMock,
    ) -> None:
        resp = promote_client.post(
            "/api/device/base-positions",
            json={
                "name": "New_Site",
                "latitude": 47.5,
                "longitude": 8.5,
                "altitude_m": 500.0,
                "accuracy_mm": 2000.0,
                "source": "survey_in",
            },
        )
        assert resp.status_code == 201
        assert "New_Site" in resp.json()["message"]
        mock_config_service.save_base_position.assert_called_once()

    def test_delete_position(
        self,
        promote_client: TestClient,
        mock_config_service: MagicMock,
    ) -> None:
        mock_config_service.delete_base_position.return_value = True
        resp = promote_client.delete("/api/device/base-positions/Office")
        assert resp.status_code == 200
        assert "Office" in resp.json()["message"]

    def test_delete_not_found(
        self,
        promote_client: TestClient,
        mock_config_service: MagicMock,
    ) -> None:
        mock_config_service.delete_base_position.return_value = False
        resp = promote_client.delete("/api/device/base-positions/Missing")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# API: restore position
# ---------------------------------------------------------------------------


class TestRestoreBasePosition:
    """Tests for POST /api/device/base-positions/{name}/restore."""

    def test_restore_success(
        self,
        promote_client: TestClient,
        mock_device_service: MagicMock,
        mock_config_service: MagicMock,
    ) -> None:
        mock_config_service.get_base_position.return_value = BaseStationPosition(
            name="Office",
            latitude=47.397742,
            longitude=8.545594,
            altitude_m=408.0,
            accuracy_mm=2500.0,
        )
        mock_device_service.configure_fixed_base = AsyncMock()
        mock_device_service.save_to_flash = AsyncMock()

        resp = promote_client.post("/api/device/base-positions/Office/restore")
        assert resp.status_code == 200
        assert "Restored" in resp.json()["message"]

        mock_device_service.configure_fixed_base.assert_awaited_once()
        mock_device_service.save_to_flash.assert_awaited_once()

    def test_restore_not_found(
        self,
        promote_client: TestClient,
        mock_config_service: MagicMock,
    ) -> None:
        mock_config_service.get_base_position.return_value = None
        resp = promote_client.post("/api/device/base-positions/Missing/restore")
        assert resp.status_code == 404

    def test_restore_device_error(
        self,
        promote_client: TestClient,
        mock_device_service: MagicMock,
        mock_config_service: MagicMock,
    ) -> None:
        mock_config_service.get_base_position.return_value = BaseStationPosition(
            name="Office",
            latitude=47.0,
            longitude=8.0,
            altitude_m=400.0,
        )
        mock_device_service.configure_fixed_base = AsyncMock(
            side_effect=RuntimeError("Device not connected"),
        )
        resp = promote_client.post("/api/device/base-positions/Office/restore")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Model: BaseStationPosition
# ---------------------------------------------------------------------------


class TestBaseStationPositionModel:
    """Tests for BaseStationPosition Pydantic model."""

    def test_defaults(self) -> None:
        pos = BaseStationPosition(
            name="Test",
            latitude=47.0,
            longitude=8.0,
            altitude_m=400.0,
        )
        assert pos.accuracy_mm == 0.0
        assert pos.source == "survey_in"
        assert pos.surveyed_at is None

    def test_with_all_fields(self) -> None:
        now = datetime.now(tz=timezone.utc)
        pos = BaseStationPosition(
            name="Full",
            latitude=47.5,
            longitude=8.5,
            altitude_m=500.0,
            accuracy_mm=1500.0,
            surveyed_at=now,
            source="manual",
        )
        assert pos.surveyed_at == now
        assert pos.source == "manual"

    def test_validation_latitude_range(self) -> None:
        with pytest.raises(Exception):
            BaseStationPosition(
                name="Bad",
                latitude=200.0,
                longitude=8.0,
                altitude_m=0.0,
            )

    def test_validation_longitude_range(self) -> None:
        with pytest.raises(Exception):
            BaseStationPosition(
                name="Bad",
                latitude=47.0,
                longitude=400.0,
                altitude_m=0.0,
            )

    def test_serialization_roundtrip(self) -> None:
        pos = BaseStationPosition(
            name="Roundtrip",
            latitude=47.397742,
            longitude=8.545594,
            altitude_m=408.123,
            accuracy_mm=2500.0,
        )
        data = pos.model_dump(mode="json")
        restored = BaseStationPosition.model_validate(data)
        assert restored.name == "Roundtrip"
        assert restored.latitude == 47.397742
