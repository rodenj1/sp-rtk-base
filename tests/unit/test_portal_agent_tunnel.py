"""A Wake opens the tunnel, and every stream reaches the base's own UI and
nothing else (ticket #88).

The agent runs behind its control socket against a fake Portal (polls) and a
fake tunnel endpoint (the Portal's end of ``/agent/tunnel``), with a stub
playing the base's UI on its port. The real Portal and agent do this
together in sp-rtk-portal's seam-1 suite.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sp_rtk_portal_protocol import AgentConfig, BaseKey, StreamClosed
from wsproto import ConnectionType, WSConnection
from wsproto.events import (
    AcceptConnection,
    CloseConnection,
    Message,
    Request,
    TextMessage,
)

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


@pytest_asyncio.fixture()
async def running(
    dirs: tuple[Path, Path],
    portal: FakePortal,
    clock: FakeClock,
    endpoint: TunnelEndpoint,
    app: AppStub,
) -> AsyncIterator[Running]:
    sock, state = dirs
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
    r = Running(sock, state, portal, clock)
    await r.start(tunnel_connect=endpoint.connector(), app_port=app.port)
    try:
        yield r
    finally:
        await r.stop()


async def wake(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    """The Portal Wakes the base: its next poll reply carries a nonce."""
    portal.wake = endpoint.nonce = NONCE
    await clock.run(seconds=20)
    portal.wake = None
    await endpoint.wait_open()
    await running.until_status(lambda s: s.tunnel == "open", "tunnel open")


async def http_get(endpoint: TunnelEndpoint, request: bytes) -> bytes:
    stream = await endpoint.open_stream()
    await stream.write(request)
    return await read_all(stream)


# --- Wake --------------------------------------------------------------------------


async def test_a_wake_opens_the_tunnel_with_a_signed_get_echoing_the_nonce(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    assert (await running.status()).tunnel == "closed"

    await wake(running, portal, endpoint, clock)

    [opened] = endpoint.opens
    assert opened.url == TUNNEL_URL
    assert opened.nonce == NONCE
    assert opened.accepted
    assert opened.version == "1"
    status = await running.status()
    assert status.tunnel == "open"
    assert status.tunnel_since == clock.now()


async def test_a_wake_while_the_tunnel_is_open_opens_no_second_one(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    await wake(running, portal, endpoint, clock)
    portal.wake = "another"
    await clock.run(seconds=60)

    assert len(endpoint.opens) == 1


async def test_a_refused_tunnel_is_logged_and_the_next_wake_tries_again(
    running: Running,
    portal: FakePortal,
    endpoint: TunnelEndpoint,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    portal.wake, endpoint.nonce = "forged", "the-real-one"
    with caplog.at_level(logging.WARNING):
        await clock.run(seconds=20)
        portal.wake = None
        await running.until_status(lambda _: bool(endpoint.opens), "an open")
        await asyncio.sleep(0.1)

    assert not endpoint.opens[0].accepted
    assert (await running.status()).tunnel == "closed"
    assert "refused the tunnel" in caplog.text

    await wake(running, portal, endpoint, clock)
    assert endpoint.opens[-1].accepted


async def test_a_forged_poll_reply_cannot_point_the_tunnel_at_another_host(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    portal.config = AgentConfig(
        revision=2, tunnel_url="wss://attacker.example/agent/tunnel"
    )
    await clock.run(seconds=20)  # the poll that brings the bad URL
    portal.wake = endpoint.nonce = "wss://192.168.1.5:22/"
    await clock.run(seconds=20)
    await endpoint.wait_open()

    # The nonce is only ever echoed to the Portal's own tunnel URL.
    assert [o.url for o in endpoint.opens] == [TUNNEL_URL]


async def test_an_oversized_nonce_is_ignored(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    portal.wake = endpoint.nonce = "n" * 1000
    await clock.run(seconds=60)

    assert endpoint.opens == []


# --- Streams -----------------------------------------------------------------------


async def test_an_http_request_down_a_stream_reaches_the_base_ui_with_a_fixed_host(
    running: Running,
    portal: FakePortal,
    endpoint: TunnelEndpoint,
    clock: FakeClock,
    app: AppStub,
) -> None:
    await wake(running, portal, endpoint, clock)

    response = await http_get(
        endpoint,
        b"POST /survey?x=1 HTTP/1.1\r\nHost: nas.lan:5000\r\n"
        b"Connection: keep-alive\r\nContent-Length: 2\r\n\r\nhi",
    )

    assert response.startswith(b"HTTP/1.1 200")
    assert response.endswith(b"base page /survey?x=1 body=hi")
    [seen] = app.seen
    assert seen.method == "POST"
    assert seen.headers["Host"] == "localhost:8080"
    assert seen.headers["Connection"] == "close"


async def test_a_websocket_down_a_stream_reaches_the_base_ui(
    running: Running,
    portal: FakePortal,
    endpoint: TunnelEndpoint,
    clock: FakeClock,
    app: AppStub,
) -> None:
    await wake(running, portal, endpoint, clock)
    stream = await endpoint.open_stream()
    ws = WSConnection(ConnectionType.CLIENT)

    await stream.write(ws.send(Request(host="whatever", target="/ws")))
    events: list[object] = []
    while not any(isinstance(e, AcceptConnection) for e in events):
        ws.receive_data(await asyncio.wait_for(stream.read(), 5))
        events += list(ws.events())
    await stream.write(ws.send(Message(data="hello")))
    reply = ""
    while not reply:
        ws.receive_data(await asyncio.wait_for(stream.read(), 5))
        reply = "".join(e.data for e in ws.events() if isinstance(e, TextMessage))
    await stream.write(ws.send(CloseConnection(code=1000)))

    assert reply == "echo hello"
    [seen] = app.seen
    assert seen.path == "/ws"
    assert seen.headers["Host"] == "localhost:8080"


async def test_streams_share_the_tunnel(
    running: Running,
    portal: FakePortal,
    endpoint: TunnelEndpoint,
    clock: FakeClock,
    app: AppStub,
) -> None:
    await wake(running, portal, endpoint, clock)

    responses = await asyncio.gather(
        *(http_get(endpoint, f"GET /p{i} HTTP/1.1\r\n\r\n".encode()) for i in range(5))
    )

    for i, response in enumerate(responses):
        assert response.endswith(f"base page /p{i} body=".encode())
    assert len(endpoint.opens) == 1


async def test_a_request_naming_another_host_in_its_target_is_refused(
    running: Running,
    portal: FakePortal,
    endpoint: TunnelEndpoint,
    clock: FakeClock,
    app: AppStub,
) -> None:
    await wake(running, portal, endpoint, clock)

    for request in (
        b"GET http://192.168.1.1/admin HTTP/1.1\r\n\r\n",
        b"CONNECT 10.0.0.1:22 HTTP/1.1\r\n\r\n",
        b"GET * HTTP/1.1\r\n\r\n",
        b"not http at all\r\n\r\n",
    ):
        assert await http_get(endpoint, request) == b""

    assert app.seen == []
    assert (await running.status()).tunnel == "open"  # only the stream ended


# --- Frames that close the tunnel --------------------------------------------------


@pytest.mark.parametrize(
    ("frame", "logged"),
    [
        (b"\x01\x00\x00\x00\x09" + b"192.168.1.5:22", "NEW frame carries"),
        (b"\x01\x00\x00\x00\x09" + b'{"host": "nas.lan"}', "NEW frame carries"),
        (b"\x2a\x00\x00\x00\x01", "unknown frame type 42"),
    ],
)
async def test_a_stream_frame_with_a_destination_or_an_unknown_frame_closes_the_tunnel(
    running: Running,
    portal: FakePortal,
    endpoint: TunnelEndpoint,
    clock: FakeClock,
    app: AppStub,
    caplog: pytest.LogCaptureFixture,
    frame: bytes,
    logged: str,
) -> None:
    await wake(running, portal, endpoint, clock)
    live = await endpoint.open_stream()

    with caplog.at_level(logging.WARNING):
        await endpoint.send_raw(frame)
        await asyncio.wait_for(endpoint.closed.wait(), 5)
        await running.until_status(lambda s: s.tunnel == "closed", "tunnel closed")

    assert logged in caplog.text
    assert app.seen == []
    with pytest.raises(StreamClosed):
        await live.write(b"GET / HTTP/1.1\r\n\r\n")


async def test_a_text_message_closes_the_tunnel(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    await wake(running, portal, endpoint, clock)
    assert endpoint.connection is not None

    await endpoint.connection.send('{"connect": "10.0.0.1:22"}')

    await asyncio.wait_for(endpoint.closed.wait(), 5)


async def test_the_portal_closing_the_tunnel_returns_the_agent_to_idle(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    await wake(running, portal, endpoint, clock)
    assert endpoint.connection is not None

    await endpoint.connection.close()

    status = await running.until_status(lambda s: s.tunnel == "closed", "closed")
    assert status.tunnel_since is not None
    await wake(running, portal, endpoint, clock)  # and opens on the next Wake
    assert len(endpoint.opens) == 2


async def test_un_claimed_on_the_portal_the_agent_closes_its_tunnel(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    await wake(running, portal, endpoint, clock)

    portal.unclaimed = True
    status = await running.until("not_connected")

    await asyncio.wait_for(endpoint.closed.wait(), 5)
    assert status.tunnel is None


async def test_a_local_un_claim_closes_the_tunnel(
    running: Running, portal: FakePortal, endpoint: TunnelEndpoint, clock: FakeClock
) -> None:
    await wake(running, portal, endpoint, clock)

    await running.unclaim()

    await asyncio.wait_for(endpoint.closed.wait(), 5)


# --- The agent's own backstop (a Portal that never closes) -------------------------

IDLE, CAP = 30, 120


@pytest.fixture()
def limits(portal: FakePortal) -> None:
    """Short tunnel limits in the agent config (applied on the first poll)."""
    portal.config = AgentConfig(
        revision=2, tunnel_idle_timeout=IDLE, tunnel_hard_cap=CAP
    )


async def held_stream(endpoint: TunnelEndpoint):
    """A websocket down a stream, left open: a Remote session in use."""
    stream = await endpoint.open_stream()
    ws = WSConnection(ConnectionType.CLIENT)
    await stream.write(ws.send(Request(host="base", target="/ws")))
    while not any(isinstance(e, AcceptConnection) for e in ws.events()):
        ws.receive_data(await stream.read())
    return stream


async def test_a_tunnel_left_idle_by_the_portal_is_closed_by_the_agent(
    limits: None,
    running: Running,
    portal: FakePortal,
    endpoint: TunnelEndpoint,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await wake(running, portal, endpoint, clock)

    await clock.run(seconds=IDLE - 1)
    assert (await running.status()).tunnel == "open"
    with caplog.at_level(logging.INFO):
        await clock.run(seconds=1)
        await asyncio.wait_for(endpoint.closed.wait(), 5)

    status = await running.until_status(lambda s: s.tunnel == "closed", "closed")
    assert [e.event for e in status.tunnel_events] == ["opened", "closed"]
    assert "no stream for 30 s" in caplog.text
    await wake(running, portal, endpoint, clock)  # a fresh Wake opens a new one
    assert len(endpoint.opens) == 2


async def test_a_tunnel_in_use_stays_open_past_the_idle_timeout(
    limits: None,
    running: Running,
    portal: FakePortal,
    endpoint: TunnelEndpoint,
    clock: FakeClock,
) -> None:
    await wake(running, portal, endpoint, clock)
    stream = await held_stream(endpoint)

    await clock.run(seconds=IDLE * 2)
    assert (await running.status()).tunnel == "open"

    await stream.close()
    await running.until_status(lambda s: s.tunnel_streams == 0, "stream gone")
    await clock.run(seconds=IDLE - 1)
    assert (await running.status()).tunnel == "open"
    await clock.run(seconds=1)
    await asyncio.wait_for(endpoint.closed.wait(), 5)


async def test_a_tunnel_is_closed_past_its_hard_cap_even_in_use(
    limits: None,
    running: Running,
    portal: FakePortal,
    endpoint: TunnelEndpoint,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await wake(running, portal, endpoint, clock)
    await held_stream(endpoint)

    await clock.run(seconds=CAP + IDLE - 1)
    assert (await running.status()).tunnel == "open"
    with caplog.at_level(logging.INFO):
        await clock.run(seconds=1)
        await asyncio.wait_for(endpoint.closed.wait(), 5)

    status = await running.until_status(lambda s: s.tunnel == "closed", "closed")
    assert status.tunnel_events[-1].event == "closed"
    assert "open for 150 s" in caplog.text
