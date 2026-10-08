"""``POST /api/update`` and ``GET /api/update/progress`` (sp-rtk-base#240).

An API client asks for an Update to the versions it read, and follows the
Update through ``status.json``, which the tests write as the host would.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.services import get_update_check_service, get_update_service
from sp_rtk_base.services.update_check import UpdateCheckService
from sp_rtk_base.services.update_service import UpdateService
from sp_rtk_base.update.state import UpdateFiles, UpdateStatus, Versions
from tests.fixtures.fake_github import FakeGitHub, Web
from tests.fixtures.fake_pypi import FakePyPI

CHECKED_AT = datetime(2026, 10, 7, 9, 12, tzinfo=timezone.utc)


class Console:
    connected = False


@pytest.fixture()
def pypi() -> FakePyPI:
    return FakePyPI()


@pytest.fixture()
def files(tmp_path: Path) -> UpdateFiles:
    return UpdateFiles(tmp_path / "update")


@pytest.fixture()
def console() -> Console:
    return Console()


@pytest.fixture()
def client(
    pypi: FakePyPI, files: UpdateFiles, console: Console
) -> Iterator[TestClient]:
    checker = UpdateCheckService(
        Web(pypi, FakeGitHub()),
        running_app="0.9.0",
        running_relay="4.1.0",
        python=(3, 11),
        clock=lambda: CHECKED_AT,
    )
    update = UpdateService(files, running=Versions(app="0.9.0", relay="4.1.0"))
    update.set_console_check(lambda: console.connected)
    app = create_api_app()
    app.dependency_overrides[get_update_check_service] = lambda: checker
    app.dependency_overrides[get_update_service] = lambda: update
    with TestClient(app) as test_client:
        yield test_client


def _available(client: TestClient, pypi: FakePyPI) -> dict[str, str]:
    pypi.publish_app("0.10.1")
    target = client.post("/api/update/check").json()["last_good"]["target"]
    return {"app": target["app"], "relay": target["relay"]}


class TestRequestAnUpdate:
    def test_writes_the_request_for_the_versions_read(
        self, client: TestClient, pypi: FakePyPI, files: UpdateFiles
    ) -> None:
        target = _available(client, pypi)

        response = client.post("/api/update", json=target)

        assert response.status_code == 202, response.text
        assert response.json()["status"]["phase"] == "requested"
        assert response.json()["updating"] is True
        request = json.loads(files.request_path.read_text())
        assert request["app"] == "0.10.1"
        assert request["relay"] == target["relay"]

    def test_refused_with_the_reason_while_a_console_link_is_connected(
        self,
        client: TestClient,
        pypi: FakePyPI,
        files: UpdateFiles,
        console: Console,
    ) -> None:
        target = _available(client, pypi)
        console.connected = True

        response = client.post("/api/update", json=target)

        assert response.status_code == 409
        assert response.json() == {
            "status": "error",
            "code": "console_connected",
            "message": "A Console link is connected. Disconnect it to update.",
        }
        assert not files.request_path.exists()

    def test_refused_without_an_available_update(
        self, client: TestClient, files: UpdateFiles
    ) -> None:
        response = client.post("/api/update", json={"app": "0.10.1", "relay": "4.1.0"})

        assert response.status_code == 409
        assert response.json()["code"] == "not_available"
        assert not files.request_path.exists()


class TestProgress:
    def test_no_update_yet(self, client: TestClient) -> None:
        response = client.get("/api/update/progress")

        assert response.status_code == 200
        assert response.json() == {"status": None, "updating": False}

    def test_follows_the_host(self, client: TestClient, files: UpdateFiles) -> None:
        files.write_status(
            UpdateStatus(
                phase="installing",
                from_=Versions(app="0.9.0", relay="4.1.0"),
                to=Versions(app="0.10.1", relay="4.2.0"),
            )
        )

        body = client.get("/api/update/progress").json()

        assert body["updating"] is True
        assert body["status"]["phase"] == "installing"
        assert body["status"]["from"] == {"app": "0.9.0", "relay": "4.1.0"}
        assert body["status"]["to"] == {"app": "0.10.1", "relay": "4.2.0"}
