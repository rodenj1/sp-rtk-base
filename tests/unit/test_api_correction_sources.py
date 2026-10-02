"""Tests for the Correction sources API (issue #192).

Seam under test: ``/api/correction-sources`` over HTTP (TestClient), with a
real ConfigService writing a temporary config.yaml.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sp_rtk_base_relay.config import NtripInputConfig

from sp_rtk_base.models.config_models import (
    CorrectionSourceProfile,
    NtripCorrectionConfig,
)
from sp_rtk_base.services.config_service import ConfigService

SOURCES = "/api/correction-sources"
SECRET = "s3cret-pa55"


def _source(name: str = "rtk2go", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "caster": "rtk2go.com",
        "port": 2101,
        "mountpoint": "MP1",
        "username": "me@example.com",
        "password": SECRET,
        "version": "2.0",
        "tls": False,
    }
    body.update(overrides)
    return body


class TestCreateAndRead:
    def test_a_created_source_is_listed_and_readable_without_its_password(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        client = api_client_with_services

        created = client.post(SOURCES, json=_source())
        listed = client.get(SOURCES)
        read = client.get(f"{SOURCES}/rtk2go")

        assert created.status_code == 201
        assert created.json()["has_password"] is True
        assert read.json() == {
            "name": "rtk2go",
            "kind": "ntrip",
            "caster": "rtk2go.com",
            "port": 2101,
            "mountpoint": "MP1",
            "username": "me@example.com",
            "version": "2.0",
            "tls": False,
            "has_password": True,
        }
        assert [s["name"] for s in listed.json()["sources"]] == ["rtk2go"]
        for response in (created, listed, read):
            assert SECRET not in response.text
            assert "password" not in json.dumps(response.json()).replace(
                "has_password", ""
            )
        # The saved config keeps it, for the Relay
        saved = mock_config_service.get_correction_source("rtk2go")
        assert saved is not None
        assert saved.config.password == SECRET


class TestPassword:
    def test_a_blank_password_on_update_keeps_the_saved_one(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        client = api_client_with_services
        client.post(SOURCES, json=_source())

        for blank in ({"password": ""}, {}):
            response = client.put(f"{SOURCES}/rtk2go", json={"port": 2102, **blank})
            assert response.status_code == 200
            assert response.json()["has_password"] is True

        saved = mock_config_service.get_correction_source("rtk2go")
        assert saved is not None
        assert saved.config.password == SECRET
        assert saved.config.port == 2102

    def test_a_new_password_replaces_the_saved_one(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        api_client_with_services.post(SOURCES, json=_source())

        api_client_with_services.put(f"{SOURCES}/rtk2go", json={"password": "n3w"})

        saved = mock_config_service.get_correction_source("rtk2go")
        assert saved is not None and saved.config.password == "n3w"

    def test_removing_the_password_is_an_explicit_action(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        api_client_with_services.post(SOURCES, json=_source())

        response = api_client_with_services.put(
            f"{SOURCES}/rtk2go", json={"remove_password": True}
        )

        assert response.json()["has_password"] is False
        saved = mock_config_service.get_correction_source("rtk2go")
        assert saved is not None and saved.config.password == ""

    def test_an_anonymous_source_has_no_password(
        self, api_client_with_services: TestClient
    ) -> None:
        response = api_client_with_services.post(
            SOURCES, json=_source(username="", password="")
        )

        assert response.json()["has_password"] is False


class TestNames:
    def test_a_duplicate_name_is_refused(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post(SOURCES, json=_source())

        response = api_client_with_services.post(SOURCES, json=_source(caster="other"))

        assert response.status_code == 409
        assert response.json()["status"] == "error"

    def test_renaming_onto_another_sources_name_is_refused(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post(SOURCES, json=_source("a"))
        api_client_with_services.post(SOURCES, json=_source("b"))

        response = api_client_with_services.put(f"{SOURCES}/a", json={"name": "b"})

        assert response.status_code == 409

    def test_a_rename_keeps_the_password_and_the_last_used_choice(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        client = api_client_with_services
        client.post(SOURCES, json=_source("old"))
        mock_config_service.set_last_correction_source("old")

        response = client.put(f"{SOURCES}/old", json={"name": "new"})

        assert response.status_code == 200
        assert response.json()["has_password"] is True
        listed = client.get(SOURCES).json()
        assert [s["name"] for s in listed["sources"]] == ["new"]
        assert listed["last_used"] == "new"

    @pytest.mark.parametrize("name", ["", "has space", "slash/name", "x" * 65])
    def test_an_invalid_name_is_refused(
        self, api_client_with_services: TestClient, name: str
    ) -> None:
        response = api_client_with_services.post(SOURCES, json=_source(name))

        assert response.status_code == 422


class TestValidation:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"version": "1.0", "tls": True},  # TLS is v2 only
            {"caster": ""},
            {"mountpoint": ""},
            {"port": 0},
            {"version": "3.0"},
        ],
    )
    def test_an_invalid_source_is_refused(
        self, api_client_with_services: TestClient, overrides: dict[str, Any]
    ) -> None:
        response = api_client_with_services.post(SOURCES, json=_source(**overrides))

        assert response.status_code == 422

    def test_an_update_that_would_be_invalid_is_refused(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        api_client_with_services.post(SOURCES, json=_source(version="2.0", tls=True))

        response = api_client_with_services.put(
            f"{SOURCES}/rtk2go", json={"version": "1.0"}
        )

        assert response.status_code == 422
        saved = mock_config_service.get_correction_source("rtk2go")
        assert saved is not None and saved.config.version == "2.0"


class TestNotFoundAndDelete:
    def test_unknown_sources_are_404(
        self, api_client_with_services: TestClient
    ) -> None:
        client = api_client_with_services

        assert client.get(f"{SOURCES}/nope").status_code == 404
        assert client.put(f"{SOURCES}/nope", json={"port": 1}).status_code == 404
        assert client.delete(f"{SOURCES}/nope").status_code == 404

    def test_deleting_the_last_used_source_stops_preselecting_it(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        client = api_client_with_services
        client.post(SOURCES, json=_source())
        mock_config_service.set_last_correction_source("rtk2go")

        assert client.delete(f"{SOURCES}/rtk2go").status_code == 200

        listed = client.get(SOURCES).json()
        assert listed == {"sources": [], "count": 0, "last_used": None}


class TestStorage:
    def test_only_the_operators_fields_are_stored_and_survive_a_reload(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
        config_path: Path,
    ) -> None:
        api_client_with_services.post(SOURCES, json=_source(tls=True))

        stored = yaml.safe_load(config_path.read_text())["correction_sources"]
        assert stored == [
            {
                "name": "rtk2go",
                "kind": "ntrip",
                "config": {
                    "caster": "rtk2go.com",
                    "port": 2101,
                    "mountpoint": "MP1",
                    "username": "me@example.com",
                    "password": SECRET,
                    "version": "2.0",
                    "tls": True,
                },
            }
        ]
        reloaded = ConfigService(config_path=config_path).get_correction_source(
            "rtk2go"
        )
        assert reloaded == mock_config_service.get_correction_source("rtk2go")

    def test_an_invalid_saved_source_is_dropped_on_load_not_fatal(
        self, config_path: Path
    ) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump(
                {
                    "correction_sources": [
                        {
                            "name": "bad name",
                            "config": {"caster": "x", "mountpoint": "y"},
                        },
                        {"name": "good", "config": {"caster": "x", "mountpoint": "y"}},
                    ]
                }
            )
        )

        sources = ConfigService(config_path=config_path).get_correction_sources()

        assert [s.name for s in sources] == ["good"]

    def test_the_relay_input_uses_the_relays_own_defaults_for_the_rest(self) -> None:
        source = CorrectionSourceProfile(
            name="rtk2go",
            config=NtripCorrectionConfig(caster="rtk2go.com", mountpoint="MP1"),
        )

        relay = source.to_relay_config().get_ntrip_config()

        defaults = NtripInputConfig(caster="c", mountpoint="m")
        assert relay.connection_timeout == defaults.connection_timeout
        assert relay.data_timeout == defaults.data_timeout
        assert (relay.retry_initial_delay, relay.retry_max_delay) == (
            defaults.retry_initial_delay,
            defaults.retry_max_delay,
        )
        assert (relay.caster, relay.mountpoint, relay.version) == (
            "rtk2go.com",
            "MP1",
            "2.0",
        )


class TestErrorsNeverEchoThePassword:
    @pytest.mark.parametrize(
        "overrides",
        [{"version": "1.0", "tls": True}, {"caster": ""}, {"port": 0}],
    )
    def test_a_refused_create_does_not_echo_the_password(
        self, api_client_with_services: TestClient, overrides: dict[str, Any]
    ) -> None:
        response = api_client_with_services.post(SOURCES, json=_source(**overrides))

        assert response.status_code == 422
        assert SECRET not in response.text

    def test_a_refused_update_does_not_echo_the_password(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post(SOURCES, json=_source(tls=True))

        response = api_client_with_services.put(
            f"{SOURCES}/rtk2go", json={"version": "1.0", "password": "an0ther"}
        )

        assert response.status_code == 422
        assert SECRET not in response.text
        assert "an0ther" not in response.text

    def test_an_invalid_password_value_itself_is_not_echoed(
        self, api_client_with_services: TestClient
    ) -> None:
        # The kind of error whose message quotes the input value directly
        response = api_client_with_services.post(
            SOURCES, json=_source(password=[SECRET])
        )

        assert response.status_code == 422
        assert SECRET not in response.text


class TestConfigExportAndImport:
    def test_the_export_carries_no_password_only_whether_one_is_saved(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post(SOURCES, json=_source())

        exported = api_client_with_services.get("/api/config/export")

        assert exported.status_code == 200
        assert SECRET not in exported.text
        source = yaml.safe_load(exported.text)["correction_sources"][0]
        assert source["config"]["password"] == ""
        assert source["config"]["has_password"] is True

    def test_importing_the_export_keeps_the_saved_password(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        client = api_client_with_services
        client.post(SOURCES, json=_source())
        exported = client.get("/api/config/export").text

        imported = client.post(
            "/api/config/import", files={"file": ("config.yaml", exported)}
        )

        assert imported.status_code == 200
        saved = mock_config_service.get_correction_source("rtk2go")
        assert saved is not None and saved.config.password == SECRET

    def test_an_imported_password_replaces_the_saved_one(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        client = api_client_with_services
        client.post(SOURCES, json=_source())
        data = yaml.safe_load(client.get("/api/config/export").text)
        data["correction_sources"][0]["config"]["password"] = "fr0m-file"

        client.post(
            "/api/config/import", files={"file": ("config.yaml", yaml.safe_dump(data))}
        )

        saved = mock_config_service.get_correction_source("rtk2go")
        assert saved is not None and saved.config.password == "fr0m-file"

    def test_an_import_with_duplicate_source_names_is_refused(
        self, api_client_with_services: TestClient
    ) -> None:
        source = {"name": "dup", "config": {"caster": "c", "mountpoint": "m"}}
        text = yaml.safe_dump({"correction_sources": [source, source]})

        response = api_client_with_services.post(
            "/api/config/import", files={"file": ("config.yaml", text)}
        )

        assert response.status_code == 400
        assert "dup" in response.text

    def test_a_refused_import_does_not_echo_the_password(
        self, api_client_with_services: TestClient
    ) -> None:
        bad = {
            "name": "x",
            "config": {
                "password": SECRET,
                "caster": "c",
                "mountpoint": "m",
                "version": "1.0",
                "tls": True,
            },
        }

        response = api_client_with_services.post(
            "/api/config/import",
            files={
                "file": ("config.yaml", yaml.safe_dump({"correction_sources": [bad]}))
            },
        )

        assert response.status_code == 400
        assert SECRET not in response.text

    def test_a_request_missing_a_field_does_not_echo_the_password(
        self, api_client_with_services: TestClient
    ) -> None:
        response = api_client_with_services.post(
            SOURCES, json={"name": "x", "mountpoint": "m", "password": SECRET}
        )

        assert response.status_code == 422
        assert SECRET not in response.text


class TestReviewFixes:
    def test_a_typed_password_wins_over_removing_the_saved_one(
        self,
        api_client_with_services: TestClient,
        mock_config_service: ConfigService,
    ) -> None:
        api_client_with_services.post(SOURCES, json=_source())

        api_client_with_services.put(
            f"{SOURCES}/rtk2go", json={"remove_password": True, "password": "n3w"}
        )

        saved = mock_config_service.get_correction_source("rtk2go")
        assert saved is not None and saved.config.password == "n3w"

    def test_renaming_to_an_empty_name_is_refused(
        self, api_client_with_services: TestClient
    ) -> None:
        api_client_with_services.post(SOURCES, json=_source())

        response = api_client_with_services.put(f"{SOURCES}/rtk2go", json={"name": ""})

        assert response.status_code == 422
