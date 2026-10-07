"""``/api/update``: the update check, as an API client sees it."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sp_rtk_base.services as services_mod
from sp_rtk_base.app import create_api_app
from sp_rtk_base.services import get_update_check_service
from sp_rtk_base.services.update_check import UpdateCheckService
from sp_rtk_base.update.release import urllib_fetch
from tests.fixtures.fake_pypi import APP_INDEX, FakePyPI

CHECKED_AT = datetime(2026, 10, 7, 9, 12, tzinfo=timezone.utc)


@pytest.fixture()
def pypi() -> FakePyPI:
    return FakePyPI()


@pytest.fixture()
def client(pypi: FakePyPI) -> Iterator[TestClient]:
    service = UpdateCheckService(
        pypi,
        running_app="0.9.0",
        running_relay="4.1.0",
        python=(3, 11),
        clock=lambda: CHECKED_AT,
    )
    app = create_api_app()
    app.dependency_overrides[get_update_check_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client


class TestGetUpdate:
    def test_before_the_first_check(self, client: TestClient) -> None:
        response = client.get("/api/update")

        assert response.status_code == 200
        assert response.json() == {
            "last_good": None,
            "checking": False,
            "last_check_failed": False,
            "error": None,
        }

    def test_after_a_check_finds_an_available_update(
        self, client: TestClient, pypi: FakePyPI
    ) -> None:
        pypi.publish_relay("4.2.0")
        pypi.publish_app("0.10.0", relay_pin="<5,>=4.2.0")
        pypi.publish_app("0.11.0", requires_python=">=3.12")
        client.post("/api/update/check")

        body = client.get("/api/update").json()

        assert body["last_good"] == {
            "running_app": "0.9.0",
            "running_relay": "4.1.0",
            "running_python": "3.11",
            "target": {
                "app": "0.10.0",
                "relay": "4.2.0",
                "newer_needs_python": {"version": "0.11.0", "python": "3.12"},
            },
            "checked_at": "2026-10-07T09:12:00Z",
            "available": True,
        }


class TestCheckNow:
    def test_check_now_returns_the_new_result(self, client: TestClient) -> None:
        response = client.post("/api/update/check")

        assert response.status_code == 200
        body = response.json()
        assert body["last_good"]["available"] is False
        assert body["last_good"]["target"]["app"] == "0.9.0"
        assert body["last_check_failed"] is False

    def test_a_failed_check_keeps_the_last_good_result(
        self, client: TestClient, pypi: FakePyPI
    ) -> None:
        client.post("/api/update/check")
        pypi.down.add(APP_INDEX)

        body = client.post("/api/update/check").json()

        assert body["last_check_failed"] is True
        assert body["error"]
        assert body["last_good"]["target"]["app"] == "0.9.0"


class TestReleaseSource:
    def test_pypi_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SP_RTK_BASE_FAKE_PYPI_DIR", raising=False)

        assert services_mod._release_fetch() is urllib_fetch  # pyright: ignore[reportPrivateUsage]

    def test_a_fake_directory_for_e2e(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "sp-rtk-base.json").write_text("{}")
        monkeypatch.setenv("SP_RTK_BASE_FAKE_PYPI_DIR", str(tmp_path))

        fetch = services_mod._release_fetch()  # pyright: ignore[reportPrivateUsage]

        assert fetch(APP_INDEX) == b"{}"
