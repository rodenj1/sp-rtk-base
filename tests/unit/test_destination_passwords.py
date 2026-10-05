"""Tests for write-only destination passwords and the config file's mode (#181).

Seams under test: the destinations API and the config export/import over HTTP
(TestClient, a real ConfigService on a temp file), and ConfigService's save
for the file mode.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from sp_rtk_base.models.config_models import DestinationProfile
from sp_rtk_base.services.config_service import ConfigService

SECRET = "s3cret-pa55"
DESTS = "/api/destinations"


def _ntrip(name: str = "caster", password: str = SECRET) -> DestinationProfile:
    return DestinationProfile(
        name=name,
        type="ntrip",
        config={
            "caster": "rtk2go.com",
            "mountpoint": "MP1",
            "username": "me",
            "password": password,
            "version": "2.0",
        },
    )


def _surepath(name: str = "sp") -> DestinationProfile:
    return DestinationProfile(
        name=name,
        type="surepath",
        config={"host": "sp.example.com", "username": "me", "password": SECRET},
    )


def _saved_password(config_svc: ConfigService, name: str) -> Any:
    dest = config_svc.get_destination(name)
    assert dest is not None
    return dest.config.get("password")


class TestResponsesNeverCarryThePassword:
    def test_list_and_get_say_only_whether_one_is_saved(
        self, api_client_with_services: TestClient, mock_config_service: ConfigService
    ) -> None:
        mock_config_service.save_destination(_ntrip())
        mock_config_service.save_destination(_surepath())
        mock_config_service.save_destination(_ntrip("anon", password=""))

        listed = api_client_with_services.get(DESTS)
        one = api_client_with_services.get(f"{DESTS}/caster")

        assert SECRET not in listed.text and SECRET not in one.text
        by_name = {d["name"]: d for d in listed.json()["destinations"]}
        assert by_name["caster"]["has_password"] is True
        assert by_name["sp"]["has_password"] is True
        assert by_name["anon"]["has_password"] is False
        assert "password" not in one.json()["config"]

    def test_create_and_update_answers_dont_carry_it(
        self, api_client_with_services: TestClient
    ) -> None:
        created = api_client_with_services.post(
            DESTS, json=_ntrip().model_dump(exclude={"filter"})
        )
        updated = api_client_with_services.put(
            f"{DESTS}/caster", json={"config": {"caster": "other.example.com"}}
        )

        assert created.status_code == 201 and SECRET not in created.text
        assert updated.status_code == 200 and SECRET not in updated.text

    def test_a_refused_request_doesnt_echo_it(
        self, api_client_with_services: TestClient
    ) -> None:
        # Malformed (no type, a bad "enabled"): FastAPI would quote the body.
        body = {"name": "x", "enabled": "maybe", "config": {"password": SECRET}}
        response = api_client_with_services.post(DESTS, json=body)

        assert response.status_code == 422
        assert SECRET not in response.text


class TestEditing:
    def test_leaving_the_password_out_keeps_the_saved_one(
        self, api_client_with_services: TestClient, mock_config_service: ConfigService
    ) -> None:
        mock_config_service.save_destination(_ntrip())

        api_client_with_services.put(
            f"{DESTS}/caster",
            json={"config": {"caster": "other.example.com", "mountpoint": "MP2"}},
        )

        assert _saved_password(mock_config_service, "caster") == SECRET

    def test_a_blank_password_keeps_the_saved_one(
        self, api_client_with_services: TestClient, mock_config_service: ConfigService
    ) -> None:
        mock_config_service.save_destination(_surepath())

        api_client_with_services.put(
            f"{DESTS}/sp",
            json={
                "config": {"host": "sp.example.com", "username": "me", "password": ""}
            },
        )

        assert _saved_password(mock_config_service, "sp") == SECRET

    def test_a_new_password_replaces_it(
        self, api_client_with_services: TestClient, mock_config_service: ConfigService
    ) -> None:
        mock_config_service.save_destination(_ntrip())

        api_client_with_services.put(
            f"{DESTS}/caster",
            json={
                "config": {
                    "caster": "rtk2go.com",
                    "mountpoint": "MP1",
                    "password": "new",
                }
            },
        )

        assert _saved_password(mock_config_service, "caster") == "new"

    def test_remove_password_clears_it(
        self, api_client_with_services: TestClient, mock_config_service: ConfigService
    ) -> None:
        mock_config_service.save_destination(_ntrip())

        response = api_client_with_services.put(
            f"{DESTS}/caster", json={"remove_password": True}
        )

        assert response.json()["has_password"] is False
        assert not _saved_password(mock_config_service, "caster")


class TestExportImport:
    def test_the_export_says_whether_one_is_saved_and_importing_keeps_it(
        self, api_client_with_services: TestClient, mock_config_service: ConfigService
    ) -> None:
        mock_config_service.save_destination(_ntrip())

        exported = api_client_with_services.get("/api/config/export").text
        assert SECRET not in exported
        dest = yaml.safe_load(exported)["destinations"][0]["config"]
        assert dest["has_password"] is True

        imported = api_client_with_services.post(
            "/api/config/import",
            files={"file": ("config.yaml", exported, "application/x-yaml")},
        )

        assert imported.status_code == 200
        assert _saved_password(mock_config_service, "caster") == SECRET
        saved = mock_config_service.get_destination("caster")
        assert saved is not None and "has_password" not in saved.config


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestConfigFileMode:
    def test_a_new_config_file_is_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "sub" / "config.yaml"
        service = ConfigService(config_path=path)

        service.save_destination(_ntrip())

        assert _mode(path) == 0o600

    def test_a_wider_existing_file_is_tightened_on_save(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("destinations: []\n")
        path.chmod(0o644)
        service = ConfigService(config_path=path)

        service.save_destination(_ntrip())

        assert _mode(path) == 0o600
        assert SECRET in path.read_text()  # still saved, just private


class TestReviewFixes:
    def test_importing_a_destination_that_changed_type_takes_the_import(
        self, api_client_with_services: TestClient, mock_config_service: ConfigService
    ) -> None:
        mock_config_service.save_destination(_ntrip("a"))
        imported = yaml.safe_dump(
            {
                "destinations": [
                    {
                        "name": "a",
                        "type": "tcp_server",
                        "enabled": False,
                        "config": {"port": 5000},
                    }
                ]
            }
        )

        response = api_client_with_services.post(
            "/api/config/import",
            files={"file": ("config.yaml", imported, "application/x-yaml")},
        )

        assert response.status_code == 200
        dest = mock_config_service.get_destination("a")
        assert dest is not None
        assert (dest.type, dest.enabled) == ("tcp_server", False)
        assert "password" not in dest.config  # never carried across types

    def test_editing_after_removing_the_password_keeps_the_field(
        self, api_client_with_services: TestClient, mock_config_service: ConfigService
    ) -> None:
        mock_config_service.save_destination(_ntrip())
        api_client_with_services.put(f"{DESTS}/caster", json={"remove_password": True})

        api_client_with_services.put(
            f"{DESTS}/caster",
            json={
                "config": {
                    "caster": "rtk2go.com",
                    "mountpoint": "MP2",
                    "username": "me",
                    "version": "2.0",
                }
            },
        )

        dest = mock_config_service.get_destination("caster")
        assert dest is not None
        assert dest.config.get("password") == ""  # the field is kept
        # It can't run without one, and says so (the card, and Relay start).
        assert dest.cannot_run_reason is not None
        assert "no password" in dest.cannot_run_reason

    def test_a_symlinked_config_stays_a_symlink(self, tmp_path: Path) -> None:
        real = tmp_path / "real" / "config.yaml"
        real.parent.mkdir()
        real.write_text("destinations: []\n")
        link = tmp_path / "config.yaml"
        link.symlink_to(real)
        service = ConfigService(config_path=link)

        service.save_destination(_ntrip())

        assert link.is_symlink()
        assert _mode(real) == 0o600
        assert SECRET in real.read_text()
