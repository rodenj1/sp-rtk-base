"""``GET /api/portal/identity`` (ticket #86, spec #82).

Unauthenticated (ADR 0001) and read-only: it says which Claimed base this
is, so the Portal can tell a browser is on the base's LAN. Only the Portal's
origin may read it cross-origin, and an unclaimed base says nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sp_rtk_portal_protocol import BaseKey

from sp_rtk_base.app import create_api_app
from sp_rtk_base.portal_agent.protocol import PORTAL_URL_ENV, STATE_ENV
from sp_rtk_base.portal_agent.state import Claim, save_claim

PORTAL = "https://sp-rtk-base.big-spray.com"


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(STATE_ENV, str(tmp_path / "state"))
    monkeypatch.delenv(PORTAL_URL_ENV, raising=False)
    return tmp_path / "state"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_api_app())


def _claim(state: Path, base_id: str = "k3mnp4qrst") -> None:
    now = "2026-01-01T12:00:00+00:00"
    save_claim(
        state, BaseKey.generate(), Claim(base_id, "op@example.com", "p", now, now)
    )


def test_a_claimed_base_returns_its_id_to_the_portal(
    state: Path, client: TestClient
) -> None:
    _claim(state)

    resp = client.get("/api/portal/identity", headers={"Origin": PORTAL})

    assert resp.status_code == 200
    assert resp.json() == {"id": "k3mnp4qrst"}
    assert resp.headers["access-control-allow-origin"] == PORTAL
    assert "Origin" in resp.headers["vary"]


@pytest.mark.parametrize("origin", ["https://evil.example", "null", None])
def test_no_other_origin_may_read_it(
    state: Path, client: TestClient, origin: str | None
) -> None:
    _claim(state)

    resp = client.get(
        "/api/portal/identity", headers={"Origin": origin} if origin else {}
    )

    assert "access-control-allow-origin" not in resp.headers


def test_an_unclaimed_base_says_nothing(state: Path, client: TestClient) -> None:
    resp = client.get("/api/portal/identity", headers={"Origin": PORTAL})

    assert resp.status_code == 404
    assert resp.content == b""


def test_the_portal_origin_follows_the_development_override(
    state: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claim(state)
    monkeypatch.setenv(PORTAL_URL_ENV, "http://localhost:8000/")

    resp = client.get(
        "/api/portal/identity", headers={"Origin": "http://localhost:8000"}
    )

    assert resp.headers["access-control-allow-origin"] == "http://localhost:8000"
