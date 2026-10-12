"""A fake Portal for the agent's unit tests, speaking the real protocol.

It verifies every request's proof with the protocol package (as the real
Portal does), so the agent's signing is exercised; the Operator's side is
scripted: ``approve()``, ``deny()``, or let the clock run past the expiry.
The real Portal + agent run together in sp-rtk-portal's seam-1 suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sp_rtk_portal_protocol import (
    CLAIM_TOKEN_PATH,
    CLAIMS_PATH,
    VERSION_HEADER,
    Claimed,
    ClaimStart,
    ClaimStarted,
    ClaimToken,
    ErrorReply,
    ProofError,
    ReplayCache,
    proof_from_authorization,
    verify_proof,
)

ORIGIN = "https://portal.test"


class FakeClock:
    def __init__(self) -> None:
        self.at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.at

    async def sleep(self, seconds: float) -> None:
        import asyncio

        self.at += timedelta(seconds=seconds)
        # A real tick, so a test gets to act between the agent's polls.
        await asyncio.sleep(0.002)


@dataclass
class Seen:
    path: str
    at: datetime
    thumbprint: str
    version: str | None
    body: dict[str, Any]


@dataclass
class FakePortal:
    clock: FakeClock
    requests: list[Seen] = field(default_factory=list)
    outcome: str | Claimed = ErrorReply.AUTHORIZATION_PENDING
    queued: list[str] = field(default_factory=list)
    """Answers for the next polls, before ``outcome`` applies again."""
    interval: int = 5
    down: bool = False
    start_status: int = 200
    _key: str | None = None
    _replays: ReplayCache = field(default_factory=ReplayCache)

    def approve(
        self, base_id: str = "abcdefghij", account: str = "op@example.com"
    ) -> None:
        self.outcome = Claimed(base_id=base_id, account=account)

    def deny(self) -> None:
        self.outcome = ErrorReply.ACCESS_DENIED

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("Portal down", request=request)
        body = request.content
        try:
            proof = verify_proof(
                proof_from_authorization(request.headers.get("authorization")),
                method=request.method,
                url=str(request.url),
                now=self.clock.now(),
                body=body,
            )
            self._replays.remember(proof, now=self.clock.now())
        except ProofError as exc:
            return httpx.Response(
                401, json={"error": "invalid_proof", "error_description": str(exc)}
            )
        data = json.loads(body) if body else {}
        self.requests.append(
            Seen(
                request.url.path,
                self.clock.now(),
                proof.thumbprint,
                request.headers.get(VERSION_HEADER),
                data,
            )
        )
        if request.url.path == CLAIMS_PATH:
            ClaimStart.from_json(data)
            if self.start_status != 200:
                return httpx.Response(self.start_status, json={"error": "server_error"})
            self._key = proof.thumbprint
            return httpx.Response(
                200,
                json=ClaimStarted(
                    device_code="device-code",
                    user_code="BCDFGHJK",
                    verification_uri=f"{ORIGIN}/claim",
                    verification_uri_complete=f"{ORIGIN}/claim?code=BCDFGHJK",
                    expires_in=600,
                    interval=self.interval,
                ).to_json(),
            )
        if request.url.path == CLAIM_TOKEN_PATH:
            ClaimToken.from_json(data)
            if proof.thumbprint != self._key:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if self.queued:
                return httpx.Response(400, json={"error": self.queued.pop(0)})
            if isinstance(self.outcome, Claimed):
                return httpx.Response(200, json=self.outcome.to_json())
            return httpx.Response(400, json={"error": self.outcome})
        return httpx.Response(404)

    @property
    def polls(self) -> list[Seen]:
        return [r for r in self.requests if r.path == CLAIM_TOKEN_PATH]
