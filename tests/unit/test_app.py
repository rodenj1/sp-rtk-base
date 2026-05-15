"""Tests for sp_rtk_base.app module — FastAPI app factory and API endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app


class TestCreateApiApp:
    """Tests for the create_api_app factory function."""

    def test_creates_fastapi_app(self) -> None:
        """create_api_app returns a FastAPI application."""
        app = create_api_app()
        assert app is not None
        assert app.title == "SP-Base API"

    def test_app_has_version(self) -> None:
        """Application version matches sp_rtk_base.__version__."""
        from sp_rtk_base import __version__

        app = create_api_app()
        assert app.version == __version__


class TestHealthEndpoint:
    """Tests for the /api/health endpoint."""

    def test_health_returns_200(self, api_client: TestClient) -> None:
        """Health check returns 200 OK."""
        response = api_client.get("/api/health")
        assert response.status_code == 200

    def test_health_returns_status_ok(self, api_client: TestClient) -> None:
        """Health check response contains status: ok."""
        response = api_client.get("/api/health")
        data = response.json()
        assert data["status"] == "ok"

    def test_health_returns_version(self, api_client: TestClient) -> None:
        """Health check response contains the application version."""
        from sp_rtk_base import __version__

        response = api_client.get("/api/health")
        data = response.json()
        assert data["version"] == __version__


class TestInitApp:
    """Tests for the init_app function."""

    def test_init_app_does_not_raise(self) -> None:
        """init_app completes without errors."""
        from sp_rtk_base.app import init_app

        # init_app registers routes on the NiceGUI app singleton.
        # Just verify it doesn't raise.
        init_app()
