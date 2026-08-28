"""Tests for sp_rtk_base.api.profiles — profile CRUD API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from sp_rtk_base.services.profile_store import ProfileStore

BUILTIN_NAME = "ublox-f9p-base-standard"


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
