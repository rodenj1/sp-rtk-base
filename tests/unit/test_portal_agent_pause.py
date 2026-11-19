"""Pause remote access on site, and what the agent tells the local page
about remote use (ticket #92).

The agent runs behind its control socket against a fake Portal (polls) and a
fake tunnel endpoint, on virtual time. The real Portal and agent pause
together in sp-rtk-portal's seam-1 suite.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sp_rtk_portal_protocol import BaseKey, EndedSession, LiveSession

from sp_rtk_base.portal_agent.client import (
    AgentUnavailableError,
    pause,
    resume,
    unclaim,
)
from sp_rtk_base.portal_agent.protocol import AgentStatus
from sp_rtk_base.portal_agent.state import Claim, save_claim
from tests.fixtures.fake_portal import FakeClock, FakePortal
from tests.fixtures.fake_tunnel import AppStub, TunnelEndpoint, app_stub
from tests.fixtures.running_agent import Running

pytestmark = pytest.mark.asyncio

NONCE = "w4k3-n0nce"


@pytest.fixture()
def dirs() -> Iterator[tuple[Path, Path]]:
    with tempfile.TemporaryDirectory(prefix="agent-") as d:
        yield Path(d) / "control.sock", Path(d) / "state"


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def portal(clock: FakeClock) -> FakePortal:
    return FakePortal(clock)


@pytest_asyncio.fixture()
async def endpoint(clock: FakeClock) -> AsyncIterator[TunnelEndpoint]:
    e = TunnelEndpoint(clock)
    await e.start()
    try:
        yield e
    finally:
        await e.stop()


@pytest_asyncio.fixture()
async def app() -> AsyncIterator[AppStub]:
    async with app_stub() as stub:
        yield stub


def _claim(state: Path, clock: FakeClock) -> None:
    at = clock.now().isoformat()
    save_claim(
        state,
        BaseKey.generate(),
        Claim(
            base_id="k3mnp4qrst",
            account="op@example.com",
            portal="portal.test",
            claimed_at=at,
            last_contact=at,
        ),
    )


@pytest_asyncio.fixture()
async def running(
    dirs: tuple[Path, Path],
    portal: FakePortal,
    clock: FakeClock,
    endpoint: TunnelEndpoint,
    app: AppStub,
) -> AsyncIterator[Running]:
    sock, state = dirs
    _claim(state, clock)
    r = Running(sock, state, portal, clock)
    r.protocol_version = 1  # a polling agent: its tunnel opens on a Wake
    await r.start(tunnel_connect=endpoint.connector(), app_port=app.port)
    try:
        yield r
    finally:
        await r.stop()


async def wake(running: Running, portal: FakePortal, endpoint: TunnelEndpoint) -> None:
    portal.wake = endpoint.nonce = NONCE
    await running.clock.run(seconds=20)
    portal.wake = None
    await endpoint.wait_open()
    await running.until_status(lambda s: s.tunnel == "open", "tunnel open")


# --- Pause and resume ------------------------------------------------------------


async def test_a_paused_agent_says_paused_and_reports_it_in_every_poll(
    running: Running, portal: FakePortal
) -> None:
    status = await pause(running.sock)

    assert status.state == "paused"
    assert status.base_id == "k3mnp4qrst"  # keeps its Claim
    await running.clock.run(seconds=60)
    polls_since = [p.body for p in portal.agent_polls[-3:]]
    assert polls_since == [{"state": "paused"}] * 3


async def test_pausing_tells_the_portal_at_once(
    running: Running, portal: FakePortal
) -> None:
    before = len(portal.agent_polls)

    await pause(running.sock)

    assert len(portal.agent_polls) == before + 1
    assert portal.agent_polls[-1].body == {"state": "paused"}


async def test_resuming_reports_connected_again(
    running: Running, portal: FakePortal
) -> None:
    await pause(running.sock)

    status = await resume(running.sock)

    assert status.state == "connected"
    assert portal.agent_polls[-1].body == {"state": "connected"}
    await running.clock.run(seconds=20)
    assert portal.agent_polls[-1].body == {"state": "connected"}


async def test_a_pause_survives_a_restart(running: Running, portal: FakePortal) -> None:
    await pause(running.sock)

    await running.restart()

    assert (await running.status()).state == "paused"
    await running.clock.run(seconds=20)
    assert portal.agent_polls[-1].body == {"state": "paused"}


async def test_an_unclaimed_agent_cannot_pause(
    dirs: tuple[Path, Path], portal: FakePortal, clock: FakeClock
) -> None:
    r = Running(*dirs, portal, clock)
    await r.start()
    try:
        with pytest.raises(AgentUnavailableError, match="not Claimed"):
            await pause(r.sock)
        with pytest.raises(AgentUnavailableError, match="not Claimed"):
            await resume(r.sock)
    finally:
        await r.stop()


async def test_a_paused_agent_ignores_a_wake(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint
) -> None:
    await pause(running.sock)

    portal.wake = endpoint.nonce = NONCE
    await running.clock.run(seconds=60)

    assert endpoint.opens == []
    assert (await running.status()).tunnel == "closed"


async def test_once_resumed_a_wake_opens_the_tunnel_again(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint
) -> None:
    await pause(running.sock)
    await resume(running.sock)

    await wake(running, portal, endpoint)

    assert len(endpoint.opens) == 1


async def test_pausing_closes_an_open_tunnel(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint
) -> None:
    await wake(running, portal, endpoint)

    status = await pause(running.sock)

    assert status.tunnel == "closed"
    await asyncio.wait_for(endpoint.closed.wait(), 5)


# --- What the local page shows about remote use -------------------------------------


async def test_the_status_lists_the_live_remote_sessions_the_portal_reports(
    running: Running, portal: FakePortal
) -> None:
    portal.sessions = [
        LiveSession(who="op@example.com", since="2026-01-01T12:00:05+00:00"),
        LiveSession(
            who="sam@example.com", since="2026-01-01T12:00:10+00:00", shared=True
        ),
    ]

    status = await running.until_status(lambda s: len(s.sessions) == 2, "sessions")

    [op, sam] = status.sessions
    assert (op.who, op.since, op.shared) == (
        "op@example.com",
        datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc),
        False,
    )
    assert (sam.who, sam.shared) == ("sam@example.com", True)
    portal.sessions = []
    await running.until_status(lambda s: s.sessions == [], "no sessions")


async def test_pausing_forgets_the_sessions_at_once(
    running: Running, portal: FakePortal
) -> None:
    portal.sessions = [LiveSession(who="op@example.com", since="2026-01-01T12:00:05Z")]
    await running.until_status(lambda s: len(s.sessions) == 1, "a session")
    portal.sessions = []  # the Portal ends them when it hears "paused"

    status = await pause(running.sock)

    assert status.sessions == []


async def test_a_malformed_session_is_skipped(
    running: Running, portal: FakePortal
) -> None:
    portal.sessions = [
        LiveSession(who="op@example.com", since="yesterday"),
        LiveSession(who="sam@example.com", since="2026-01-01T12:00:10+00:00"),
    ]

    status = await running.until_status(lambda s: len(s.sessions) == 1, "a session")

    assert status.sessions[0].who == "sam@example.com"


# --- Tunnel opens and closes, for the Event log ----------------------------------------


async def test_the_status_keeps_each_tunnel_open_and_close(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint
) -> None:
    assert (await running.status()).tunnel_events == []
    opened_at = running.clock.now() + timedelta(seconds=20)

    await wake(running, portal, endpoint)
    assert endpoint.connection is not None
    await endpoint.connection.close()
    status = await running.until_status(
        lambda s: len(s.tunnel_events) == 2, "two tunnel events"
    )

    opened, closed = status.tunnel_events
    assert (opened.event, opened.at) == ("opened", opened_at)
    assert closed.event == "closed"
    assert opened.id != closed.id


async def test_a_refused_tunnel_is_neither_an_open_nor_a_close(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint
) -> None:
    portal.wake, endpoint.nonce = NONCE, "another nonce"
    await running.clock.run(seconds=20)
    portal.wake = None
    for _ in range(500):  # the refusal comes back in real time
        if endpoint.opens and (await running.status()).tunnel == "closed":
            break
        await asyncio.sleep(0.01)

    assert endpoint.opens and not endpoint.opens[0].accepted
    assert (await running.status()).tunnel_events == []


async def test_an_un_claim_forgets_the_pause(running: Running) -> None:
    await pause(running.sock)

    status = await unclaim(running.sock)

    assert status.state == "not_connected"
    assert not (running.state / "paused").exists()
    await running.restart()
    assert (await running.status()).state == "not_connected"


# --- The base's own record of Remote sessions (ticket #203) -----------------------------

OP = LiveSession(who="op@example.com", since="2026-01-01T12:00:05+00:00", id="7")
SAM = LiveSession(
    who="sam@example.com", since="2026-01-01T12:00:10+00:00", shared=True, id="8"
)


def _ended(session: LiveSession, reason: str, at: str) -> EndedSession:
    return EndedSession(
        id=session.id,
        who=session.who,
        since=session.since,
        at=at,
        reason=reason,
        shared=session.shared,
    )


def _record(status: AgentStatus) -> list[tuple[str, ...]]:
    return [
        tuple(str(v) for v in (e.event, e.who, e.shared, e.reason) if v is not None)
        for e in status.remote_events
    ]


async def test_each_remote_session_start_and_end_is_recorded_once(
    running: Running, portal: FakePortal
) -> None:
    portal.sessions = [OP, SAM]
    await running.until_status(lambda s: len(s.remote_events) == 2, "two starts")
    await running.clock.run(seconds=60)  # the same sessions, poll after poll
    portal.sessions = [OP]
    portal.ended = [_ended(SAM, "revoked", "2026-01-01T12:01:30+00:00")]
    await running.clock.run(seconds=60)

    status = await running.status()

    assert _record(status) == [
        ("session_started", "op@example.com", "False"),
        ("session_started", "sam@example.com", "True"),
        ("session_ended", "sam@example.com", "True", "revoked"),
    ]
    started, _, ended = status.remote_events
    assert started.at == datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)
    assert ended.at == datetime(2026, 1, 1, 12, 1, 30, tzinfo=timezone.utc)
    assert [s.id for s in status.sessions] == ["7"]
    assert len({e.id for e in status.remote_events}) == 3


async def test_a_session_that_ended_between_polls_still_gets_its_start(
    running: Running, portal: FakePortal
) -> None:
    portal.ended = [_ended(SAM, "idle", "2026-01-01T12:00:15+00:00")]

    status = await running.until_status(lambda s: len(s.remote_events) == 2, "both")

    assert _record(status) == [
        ("session_started", "sam@example.com", "True"),
        ("session_ended", "sam@example.com", "True", "idle"),
    ]


async def test_a_session_gone_without_an_end_ends_for_an_unknown_reason(
    running: Running, portal: FakePortal
) -> None:
    portal.sessions = [OP]
    await running.until_status(lambda s: len(s.remote_events) == 1, "a start")
    portal.sessions = []  # e.g. the Portal restarted

    status = await running.until_status(lambda s: len(s.remote_events) == 2, "an end")

    assert _record(status)[-1] == (
        "session_ended",
        "op@example.com",
        "False",
        "unknown",
    )


async def test_sessions_without_ids_are_listed_but_not_recorded(
    running: Running, portal: FakePortal
) -> None:
    portal.sessions = [LiveSession(who="op@example.com", since=OP.since)]  # old Portal
    portal.ended = [
        EndedSession(
            id="", who="x@example.com", since=OP.since, at=OP.since, reason="left"
        ),
        EndedSession(
            id="9", who="y@example.com", since=OP.since, at="soon", reason="left"
        ),
    ]
    await running.until_status(lambda s: len(s.sessions) == 1, "a session")
    portal.sessions = []
    await running.clock.run(seconds=20)

    assert (await running.status()).remote_events == []


async def test_the_record_survives_an_agent_restart_with_the_same_ids(
    running: Running, portal: FakePortal
) -> None:
    portal.sessions = [OP]
    before = await running.until_status(lambda s: len(s.remote_events) == 1, "start")

    await running.restart()
    after = await running.until_status(lambda s: len(s.remote_events) == 1, "start")

    assert after.remote_events[0].id == before.remote_events[0].id


async def test_each_wake_acted_on_is_recorded_once_with_its_time(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint
) -> None:
    heard_at = running.clock.now() + timedelta(seconds=20)
    portal.wake = endpoint.nonce = NONCE
    await running.clock.run(seconds=60)  # the same Wake in three replies

    [wake] = (await running.status()).remote_events

    assert (wake.event, wake.at, wake.who, wake.reason) == (
        "wake",
        heard_at,
        None,
        None,
    )


async def test_a_wake_ignored_while_paused_is_not_recorded(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint
) -> None:
    await pause(running.sock)
    portal.wake = endpoint.nonce = NONCE
    await running.clock.run(seconds=40)

    assert (await running.status()).remote_events == []


async def test_a_pause_records_how_the_portal_ended_the_sessions(
    running: Running, portal: FakePortal
) -> None:
    portal.sessions = [OP]
    await running.until_status(lambda s: len(s.remote_events) == 1, "a start")
    portal.sessions = []
    portal.ended = [_ended(OP, "paused", "2026-01-01T12:01:00+00:00")]

    await pause(running.sock)

    status = await running.status()
    assert status.sessions == []
    assert _record(status)[-1] == ("session_ended", "op@example.com", "False", "paused")
