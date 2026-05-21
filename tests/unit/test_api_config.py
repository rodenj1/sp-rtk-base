"""Tests for config import/export API endpoints."""

from __future__ import annotations

import io

import yaml
from fastapi.testclient import TestClient

from sp_rtk_base.models.config_models import (
    AppConfig,
    AppSettings,
    DestinationProfile,
    InputProfile,
)
from sp_rtk_base.services.config_service import ConfigService


class TestExportConfig:
    """Tests for GET /api/config/export."""

    def test_export_default_config(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Export returns valid YAML with correct headers."""
        resp = api_client_with_services.get("/api/config/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-yaml"
        assert "sp-rtk-base-config.yaml" in resp.headers["content-disposition"]

        data = yaml.safe_load(resp.text)
        assert isinstance(data, dict)
        assert "destinations" in data
        assert "settings" in data

    def test_export_with_destinations(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Export includes saved destinations and input config."""
        config = AppConfig(
            input=InputProfile(
                source="tcp", config={"host": "127.0.0.1", "port": 5015}
            ),
            destinations=[
                DestinationProfile(
                    name="rtk2go",
                    type="ntrip",
                    config={"host": "rtk2go.com", "port": 2101},
                ),
            ],
            settings=AppSettings(auto_start=True),
        )
        mock_config_service.save_config(config)

        resp = api_client_with_services.get("/api/config/export")
        assert resp.status_code == 200

        data = yaml.safe_load(resp.text)
        assert len(data["destinations"]) == 1
        assert data["destinations"][0]["name"] == "rtk2go"
        assert data["input"]["source"] == "tcp"
        assert data["settings"]["auto_start"] is True


class TestImportConfig:
    """Tests for POST /api/config/import."""

    def test_import_valid_config(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Import valid YAML config succeeds and persists."""
        config_data = {
            "destinations": [
                {
                    "name": "test-dest",
                    "type": "tcp_server",
                    "config": {"host": "0.0.0.0", "port": 9000},
                },
            ],
            "settings": {"auto_start": False},
        }
        yaml_text = yaml.dump(config_data)

        resp = api_client_with_services.post(
            "/api/config/import",
            files={
                "file": (
                    "config.yaml",
                    io.BytesIO(yaml_text.encode()),
                    "application/x-yaml",
                )
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "1 destinations" in body["message"]

        # Verify it was saved
        saved = mock_config_service.get_config()
        assert len(saved.destinations) == 1
        assert saved.destinations[0].name == "test-dest"

    def test_import_with_input_config(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Import with input config reports 'configured'."""
        config_data: dict[str, object] = {
            "input": {"source": "tcp", "config": {"host": "192.168.1.1", "port": 5015}},
            "destinations": [],
            "settings": {},
        }
        yaml_text = yaml.dump(config_data)

        resp = api_client_with_services.post(
            "/api/config/import",
            files={
                "file": (
                    "config.yaml",
                    io.BytesIO(yaml_text.encode()),
                    "application/x-yaml",
                )
            },
        )
        assert resp.status_code == 200
        assert "input=configured" in resp.json()["message"]

    def test_import_empty_file_returns_400(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Import an empty file returns 400."""
        resp = api_client_with_services.post(
            "/api/config/import",
            files={"file": ("config.yaml", io.BytesIO(b""), "application/x-yaml")},
        )
        assert resp.status_code == 400
        assert "empty" in resp.json()["detail"].lower()

    def test_import_invalid_yaml_returns_400(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Import invalid YAML returns 400."""
        bad_yaml = b"destinations:\n  - name: foo\n    type: [invalid unclosed"
        resp = api_client_with_services.post(
            "/api/config/import",
            files={"file": ("config.yaml", io.BytesIO(bad_yaml), "application/x-yaml")},
        )
        assert resp.status_code == 400
        assert "Invalid YAML" in resp.json()["detail"]

    def test_import_non_dict_yaml_returns_400(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Import YAML that is not a mapping returns 400."""
        resp = api_client_with_services.post(
            "/api/config/import",
            files={
                "file": (
                    "config.yaml",
                    io.BytesIO(b"- item1\n- item2"),
                    "application/x-yaml",
                )
            },
        )
        assert resp.status_code == 400
        assert "mapping" in resp.json()["detail"].lower()

    def test_import_invalid_schema_returns_400(
        self,
        api_client_with_services: TestClient,
    ) -> None:
        """Import YAML with invalid schema returns 400."""
        bad_schema = yaml.dump({"destinations": "not_a_list"})
        resp = api_client_with_services.post(
            "/api/config/import",
            files={
                "file": (
                    "config.yaml",
                    io.BytesIO(bad_schema.encode()),
                    "application/x-yaml",
                )
            },
        )
        assert resp.status_code == 400
        assert "schema" in resp.json()["detail"].lower()

    def test_export_import_roundtrip(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        """Export → import roundtrip preserves configuration."""
        config = AppConfig(
            input=InputProfile(
                source="serial", config={"port": "/dev/ttyACM0", "baud_rate": 115200}
            ),
            destinations=[
                DestinationProfile(
                    name="surepath",
                    type="surepath",
                    config={"host": "surepath.example.com", "port": 2101},
                ),
                DestinationProfile(
                    name="tcp-out",
                    type="tcp_server",
                    config={"host": "0.0.0.0", "port": 9000},
                ),
            ],
            settings=AppSettings(auto_start=True, metrics_enabled=False),
        )
        mock_config_service.save_config(config)

        # Export
        export_resp = api_client_with_services.get("/api/config/export")
        assert export_resp.status_code == 200
        exported_yaml = export_resp.content

        # Clear config
        mock_config_service.save_config(AppConfig())

        # Import the exported YAML
        import_resp = api_client_with_services.post(
            "/api/config/import",
            files={
                "file": ("config.yaml", io.BytesIO(exported_yaml), "application/x-yaml")
            },
        )
        assert import_resp.status_code == 200

        # Verify roundtrip
        restored = mock_config_service.get_config()
        assert len(restored.destinations) == 2
        assert restored.destinations[0].name == "surepath"
        assert restored.destinations[1].name == "tcp-out"
        assert restored.input is not None
        assert restored.input.source == "serial"
        assert restored.settings.auto_start is True
        assert restored.settings.metrics_enabled is False
