"""A Claimed agent holds its tunnel open (ADR 0006, protocol version 2,
ticket #204): it opens it with no Wake on start, pings every 50 s, never
closes it on a timer of its own, and opens it again after a drop (after a
random, growing delay, #206).

The agent runs behind its control socket against a fake Portal (polls) and a
fake tunnel endpoint, on virtual time. The real Portal and agent hold the
tunnel together in sp-rtk-portal's seam-1 suite.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sp_rtk_portal_protocol import PROTOCOL_VERSION, AgentConfig, BaseKey
from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, Request

from sp_rtk_base.portal_agent.client import pause, resume
from sp_rtk_base.portal_agent.state import Claim, save_claim
from tests.fixtures.fake_portal import FakeClock, FakePortal
from tests.fixtures.fake_tunnel import (
    TUNNEL_URL,
    AppStub,
    TunnelEndpoint,
    app_stub,
    read_all,
)
from tests.fixtures.running_agent import Running

pytestmark = pytest.mark.asyncio


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


@pytest.fixture()
def claimed(dirs: tuple[Path, Path], clock: FakeClock) -> None:
    at = clock.now().isoformat()
    save_claim(
        dirs[1],
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
    claimed: None,
    portal: FakePortal,
    clock: FakeClock,
    endpoint: TunnelEndpoint,
    app: AppStub,
) -> AsyncIterator[Running]:
    r = Running(*dirs, portal, clock)
    await r.start(tunnel_connect=endpoint.connector(), app_port=app.port)
    try:
        yield r
    finally:
        await r.stop()


async def holding(running: Running, endpoint: TunnelEndpoint) -> None:
    await endpoint.wait_open()
    await running.until_status(lambda s: s.connection_mode == "holding", "holding")


async def test_on_start_the_agent_opens_its_tunnel_with_no_nonce_and_holds_it(
    running: Running, endpoint: TunnelEndpoint
) -> None:
    await holding(running, endpoint)

    [opened] = endpoint.opens
    assert opened.url == TUNNEL_URL
    assert opened.nonce is None
    assert opened.version == str(PROTOCOL_VERSION)
    status = await running.status()
    assert status.tunnel == "open"
    assert [e.event for e in status.tunnel_events] == ["opened"]


async def test_it_pings_every_50_s_and_keeps_the_tunnel_for_hours_used_or_not(
    running: Running, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    await holding(running, endpoint)
    opened_at = clock.now()
    stream = await endpoint.open_stream()  # a websocket left open: in use
    ws = WSConnection(ConnectionType.CLIENT)
    await stream.write(ws.send(Request(host="base", target="/ws")))
    while not any(isinstance(e, AcceptConnection) for e in ws.events()):
        ws.receive_data(await stream.read())

    await clock.run(hours=2)
    await stream.close()
    await clock.run(hours=2)  # and unused

    assert (await running.status()).tunnel == "open"
    assert len(endpoint.opens) == 1
    expected = [opened_at + timedelta(seconds=50 * i) for i in range(1, 289)]
    assert endpoint.pings == expected


async def test_the_ping_interval_comes_from_the_agent_config(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    portal.config = AgentConfig(revision=2, ping_interval=30)
    await clock.run(seconds=20)  # the poll that brings it
    await running.stop()
    await running.start(tunnel_connect=endpoint.connector())
    await holding(running, endpoint)
    opened_at = clock.now()

    await clock.run(seconds=90)

    assert endpoint.pings[-3:] == [
        opened_at + timedelta(seconds=s) for s in (30, 60, 90)
    ]


async def test_after_the_portal_closes_it_the_agent_reconnects_within_60_s(
    running: Running, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    await holding(running, endpoint)

    assert endpoint.connection is not None
    await endpoint.connection.close()
    status = await running.until_status(
        lambda s: s.connection_mode == "reconnecting", "reconnecting"
    )
    assert status.tunnel == "closed"

    await clock.run(seconds=60)  # its first retry: a random 0-60 s
    await holding(running, endpoint)
    assert len(endpoint.opens) == 2
    status = await running.status()
    assert [e.event for e in status.tunnel_events] == ["opened", "closed", "opened"]


async def test_a_refused_hold_is_tried_again_within_2_min(
    dirs: tuple[Path, Path],
    claimed: None,
    portal: FakePortal,
    clock: FakeClock,
    endpoint: TunnelEndpoint,
) -> None:
    endpoint.refuse_held = True
    r = Running(*dirs, portal, clock)
    await r.start(tunnel_connect=endpoint.connector())
    try:
        await r.until_true(lambda: len(endpoint.opens) == 1)
        assert (await r.status()).connection_mode == "reconnecting"

        endpoint.refuse_held = False
        await clock.run(seconds=120)  # after one failure: a random 0-120 s
        await holding(r, endpoint)
        assert [o.accepted for o in endpoint.opens] == [False, True]
    finally:
        await r.stop()


async def test_paused_it_keeps_holding_its_tunnel_but_refuses_every_stream(
    running: Running, endpoint: TunnelEndpoint, clock: FakeClock, app: AppStub
) -> None:
    await holding(running, endpoint)

    await pause(running.sock)
    await clock.run(minutes=5)
    stream = await endpoint.open_stream()
    await stream.write(b"GET /survey HTTP/1.1\r\n\r\n")

    assert await read_all(stream) == b""
    assert app.seen == []
    assert not endpoint.closed.is_set()
    assert (await running.status()).connection_mode == "holding"

    await resume(running.sock)
    stream = await endpoint.open_stream()
    await stream.write(b"GET /survey HTTP/1.1\r\n\r\n")
    assert (await read_all(stream)).endswith(b"base page /survey body=")
    assert len(endpoint.opens) == 1  # the same tunnel throughout


async def test_a_version_1_agent_only_polls(
    dirs: tuple[Path, Path],
    claimed: None,
    portal: FakePortal,
    clock: FakeClock,
    endpoint: TunnelEndpoint,
) -> None:
    r = Running(*dirs, portal, clock)
    r.protocol_version = 1
    await r.start(tunnel_connect=endpoint.connector())
    try:
        await clock.run(minutes=5)
        assert endpoint.opens == []
        assert (await r.status()).connection_mode == "polling"
    finally:
        await r.stop()


async def test_an_unclaimed_agent_has_no_connection(
    dirs: tuple[Path, Path], portal: FakePortal, clock: FakeClock
) -> None:
    r = Running(*dirs, portal, clock)
    await r.start()
    try:
        assert (await r.status()).connection_mode == "unclaimed"
    finally:
        await r.stop()
