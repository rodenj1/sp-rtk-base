"""The Portal agent reaches dual-stack hosts promptly over IPv4.

Its unit gives it no IPv6 (``IPAddressDeny=::/0``, no ``AF_INET6``), so a
host name with both AAAA and A records must not cost it a timeout: every
client asks the resolver for IPv4 addresses only. These tests stand the
unit's rules in: a resolver that answers ``dual.test`` with an AAAA and an
A record (127.0.0.1, where the test's server listens), and a ``socket``
that can't be created for IPv6.
"""

from __future__ import annotations

import asyncio
import errno
import socket
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from sp_rtk_portal_protocol import BaseKey
from websockets.asyncio.server import ServerConnection, serve

from sp_rtk_base.portal_agent import doorbell
from sp_rtk_base.portal_agent.portal_api import PortalApi
from sp_rtk_base.portal_agent.tunnel import connect_websocket

pytestmark = pytest.mark.asyncio

DUAL = "dual.test"
PROMPTLY_S = 2.0


class DualStackResolver:
    """``socket.getaddrinfo`` for ``dual.test``: an AAAA (first, as a real
    resolver orders it) and an A record. Records the family each lookup
    asked for."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.families: list[int] = []

    def __call__(
        self,
        host: Any,
        port: Any,
        family: int = 0,
        type: int = 0,  # socket.getaddrinfo's own name
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[Any, ...]]:
        # anyio (under httpx) passes the name IDNA-encoded, as bytes.
        if host not in (DUAL, DUAL.encode()):
            return self._real(host, port, family, type, proto, flags)
        self.families.append(family)
        port = int(port or 0)
        kind = type or socket.SOCK_STREAM
        answers: list[tuple[Any, ...]] = [
            (socket.AF_INET6, kind, 0, "", ("2600:8801::1", port, 0, 0)),
            (socket.AF_INET, kind, 0, "", ("127.0.0.1", port)),
        ]
        return [a for a in answers if family in (socket.AF_UNSPEC, a[0])]


@pytest.fixture()
def no_ipv6(monkeypatch: pytest.MonkeyPatch) -> Iterator[DualStackResolver]:
    real_socket = socket.socket

    class Ipv4Socket(real_socket):  # type: ignore[misc, valid-type]
        def __init__(
            self,
            family: int = -1,
            type: int = -1,
            proto: int = -1,
            fileno: int | None = None,
        ) -> None:
            if family == socket.AF_INET6:
                # What RestrictAddressFamilies does to a family it leaves out.
                raise OSError(errno.EAFNOSUPPORT, "Address family not supported")
            super().__init__(family, type, proto, fileno)

    resolver = DualStackResolver(socket.getaddrinfo)
    monkeypatch.setattr(socket, "socket", Ipv4Socket)
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    yield resolver


@pytest_asyncio.fixture()
async def http_server() -> AsyncIterator[tuple[int, list[bytes]]]:
    """A server on 127.0.0.1 answering every HTTP request 200."""
    heads: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        heads.append(head)
        length = next(
            (
                int(ln.split(b":", 1)[1])
                for ln in head.split(b"\r\n")
                if ln.lower().startswith(b"content-length:")
            ),
            0,
        )
        await reader.readexactly(length)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[1], heads


async def test_the_portal_client(
    no_ipv6: DualStackResolver, http_server: tuple[int, list[bytes]]
) -> None:
    port, heads = http_server
    api = PortalApi(f"http://{DUAL}:{port}", now=lambda: datetime.now(timezone.utc))
    try:
        await asyncio.wait_for(api.release(BaseKey.generate()), PROMPTLY_S)
    finally:
        await api.aclose()

    assert len(heads) == 1
    assert no_ipv6.families == [socket.AF_INET]


async def test_the_tunnel_websocket(no_ipv6: DualStackResolver) -> None:
    async def echo(ws: ServerConnection) -> None:
        async for message in ws:
            await ws.send(message)

    async with serve(echo, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        ws = await asyncio.wait_for(
            connect_websocket(f"ws://{DUAL}:{port}/tunnel", {}), PROMPTLY_S
        )
        try:
            await ws.send(b"hello")
            assert await asyncio.wait_for(ws.recv(), PROMPTLY_S) == b"hello"
        finally:
            await ws.close()

    assert no_ipv6.families == [socket.AF_INET]


async def test_the_doorbell(no_ipv6: DualStackResolver) -> None:
    addresses = await asyncio.wait_for(doorbell.system_resolve(DUAL, 4500), PROMPTLY_S)

    assert addresses == ["127.0.0.1"]
    assert no_ipv6.families == [socket.AF_INET]
    # ... and its UDP socket is an IPv4 one, which the unit allows.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(PROMPTLY_S)
        sock = await doorbell.open_udp(addresses[0], lambda _data, _src: None)
        try:
            sock.sendto(b"keepalive", listener.getsockname())
            assert listener.recv(64) == b"keepalive"
        finally:
            sock.close()
