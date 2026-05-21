"""Tests for sp_rtk_base.api.settings — settings and input config API endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from sp_rtk_base.models.config_models import InputProfile
from sp_rtk_base.services.config_service import ConfigService


class TestGetSettings:
    """Tests for GET /api/settings."""

    def test_get_defaults(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Returns default settings when none configured."""
        resp = api_client_with_services.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["auto_start"] is False
        assert data["status_poll_interval"] == 2.0


class TestUpdateSettings:
    """Tests for PUT /api/settings."""

    def test_update_auto_start(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Updates auto_start setting."""
        resp = api_client_with_services.put(
            "/api/settings",
            json={"auto_start": True},
        )
        assert resp.status_code == 200
        assert resp.json()["auto_start"] is True

    def test_partial_update_preserves_defaults(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Partial update preserves unset fields."""
        resp = api_client_with_services.put(
            "/api/settings",
            json={"status_poll_interval": 5.0},
        )
        assert resp.status_code == 200
        assert resp.json()["auto_start"] is False  # Default preserved
        assert resp.json()["status_poll_interval"] == 5.0

    def test_update_persists(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Updated settings are persisted to config."""
        api_client_with_services.put(
            "/api/settings",
            json={"auto_start": True, "status_poll_interval": 3.0},
        )

        settings = mock_config_service.get_settings()
        assert settings.auto_start is True
        assert settings.status_poll_interval == 3.0


class TestGetInputConfig:
    """Tests for GET /api/input."""

    def test_no_input_configured(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Returns configured=False when no input set."""
        resp = api_client_with_services.get("/api/input")
        assert resp.status_code == 200
        assert resp.json()["configured"] is False

    def test_input_configured(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Returns input config when configured."""
        mock_config_service.save_input_config(
            InputProfile(source="tcp", config={"host": "1.2.3.4", "port": 5015})
        )
        resp = api_client_with_services.get("/api/input")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["source"] == "tcp"
        assert data["config"]["host"] == "1.2.3.4"


class TestUpdateInputConfig:
    """Tests for PUT /api/input."""

    def test_set_input_config(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Sets a new input configuration."""
        resp = api_client_with_services.put(
            "/api/input",
            json={
                "source": "serial",
                "config": {"port": "/dev/ttyUSB0", "baud_rate": 115200},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["source"] == "serial"

        # Verify persisted
        saved = mock_config_service.get_input_config()
        assert saved is not None
        assert saved.source == "serial"

    def test_update_replaces_config(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Updating input config replaces existing."""
        mock_config_service.save_input_config(
            InputProfile(source="tcp", config={"host": "1.2.3.4", "port": 5015})
        )
        resp = api_client_with_services.put(
            "/api/input",
            json={"source": "serial", "config": {"port": "/dev/ttyUSB0"}},
        )
        assert resp.status_code == 200
        assert resp.json()["source"] == "serial"
