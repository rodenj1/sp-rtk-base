"""A fake Portal for the agent's unit tests, speaking the real protocol.

It verifies every request's proof with the protocol package (as the real
Portal does), so the agent's signing is exercised; the Operator's side is
scripted: ``approve()``, ``deny()``, or let the clock run past the expiry.
The real Portal + agent run together in sp-rtk-portal's seam-1 suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sp_rtk_portal_protocol import (
    CHECK_IN_PATH,
    CLAIM_TOKEN_PATH,
    CLAIMS_PATH,
    POLL_PATH,
    RELEASE_PATH,
    VERSION_HEADER,
    AgentConfig,
    CheckIn,
    CheckInReply,
    Claimed,
    ClaimStart,
    ClaimStarted,
    ClaimToken,
    ErrorReply,
    Notice,
    Poll,
    PollReply,
    ProofError,
    Release,
    Released,
    ReplayCache,
    UpgradeRequired,
    proof_from_authorization,
    verify_proof,
)

ORIGIN = "https://portal.test"


@dataclass
class _Sleeper:
    deadline: datetime
    seq: int
    future: asyncio.Future[None]
    task: asyncio.Task[Any] | None


class FakeClock:
    """Virtual time for the agent: its sleeps wait until the test moves the
    clock past them (``run``) or wakes the next one (``step``).

    Nothing happens on its own, so a test decides exactly how much time
    passes between the agent's requests.
    """

    def __init__(self) -> None:
        self.at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self._sleepers: list[_Sleeper] = []
        self._seq = itertools.count()

    def now(self) -> datetime:
        return self.at

    async def sleep(self, seconds: float) -> None:
        sleeper = _Sleeper(
            self.at + timedelta(seconds=seconds),
            next(self._seq),
            asyncio.get_running_loop().create_future(),
            asyncio.current_task(),
        )
        self._sleepers.append(sleeper)
        try:
            await sleeper.future
        finally:
            with contextlib.suppress(ValueError):
                self._sleepers.remove(sleeper)

    def _next(self) -> _Sleeper | None:
        waiting = [s for s in self._sleepers if not s.future.done()]
        return min(waiting, key=lambda s: (s.deadline, s.seq), default=None)

    async def _wake(self, sleeper: _Sleeper) -> None:
        self.at = max(self.at, sleeper.deadline)
        sleeper.future.set_result(None)
        # Let the woken task do its work, up to its next sleep (or its end).
        loop = asyncio.get_running_loop()
        give_up = loop.time() + 5
        while loop.time() < give_up:
            await asyncio.sleep(0.001)
            if sleeper.task is None or sleeper.task.done():
                return
            if any(
                s.task is sleeper.task and s.seq > sleeper.seq for s in self._sleepers
            ):
                return

    async def step(self) -> bool:
        """Jump to the next sleep's end and run it. ``False`` if none waits."""
        await asyncio.sleep(0.001)
        sleeper = self._next()
        if sleeper is None:
            return False
        await self._wake(sleeper)
        return True

    async def run(self, **delta: float) -> None:
        """Let ``timedelta(**delta)`` pass, waking each sleep on the way."""
        target = self.at + timedelta(**delta)
        await asyncio.sleep(0.001)
        while (sleeper := self._next()) is not None and sleeper.deadline <= target:
            await self._wake(sleeper)
        self.at = target


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
    # Polls and Check-ins (ticket #87)
    config: AgentConfig = field(default_factory=lambda: AgentConfig(revision=1))
    metered: bool = False
    notices: list[Notice] = field(default_factory=list)
    min_version: int | None = None
    """Refuse (426) any request with a lower protocol version."""
    status: int = 200
    """Answer polls and Check-ins with this HTTP status (with no body)."""
    wake: str | None = None
    """Put this Wake nonce in every poll reply (ticket #88)."""
    doorbell_heard: bool = False
    """Tell Check-ins the Portal hears the base's Doorbell (ticket #100)."""
    ring_key: str | None = None
    """The ring key every Check-in reply carries."""
    unclaimed: bool = False
    """The base was Un-claimed on the Portal: polls, Check-ins and releases
    get 401 ``unclaimed`` (ticket #93)."""
    released_base_id: str = "k3mnp4qrst"
    rate_limited: dict[str, int | str | None] = field(default_factory=dict)
    """Answer requests to these paths 429 ``slow_down`` with this
    ``Retry-After`` (seconds; ``None``: no header) (ticket #98)."""
    _key: str | None = None
    _replays: ReplayCache = field(default_factory=ReplayCache)

    def approve(
        self, base_id: str = "abcdefghij", account: str = "op@example.com"
    ) -> None:
        self.outcome = Claimed(base_id=base_id, account=account)
        self.unclaimed = False  # a new Claim, a new base

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
        version = request.headers.get(VERSION_HEADER)
        if self.min_version is not None and int(version or 0) < self.min_version:
            return httpx.Response(
                426,
                json=UpgradeRequired(
                    min_version=self.min_version, message="Too old, upgrade."
                ).to_json(),
            )
        if request.url.path in self.rate_limited:
            retry_after = self.rate_limited[request.url.path]
            return httpx.Response(
                429,
                json=ErrorReply(ErrorReply.SLOW_DOWN).to_json(),
                headers={}
                if retry_after is None
                else {"Retry-After": str(retry_after)},
            )
        if self.unclaimed and request.url.path in (
            POLL_PATH,
            CHECK_IN_PATH,
            RELEASE_PATH,
        ):
            return httpx.Response(401, json=ErrorReply(ErrorReply.UNCLAIMED).to_json())
        if request.url.path == RELEASE_PATH:
            Release.from_json(data)
            self.unclaimed = True
            return httpx.Response(
                200, json=Released(base_id=self.released_base_id).to_json()
            )
        if request.url.path in (POLL_PATH, CHECK_IN_PATH):
            if self.status != 200:
                return httpx.Response(self.status)
            msg_cls = Poll if request.url.path == POLL_PATH else CheckIn
            msg_cls.from_json(data)
            common: dict[str, Any] = {
                "config": self.config,
                "metered": self.metered,
                "notices": list(self.notices),
            }
            if request.url.path == POLL_PATH:
                return httpx.Response(
                    200, json=PollReply(**common, wake=self.wake).to_json()
                )
            return httpx.Response(
                200,
                json=CheckInReply(
                    **common, doorbell_heard=self.doorbell_heard, ring_key=self.ring_key
                ).to_json(),
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

    @property
    def agent_polls(self) -> list[Seen]:
        """Polls of a Claimed base (``/agent/poll``)."""
        return [r for r in self.requests if r.path == POLL_PATH]

    @property
    def releases(self) -> list[Seen]:
        """Local Un-claims' releases (``/agent/release``)."""
        return [r for r in self.requests if r.path == RELEASE_PATH]

    @property
    def check_ins(self) -> list[Seen]:
        return [r for r in self.requests if r.path == CHECK_IN_PATH]
