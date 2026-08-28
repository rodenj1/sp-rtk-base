"""Tests for sp_rtk_base.api.profiles — profile CRUD API endpoints."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.device_models import DeviceInfo
from sp_rtk_base.models.hardware_identity import HardwareConfidence
from sp_rtk_base.services import get_device_service, get_profile_store
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.profile_store import ProfileStore

BUILTIN_NAME = "ublox-f9p-base-standard"


@pytest.fixture()
def mock_device_service() -> DeviceService:
    """Create a mock DeviceService, disconnected (device_info=None) by default."""
    svc = MagicMock(spec=DeviceService)
    svc.device_info = None
    return svc


@pytest.fixture()
def api_client_with_device(
    mock_config_service: ConfigService,
    mock_relay_service: MagicMock,
    mock_event_bridge: MagicMock,
    mock_metrics_service: MetricsService,
    mock_profile_store: ProfileStore,
    mock_device_service: DeviceService,
) -> TestClient:
    """``api_client_with_services`` plus an overridable device service."""
    from sp_rtk_base.services import (
        get_config_service,
        get_event_bridge,
        get_metrics_service,
        get_relay_service,
    )

    app = create_api_app()
    app.dependency_overrides[get_config_service] = lambda: mock_config_service
    app.dependency_overrides[get_relay_service] = lambda: mock_relay_service
    app.dependency_overrides[get_event_bridge] = lambda: mock_event_bridge
    app.dependency_overrides[get_metrics_service] = lambda: mock_metrics_service
    app.dependency_overrides[get_profile_store] = lambda: mock_profile_store
    app.dependency_overrides[get_device_service] = lambda: mock_device_service
    return TestClient(app)


def _profile_payload(name: str = "my-custom", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "version": 1,
        "hardware": "ZED-F9P",
        "data_link_port": ["UART1"],
        "rtcm_stream": {"matrix": {"1005": {"UART1": True}}},
    }
    payload.update(overrides)
    return payload


class TestListProfiles:
    def test_lists_builtin_only_when_no_customs(
        self, api_client_with_services: TestClient
    ) -> None:
        resp = api_client_with_services.get("/api/profiles")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["profiles"][0]["profile"]["name"] == BUILTIN_NAME
        assert data["profiles"][0]["is_builtin"] is True

    def test_builtins_before_customs_alphabetical(
        self, api_client_with_services: TestClient, mock_profile_store: ProfileStore
    ) -> None:
        api_client_with_services.post("/api/profiles", json=_profile_payload("zeta"))
        api_client_with_services.post("/api/profiles", json=_profile_payload("alpha"))

        resp = api_client_with_services.get("/api/profiles")
        names = [item["profile"]["name"] for item in resp.json()["profiles"]]
        assert names == [BUILTIN_NAME, "alpha", "zeta"]


class TestGetProfile:
    def test_get_existing(self, api_client_with_services: TestClient) -> None:
        resp = api_client_with_services.get(f"/api/profiles/{BUILTIN_NAME}")
        assert resp.status_code == 200
        assert resp.json()["profile"]["name"] == BUILTIN_NAME
        assert resp.json()["is_builtin"] is True

    def test_get_not_found(self, api_client_with_services: TestClient) -> None:
        resp = api_client_with_services.get("/api/profiles/does-not-exist")
        assert resp.status_code == 404


class TestCreateProfile:
    def test_create_returns_201(self, api_client_with_services: TestClient) -> None:
        resp = api_client_with_services.post("/api/profiles", json=_profile_payload())
        assert resp.status_code == 201
        assert resp.json()["profile"]["name"] == "my-custom"
        assert resp.json()["is_builtin"] is False

    def test_create_conflict_with_builtin_is_409(
        self, api_client_with_services: TestClient
    ) -> None:
        resp = api_client_with_services.post(
            "/api/profiles", json=_profile_payload(BUILTIN_NAME)
        )
        assert resp.status_code == 409

    def test_create_conflict_with_custom_is_409(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post("/api/profiles", json=_profile_payload())
        resp = api_client_with_services.post("/api/profiles", json=_profile_payload())
        assert resp.status_code == 409

    def test_create_schema_violation_is_422(
        self, api_client_with_services: TestClient
    ) -> None:
        payload = _profile_payload()
        del payload["data_link_port"]
        resp = api_client_with_services.post("/api/profiles", json=payload)
        assert resp.status_code == 422

    def test_create_unsafe_name_is_400(
        self, api_client_with_services: TestClient
    ) -> None:
        resp = api_client_with_services.post(
            "/api/profiles", json=_profile_payload("has/slash")
        )
        assert resp.status_code == 400


class TestRenameProfile:
    def test_rename_returns_updated_profile(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post(
            "/api/profiles", json=_profile_payload("old-name")
        )
        resp = api_client_with_services.patch(
            "/api/profiles/old-name", json={"new_name": "new-name"}
        )
        assert resp.status_code == 200
        assert resp.json()["profile"]["name"] == "new-name"

    def test_rename_builtin_is_403(self, api_client_with_services: TestClient) -> None:
        resp = api_client_with_services.patch(
            f"/api/profiles/{BUILTIN_NAME}", json={"new_name": "something-else"}
        )
        assert resp.status_code == 403

    def test_rename_unknown_is_404(self, api_client_with_services: TestClient) -> None:
        resp = api_client_with_services.patch(
            "/api/profiles/does-not-exist", json={"new_name": "something-else"}
        )
        assert resp.status_code == 404

    def test_rename_to_colliding_name_is_409(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post("/api/profiles", json=_profile_payload("first"))
        api_client_with_services.post("/api/profiles", json=_profile_payload("second"))
        resp = api_client_with_services.patch(
            "/api/profiles/first", json={"new_name": "second"}
        )
        assert resp.status_code == 409

    def test_rename_to_unsafe_name_is_400(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post(
            "/api/profiles", json=_profile_payload("old-name")
        )
        resp = api_client_with_services.patch(
            "/api/profiles/old-name", json={"new_name": "has/slash"}
        )
        assert resp.status_code == 400

    def test_rename_to_same_name_is_a_noop_not_a_conflict(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post(
            "/api/profiles", json=_profile_payload("same-name")
        )
        resp = api_client_with_services.patch(
            "/api/profiles/same-name", json={"new_name": "same-name"}
        )
        assert resp.status_code == 200
        assert resp.json()["profile"]["name"] == "same-name"


class TestDeleteProfile:
    def test_delete_returns_ok(self, api_client_with_services: TestClient) -> None:
        api_client_with_services.post("/api/profiles", json=_profile_payload())
        resp = api_client_with_services.delete("/api/profiles/my-custom")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_delete_builtin_is_403(self, api_client_with_services: TestClient) -> None:
        resp = api_client_with_services.delete(f"/api/profiles/{BUILTIN_NAME}")
        assert resp.status_code == 403

    def test_delete_unknown_is_404(self, api_client_with_services: TestClient) -> None:
        resp = api_client_with_services.delete("/api/profiles/does-not-exist")
        assert resp.status_code == 404


class TestExportImport:
    def test_export_builtin(self, api_client_with_services: TestClient) -> None:
        resp = api_client_with_services.get(f"/api/profiles/{BUILTIN_NAME}/export")
        assert resp.status_code == 200
        assert resp.json()["name"] == BUILTIN_NAME

    def test_export_unknown_is_404(self, api_client_with_services: TestClient) -> None:
        resp = api_client_with_services.get("/api/profiles/does-not-exist/export")
        assert resp.status_code == 404

    def test_roundtrip_export_then_import(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post("/api/profiles", json=_profile_payload("source"))
        exported = api_client_with_services.get("/api/profiles/source/export").json()
        api_client_with_services.delete("/api/profiles/source")

        resp = api_client_with_services.post("/api/profiles/import", json=exported)
        assert resp.status_code == 201
        assert resp.json()["profile"]["name"] == "source"

    def test_import_unknown_version_is_422(
        self, api_client_with_services: TestClient
    ) -> None:
        payload = _profile_payload("importee")
        payload["version"] = 999
        resp = api_client_with_services.post("/api/profiles/import", json=payload)
        assert resp.status_code == 422

    def test_import_conflicting_name_is_409(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post("/api/profiles", json=_profile_payload("dup"))
        resp = api_client_with_services.post(
            "/api/profiles/import", json=_profile_payload("dup")
        )
        assert resp.status_code == 409

    def test_import_unsafe_name_is_400(
        self, api_client_with_services: TestClient
    ) -> None:
        resp = api_client_with_services.post(
            "/api/profiles/import", json=_profile_payload("has/slash")
        )
        assert resp.status_code == 400


class TestMalformedCustomFileDoesNotBreakListing:
    def test_bad_file_skipped_others_still_list(
        self, api_client_with_services: TestClient, mock_profile_store: ProfileStore
    ) -> None:
        api_client_with_services.post("/api/profiles", json=_profile_payload("good"))
        bad_path = mock_profile_store.profiles_dir / "corrupt.yaml"
        bad_path.write_text("{not valid yaml::", encoding="utf-8")

        resp = api_client_with_services.get("/api/profiles")
        assert resp.status_code == 200
        names = [item["profile"]["name"] for item in resp.json()["profiles"]]
        assert "good" in names
        assert len(names) == 2  # builtin + good; corrupt.yaml silently skipped


class TestHardwareIdentityInListing:
    """GET /api/profiles carries the resolved receiver identity and tags
    each profile with compatibility against it (issue #60)."""

    def test_no_device_is_unknown_with_no_default_and_incompatible_builtin(
        self, api_client_with_device: TestClient
    ) -> None:
        resp = api_client_with_device.get("/api/profiles")
        assert resp.status_code == 200
        data = resp.json()
        assert data["hardware_target"] == "unknown"
        assert data["hardware_confidence"] == "unknown"
        assert data["default_selection"] is None

        builtin = next(
            item for item in data["profiles"] if item["profile"]["name"] == BUILTIN_NAME
        )
        assert builtin["compatible"] is False
        assert builtin["incompatible_reason"] is not None

    def test_confirmed_matching_device_defaults_to_it_and_is_compatible(
        self,
        api_client_with_device: TestClient,
        mock_device_service: DeviceService,
    ) -> None:
        mock_device_service.device_info = DeviceInfo(  # type: ignore[misc]
            vendor="u-blox",
            model="ZED-F9P",
            hardware_target="ZED-F9P",
            hardware_confidence=HardwareConfidence.CONFIRMED,
        )

        resp = api_client_with_device.get("/api/profiles")
        data = resp.json()
        assert data["hardware_target"] == "ZED-F9P"
        assert data["hardware_confidence"] == "confirmed"
        assert data["default_selection"] == BUILTIN_NAME

        builtin = next(
            item for item in data["profiles"] if item["profile"]["name"] == BUILTIN_NAME
        )
        assert builtin["compatible"] is True
        assert builtin["incompatible_reason"] is None

    def test_inferred_identity_never_unlocks_a_specific_model_default(
        self,
        api_client_with_device: TestClient,
        mock_device_service: DeviceService,
    ) -> None:
        mock_device_service.device_info = DeviceInfo(  # type: ignore[misc]
            vendor="u-blox",
            model="ZED-F9P",
            hardware_target="ZED-F9P",
            hardware_confidence=HardwareConfidence.INFERRED,
        )

        resp = api_client_with_device.get("/api/profiles")
        data = resp.json()
        assert data["hardware_confidence"] == "inferred"
        assert data["default_selection"] is None

        builtin = next(
            item for item in data["profiles"] if item["profile"]["name"] == BUILTIN_NAME
        )
        assert builtin["compatible"] is False
        assert "unconfirmed" in (builtin["incompatible_reason"] or "")
