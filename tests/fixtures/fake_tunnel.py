"""The Portal's end of the tunnel, and a stand-in for the base's own UI, for
the agent's tunnel unit tests.

- :class:`TunnelEndpoint` is a websocket server on a free local port playing
  ``/agent/tunnel``: it checks the open's proof (signed GET, echoing the
  expected Wake nonce) as the real Portal does, then drives the tunnel with
  the protocol package's ``Tunnel`` (Portal role), or sends raw frames.
- :func:`connect_via` is the agent's ``tunnel_connect``: it records the URL
  the agent asked for and connects to the endpoint instead.
- :class:`AppStub` plays sp-rtk-base on ``:8080``: an HTTP route and an echo
  websocket, recording every request it gets.

The real Portal and agent open the tunnel together in sp-rtk-portal's
seam-1 suite.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web
from sp_rtk_portal_protocol import (
    VERSION_HEADER,
    ProofError,
    ReplayCache,
    Stream,
    Tunnel,
    proof_from_authorization,
    verify_proof,
)
from sp_rtk_portal_protocol.tunnel import signed_url
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request, Response

from tests.fixtures.fake_portal import ORIGIN, FakeClock

TUNNEL_URL = ORIGIN.replace("https://", "wss://") + "/agent/tunnel"


@dataclass
class Open:
    url: str
    """What the agent asked to connect to."""
    nonce: str | None
    version: str | None
    accepted: bool


@dataclass
class TunnelEndpoint:
    clock: FakeClock
    nonce: str | None = None
    """The Wake nonce the next open must echo."""
    opens: list[Open] = field(default_factory=list)
    connection: ServerConnection | None = None
    tunnel: Tunnel | None = None
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    _server: Server | None = None
    _replays: ReplayCache = field(default_factory=ReplayCache)
    _asked: list[str] = field(default_factory=list)

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await serve(
            self._handle, "127.0.0.1", 0, process_request=self._check
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _check(self, conn: ServerConnection, request: Request) -> Response | None:
        url = self._asked.pop(0) if self._asked else "?"
        nonce = None
        try:
            proof = verify_proof(
                proof_from_authorization(request.headers.get("Authorization")),
                method="GET",
                url=signed_url(TUNNEL_URL),
                now=self.clock.now(),
            )
            self._replays.remember(proof, now=self.clock.now())
            nonce = proof.nonce
            ok = self.nonce is not None and proof.nonce == self.nonce
        except ProofError:
            ok = False
        self.opens.append(Open(url, nonce, request.headers.get(VERSION_HEADER), ok))
        if not ok:
            return conn.respond(403, "refused")
        self.nonce = None  # one-time
        return None

    async def _handle(self, conn: ServerConnection) -> None:
        self.connection, self.closed = conn, asyncio.Event()
        self.tunnel = Tunnel(conn.send, role="portal")
        try:
            async for message in conn:
                if isinstance(message, bytes):
                    await self.tunnel.feed(message)
        except Exception:
            pass
        finally:
            self.tunnel.close()
            self.closed.set()

    async def wait_open(self) -> Tunnel:
        for _ in range(500):
            if self.tunnel is not None and not self.tunnel.closed:
                return self.tunnel
            await asyncio.sleep(0.01)
        raise AssertionError(f"the agent never opened the tunnel: {self.opens}")

    async def open_stream(self) -> Stream:
        return await (await self.wait_open()).open_stream()

    async def send_raw(self, frame: bytes) -> None:
        assert self.connection is not None
        await self.connection.send(frame)

    def connector(self) -> Any:
        """The agent's ``tunnel_connect``: records the URL it was given and
        connects here."""

        async def connect_here(url: str, headers: dict[str, str]) -> ClientConnection:
            self._asked.append(url)
            return await connect(
                f"ws://127.0.0.1:{self.port}/agent/tunnel", additional_headers=headers
            )

        return connect_here


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]


class AppStub:
    """sp-rtk-base's UI on ``:8080``, as far as the tunnel can tell."""

    def __init__(self) -> None:
        self.seen: list[Seen] = []
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/ws", self._ws)
        app.router.add_route("*", "/{tail:.*}", self._page)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None
        self.port = int(server.sockets[0].getsockname()[1])  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def _record(self, request: web.Request) -> None:
        self.seen.append(Seen(request.method, request.path_qs, dict(request.headers)))

    async def _page(self, request: web.Request) -> web.Response:
        self._record(request)
        body = await request.read()
        return web.Response(text=f"base page {request.path_qs} body={body.decode()}")

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        self._record(request)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await ws.send_str("echo " + msg.data)
        return ws


@contextlib.asynccontextmanager
async def app_stub() -> AsyncIterator[AppStub]:
    stub = AppStub()
    await stub.start()
    try:
        yield stub
    finally:
        await stub.stop()


async def read_all(stream: Stream) -> bytes:
    data = b""
    while chunk := await asyncio.wait_for(stream.read(), 5):
        data += chunk
    return data
