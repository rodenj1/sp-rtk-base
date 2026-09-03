"""Tests for POST /api/input/bluetooth/test — the Verification endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.api.bluetooth import router as bluetooth_router
from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.bluetooth_models import (
    StageResult,
    StageStatus,
    VerificationStage,
    build_result,
)
from sp_rtk_base.services import get_bluetooth_verification_service
from sp_rtk_base.services.bluetooth_service import VerificationRefusedError


class StubService:
    """Records the arguments the route passed through."""

    def __init__(
        self, result: Any = None, refusal: VerificationRefusedError | None = None
    ):
        self.result = result
        self.refusal = refusal
        self.seen: dict[str, Any] = {}

    async def verify(self, **kwargs: Any) -> Any:
        self.seen = kwargs
        if self.refusal is not None:
            raise self.refusal
        return self.result


def _green() -> Any:
    return build_result(
        {
            s: StageResult(stage=s, status=StageStatus.PASSED)
            for s in VerificationStage.ordered()
        },
        rfcomm_channel=1,
        verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture()
def client_for() -> Any:
    def _build(service: StubService) -> TestClient:
        app = create_api_app()
        if not any(r.path.endswith("/bluetooth/test") for r in app.routes):  # type: ignore[attr-defined]
            app.include_router(bluetooth_router)
        app.dependency_overrides[get_bluetooth_verification_service] = lambda: service
        return TestClient(app)

    return _build


class TestTheGreenResponse:
    def test_a_green_is_returned_with_all_five_stages(self, client_for: Any) -> None:
        client = client_for(StubService(result=_green()))
        resp = client.post(
            "/api/input/bluetooth/test",
            json={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["verdict"] == "green"
        assert [s["stage"] for s in body["stages"]] == [
            "discover",
            "pair",
            "trust",
            "connect",
            "data",
        ]

    def test_expiry_is_absolute_and_utc(self, client_for: Any) -> None:
        """The client owns the visible countdown, so it needs both ends."""
        client = client_for(StubService(result=_green()))
        body = client.post(
            "/api/input/bluetooth/test",
            json={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
        ).json()
        assert body["verified_at"].startswith("2026-01-01T00:00:00")
        assert body["expires_at"].startswith("2026-01-01T00:00:30")


class TestTheRequestContract:
    def test_mac_address_is_required(self, client_for: Any) -> None:
        """No name discovery in the test path — a second discovery
        semantic is a second thing to keep in step with the relay's."""
        client = client_for(StubService(result=_green()))
        resp = client.post("/api/input/bluetooth/test", json={"pin": "1234"})
        assert resp.status_code == 422

    def test_a_client_supplied_scan_timeout_is_ignored(self, client_for: Any) -> None:
        """The server owns it, so the Verification and the run wait
        identically. A client-chosen timeout is a way to manufacture a
        Green that Save will not reproduce."""
        stub = StubService(result=_green())
        client = client_for(stub)
        client.post(
            "/api/input/bluetooth/test",
            json={
                "mac_address": "AA:BB:CC:DD:EE:FF",
                "pin": "1234",
                "scan_timeout": 1,
            },
        )
        assert "scan_timeout" not in stub.seen

    def test_the_adapter_defaults_to_hci0(self, client_for: Any) -> None:
        stub = StubService(result=_green())
        client = client_for(stub)
        client.post(
            "/api/input/bluetooth/test",
            json={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
        )
        assert stub.seen["adapter"] == "hci0"

    def test_confirm_repair_defaults_to_false(self, client_for: Any) -> None:
        """Consent is never assumed."""
        stub = StubService(result=_green())
        client = client_for(stub)
        client.post(
            "/api/input/bluetooth/test",
            json={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
        )
        assert stub.seen["confirm_repair"] is False

    def test_confirm_repair_is_passed_through(self, client_for: Any) -> None:
        stub = StubService(result=_green())
        client = client_for(stub)
        client.post(
            "/api/input/bluetooth/test",
            json={
                "mac_address": "AA:BB:CC:DD:EE:FF",
                "pin": "1234",
                "confirm_repair": True,
            },
        )
        assert stub.seen["confirm_repair"] is True


class TestRefusals:
    """Three unrelated refusals share 409 with unrelated remedies, so the
    machine-readable code — not the status — is what a client branches on."""

    @pytest.mark.parametrize(
        "code",
        ["relay_running", "verification_in_progress", "repair_confirmation_required"],
    )
    def test_a_refusal_is_a_409_carrying_its_code(
        self, client_for: Any, code: str
    ) -> None:
        client = client_for(
            StubService(refusal=VerificationRefusedError(code=code, message="nope"))
        )
        resp = client.post(
            "/api/input/bluetooth/test",
            json={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == code

    def test_a_refusal_keeps_the_repos_error_shape(self, client_for: Any) -> None:
        client = client_for(
            StubService(
                refusal=VerificationRefusedError(
                    code="relay_running", message="Stop it"
                )
            )
        )
        body = client.post(
            "/api/input/bluetooth/test",
            json={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
        ).json()
        assert body["status"] == "error"
        assert body["message"] == "Stop it"

    def test_a_refusal_carries_no_verdict(self, client_for: Any) -> None:
        """A refusal is not a third verdict: `stages` would be meaningless."""
        client = client_for(
            StubService(
                refusal=VerificationRefusedError(
                    code="relay_running", message="Stop it"
                )
            )
        )
        body = client.post(
            "/api/input/bluetooth/test",
            json={"mac_address": "AA:BB:CC:DD:EE:FF", "pin": "1234"},
        ).json()
        assert "verdict" not in body
