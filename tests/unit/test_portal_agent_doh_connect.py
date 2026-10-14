"""The agent connects to the address DoH gave, but as the Portal's host name.

TLS (SNI and the certificate check) and the HTTP ``Host`` header stay on the
real name, so Cloudflare routes the request to the Portal and the agent
still knows it is talking to the Portal. These tests serve TLS on 127.0.0.1
with a certificate for ``portal.test`` only, a name the system can't
resolve, and give the clients a resolver that maps it to 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sp_rtk_portal_protocol import BaseKey
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from sp_rtk_base.portal_agent import doorbell
from sp_rtk_base.portal_agent.control import ClaimStartError, PortalAgent
from sp_rtk_base.portal_agent.portal_api import (
    PortalApi,
    PortalUnreachableError,
    ipv4_transport,
)
from sp_rtk_base.portal_agent.tunnel import connect_websocket
from tests.fixtures.tls import TestCa

pytestmark = pytest.mark.asyncio

HOST = "portal.test"
PROMPTLY_S = 2.0


class Names:
    """A resolver mapping ``portal.test`` to 127.0.0.1, recording what it
    was asked and told to forget."""

    def __init__(self, address: str | None = "127.0.0.1") -> None:
        self.address_for = address
        self.asked: list[str] = []
        self.forgotten: list[str] = []

    async def address(self, host: str) -> str | None:
        self.asked.append(host)
        return self.address_for

    def forget(self, host: str) -> None:
        self.forgotten.append(host)


@dataclass
class Seen:
    sni: list[str | None] = field(default_factory=lambda: list[str | None]())
    hosts: list[str] = field(default_factory=lambda: list[str]())


@pytest.fixture(scope="module")
def ca() -> TestCa:
    return TestCa()


def _server_context(ca: TestCa, seen: Seen) -> ssl.SSLContext:
    context = ca.server_context(HOST)

    def record(
        _sock: ssl.SSLObject, name: str | None, _context: ssl.SSLContext
    ) -> None:
        seen.sni.append(name)

    context.sni_callback = record
    return context


@pytest_asyncio.fixture()
async def https_portal(ca: TestCa) -> AsyncIterator[tuple[int, Seen]]:
    """An HTTPS server on 127.0.0.1 answering every request 200."""
    seen = Seen()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        seen.hosts += [
            ln.split(":", 1)[1].strip()
            for ln in lines
            if ln.lower().startswith("host:")
        ]
        length = next(
            (
                int(ln.split(":", 1)[1])
                for ln in lines
                if ln.lower().startswith("content-length:")
            ),
            0,
        )
        await reader.readexactly(length)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(
        handle, "127.0.0.1", 0, ssl=_server_context(ca, seen)
    )
    async with server:
        yield server.sockets[0].getsockname()[1], seen


async def test_the_portal_client_keeps_sni_and_host_on_the_name(
    ca: TestCa, https_portal: tuple[int, Seen]
) -> None:
    port, seen = https_portal
    names = Names()
    api = PortalApi(
        f"https://{HOST}:{port}",
        now=lambda: datetime.now(timezone.utc),
        transport=ipv4_transport(names, verify=ca.client_context()),
    )
    try:
        await asyncio.wait_for(api.release(BaseKey.generate()), PROMPTLY_S)
    finally:
        await api.aclose()

    assert names.asked == [HOST]
    assert seen.sni == [HOST]
    assert seen.hosts == [f"{HOST}:{port}"]


async def test_the_portal_client_forgets_an_address_it_cant_reach(
    ca: TestCa, https_portal: tuple[int, Seen]
) -> None:
    port, _ = https_portal
    names = Names(address="127.0.0.2")  # nothing listens there
    api = PortalApi(
        f"https://{HOST}:{port}",
        now=lambda: datetime.now(timezone.utc),
        transport=ipv4_transport(names, verify=ca.client_context()),
    )
    try:
        with pytest.raises(PortalUnreachableError):
            await asyncio.wait_for(api.release(BaseKey.generate()), PROMPTLY_S)
    finally:
        await api.aclose()

    assert names.forgotten == [HOST]


@pytest_asyncio.fixture()
async def wss_portal(ca: TestCa) -> AsyncIterator[tuple[int, Seen]]:
    """A websocket echo server on 127.0.0.1 over TLS."""
    seen = Seen()

    def record(_conn: ServerConnection, request: Request) -> Response | None:
        seen.hosts.append(request.headers["Host"])
        return None

    async def echo(ws: ServerConnection) -> None:
        async for message in ws:
            await ws.send(message)

    async with serve(
        echo,
        "127.0.0.1",
        0,
        ssl=_server_context(ca, seen),
        process_request=record,
    ) as server:
        yield next(iter(server.sockets)).getsockname()[1], seen


async def test_the_tunnel_keeps_sni_and_host_on_the_name(
    ca: TestCa, wss_portal: tuple[int, Seen]
) -> None:
    port, seen = wss_portal
    names = Names()
    ws = await asyncio.wait_for(
        connect_websocket(
            f"wss://{HOST}:{port}/agent/tunnel",
            {},
            names=names,
            ssl=ca.client_context(),
        ),
        PROMPTLY_S,
    )
    try:
        await ws.send(b"hello")
        assert await asyncio.wait_for(ws.recv(), PROMPTLY_S) == b"hello"
    finally:
        await ws.close()

    assert names.asked == [HOST]
    assert seen.sni == [HOST]
    assert seen.hosts == [f"{HOST}:{port}"]


async def test_the_tunnel_forgets_an_address_it_cant_reach(
    ca: TestCa, wss_portal: tuple[int, Seen]
) -> None:
    port, _ = wss_portal
    names = Names(address="127.0.0.2")
    with pytest.raises(OSError):
        await asyncio.wait_for(
            connect_websocket(
                f"wss://{HOST}:{port}/agent/tunnel",
                {},
                names=names,
                ssl=ca.client_context(),
            ),
            PROMPTLY_S,
        )

    assert names.forgotten == [HOST]


async def test_the_doorbell_host_is_looked_up_like_the_portal_host() -> None:
    asked: list[str] = []

    async def system(host: str, port: int) -> list[str]:
        asked.append(host)
        return ["192.168.50.180"]

    resolve = doorbell.resolve_with(Names(address="104.21.39.51"), system)
    assert await resolve("doorbell.test", 4500) == ["104.21.39.51"]
    assert asked == []

    # DoH said to connect by name: the system resolver answers (and the
    # Doorbell's own check refuses a LAN answer, as before).
    resolve = doorbell.resolve_with(Names(address=None), system)
    assert await resolve("doorbell.test", 4500) == ["192.168.50.180"]
    assert await doorbell.checked_address("doorbell.test", 4500, resolve) is None


async def test_the_agent_reaches_the_portal_where_its_names_say(
    ca: TestCa,
    https_portal: tuple[int, Seen],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, seen = https_portal
    (tmp_path / "ca.pem").write_text(ca.pem)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "ca.pem"))
    names = Names()
    agent = PortalAgent(
        tmp_path / "state", portal_url=f"https://{HOST}:{port}", names=names
    )
    try:
        with pytest.raises(ClaimStartError):  # the server's empty 200
            await asyncio.wait_for(agent.start_claim(), PROMPTLY_S)
    finally:
        await agent.close()

    assert names.asked == [HOST]
    assert seen.sni == [HOST]
