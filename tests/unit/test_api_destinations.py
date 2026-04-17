"""Tests for sp_base.api.destinations — destination CRUD API endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from sp_base.models.config_models import DestinationProfile
from sp_base.services.config_service import ConfigService


def _add_test_dest(config_svc: ConfigService, name: str = "rtk2go") -> None:
    """Helper: add a test destination to config."""
    config_svc.save_destination(
        DestinationProfile(
            name=name,
            type="ntrip",
            config={"caster": "rtk2go.com", "mountpoint": "MOUNT", "password": "pw"},
        )
    )


class TestListDestinations:
    """Tests for GET /api/destinations."""

    def test_empty_list(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Returns empty list when no destinations configured."""
        resp = api_client_with_services.get("/api/destinations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["destinations"] == []

    def test_list_with_destinations(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Returns all configured destinations."""
        _add_test_dest(mock_config_service, "dest1")
        _add_test_dest(mock_config_service, "dest2")

        resp = api_client_with_services.get("/api/destinations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        names = [d["name"] for d in data["destinations"]]
        assert "dest1" in names
        assert "dest2" in names


class TestGetDestination:
    """Tests for GET /api/destinations/{name}."""

    def test_get_existing(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Returns destination when it exists."""
        _add_test_dest(mock_config_service)
        resp = api_client_with_services.get("/api/destinations/rtk2go")
        assert resp.status_code == 200
        assert resp.json()["name"] == "rtk2go"
        assert resp.json()["type"] == "ntrip"

    def test_get_not_found(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Returns 404 for unknown destination."""
        resp = api_client_with_services.get("/api/destinations/missing")
        assert resp.status_code == 404


class TestCreateDestination:
    """Tests for POST /api/destinations."""

    def test_create_success(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Creates a new destination and returns 201."""
        resp = api_client_with_services.post(
            "/api/destinations",
            json={
                "name": "new-dest",
                "type": "tcp_server",
                "config": {"port": 9000},
            },
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "new-dest"
        assert resp.json()["type"] == "tcp_server"

        # Verify persisted
        dest = mock_config_service.get_destination("new-dest")
        assert dest is not None

    def test_create_duplicate(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Returns 409 when name already exists."""
        _add_test_dest(mock_config_service)
        resp = api_client_with_services.post(
            "/api/destinations",
            json={
                "name": "rtk2go",
                "type": "ntrip",
                "config": {"caster": "x", "mountpoint": "M", "password": "p"},
            },
        )
        assert resp.status_code == 409

    def test_create_default_enabled(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """New destinations are enabled by default."""
        resp = api_client_with_services.post(
            "/api/destinations",
            json={"name": "d1", "type": "tcp_server", "config": {"port": 9000}},
        )
        assert resp.status_code == 201
        assert resp.json()["enabled"] is True


class TestUpdateDestination:
    """Tests for PUT /api/destinations/{name}."""

    def test_update_enabled(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Updates the enabled field."""
        _add_test_dest(mock_config_service)
        resp = api_client_with_services.put(
            "/api/destinations/rtk2go",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_update_not_found(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Returns 404 for unknown destination."""
        resp = api_client_with_services.put(
            "/api/destinations/missing",
            json={"enabled": False},
        )
        assert resp.status_code == 404

    def test_partial_update_preserves_fields(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Partial update preserves unset fields."""
        _add_test_dest(mock_config_service)
        resp = api_client_with_services.put(
            "/api/destinations/rtk2go",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        # Type should be preserved
        assert resp.json()["type"] == "ntrip"


class TestDeleteDestination:
    """Tests for DELETE /api/destinations/{name}."""

    def test_delete_existing(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Deletes an existing destination."""
        _add_test_dest(mock_config_service)
        resp = api_client_with_services.delete("/api/destinations/rtk2go")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify removed
        assert mock_config_service.get_destination("rtk2go") is None

    def test_delete_not_found(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Returns 404 for unknown destination."""
        resp = api_client_with_services.delete("/api/destinations/missing")
        assert resp.status_code == 404


class TestCreateDestinationErrors:
    """Error branch tests for POST /api/destinations."""

    def test_create_with_invalid_type_returns_400(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Returns 400 when destination type is invalid."""
        resp = api_client_with_services.post(
            "/api/destinations",
            json={
                "name": "bad-dest",
                "type": "invalid_type_xyz",
                "config": {},
            },
        )
        # Pydantic validation for Literal type should cause a 400
        # or the DestinationProfile validation fails
        assert resp.status_code in (400, 422)


class TestUpdateDestinationErrors:
    """Error branch tests for PUT /api/destinations/{name}."""

    def test_update_config_field(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Updates the config field of a destination."""
        _add_test_dest(mock_config_service)
        resp = api_client_with_services.put(
            "/api/destinations/rtk2go",
            json={"config": {"caster": "new-caster.com", "mountpoint": "NEW", "password": "pw2"}},
        )
        assert resp.status_code == 200
        assert resp.json()["config"]["caster"] == "new-caster.com"

    def test_update_filter_field(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Updates the filter field of a destination."""
        _add_test_dest(mock_config_service)
        resp = api_client_with_services.put(
            "/api/destinations/rtk2go",
            json={"filter": {"mode": "allowlist", "message_ids": [1005, 1077]}},
        )
        assert resp.status_code == 200
        assert resp.json()["filter"]["mode"] == "allowlist"
