"""The agent keeps its HTTPS connection to the Portal open between requests
(ticket #201): a poll costs its request and response, not a new TCP
connection and TLS handshake.

These run the agent's own client (``ipv4_transport``) against a keep-alive
HTTPS server on 127.0.0.1 that counts the connections it accepts. Time is
moved on for both the agent's clock and httpcore's (``time.monotonic``),
as it would pass between real polls.
"""

from __future__ import annotations

import asyncio
import time
import types
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpcore
import pytest
import pytest_asyncio
from sp_rtk_portal_protocol import BaseKey

from sp_rtk_base.portal_agent.portal_api import (
    KEEPALIVE_MARGIN_S,
    PortalApi,
    ipv4_transport,
)
from tests.fixtures.tls import TestCa

pytestmark = pytest.mark.asyncio

HOST = "portal.test"


class Names:
    async def address(self, host: str) -> str | None:
        return "127.0.0.1"

    def forget(self, host: str) -> None:
        pass


class KeepAlivePortal:
    """Answers every request 200 and keeps the connection open."""

    def __init__(self) -> None:
        self.connections = 0
        self.port = 0
        self._writers: list[asyncio.StreamWriter] = []

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self._writers.append(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    (
                        int(ln.split(b":", 1)[1])
                        for ln in head.split(b"\r\n")
                        if ln.lower().startswith(b"content-length:")
                    ),
                    0,
                )
                await reader.readexactly(length)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def close_idle(self) -> None:
        """The Portal (or Cloudflare) closes its idle connections."""
        for writer in self._writers:
            writer.close()
        self._writers.clear()
        await asyncio.sleep(0.05)  # the agent's side sees the close


class Clock:
    """The agent's clock and httpcore's, moved on together."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self._offset = 0.0
        fake = types.SimpleNamespace(monotonic=lambda: time.monotonic() + self._offset)
        monkeypatch.setattr(httpcore._async.http11, "time", fake)  # pyright: ignore[reportPrivateUsage]

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)
        self._offset += seconds


@pytest.fixture(scope="module")
def ca() -> TestCa:
    return TestCa()


@pytest_asyncio.fixture()
async def portal(ca: TestCa) -> AsyncIterator[KeepAlivePortal]:
    p = KeepAlivePortal()
    server = await asyncio.start_server(
        p.handle, "127.0.0.1", 0, ssl=ca.server_context(HOST)
    )
    p.port = server.sockets[0].getsockname()[1]
    async with server:
        yield p


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


@pytest_asyncio.fixture()
async def api(
    ca: TestCa, portal: KeepAlivePortal, clock: Clock
) -> AsyncIterator[PortalApi]:
    a = PortalApi(
        f"https://{HOST}:{portal.port}",
        now=clock.now,
        transport=ipv4_transport(Names(), verify=ca.client_context()),
    )
    yield a
    await a.aclose()


KEY = BaseKey.generate()


async def test_requests_a_poll_interval_apart_share_one_connection(
    api: PortalApi, portal: KeepAlivePortal, clock: Clock
) -> None:
    for gap in [20, 20, 60, 60, 20]:  # unmetered and metered polls
        await api.release(KEY)
        clock.advance(gap)
    await api.release(KEY)

    assert portal.connections == 1


async def test_a_longer_poll_interval_keeps_the_connection_as_long(
    api: PortalApi, portal: KeepAlivePortal, clock: Clock
) -> None:
    """Only the Portal or the network closing it makes a new connection,
    whatever the poll interval."""
    api.poll_interval = lambda: 300
    for _ in range(3):
        await api.release(KEY)
        clock.advance(300)
    await api.release(KEY)

    assert portal.connections == 1


@pytest.mark.parametrize("interval", [20, 60, 300])
async def test_a_connection_idle_well_past_the_poll_interval_is_replaced(
    api: PortalApi, portal: KeepAlivePortal, clock: Clock, interval: int
) -> None:
    """A NAT may have forgotten a long-idle connection without a word: the
    agent doesn't wait out a timeout on it."""
    api.poll_interval = lambda: interval
    await api.release(KEY)
    clock.advance(interval + KEEPALIVE_MARGIN_S + 1)
    await api.release(KEY)

    assert portal.connections == 2


async def test_after_the_portal_closes_the_idle_connection_the_next_request_reconnects(
    api: PortalApi, portal: KeepAlivePortal, clock: Clock
) -> None:
    await api.release(KEY)
    clock.advance(20)
    await portal.close_idle()

    await api.release(KEY)  # no PortalUnreachableError

    assert portal.connections == 2
