"""The tunnel: the Portal's streams to the base's own UI.

A Claimed base holds its tunnel open (ADR 0006, protocol version 2): the
agent opens its tunnel URL (already pinned to ``wss://`` on the Portal's
own host by :mod:`.agent_config`) with a GET signed by the base key and no
nonce, and keeps it open (:meth:`TunnelClient.hold`). It pings the Portal
every ``ping_interval``; the Portal never pings, and closes a held tunnel
that sent nothing for its silence timeout. The Portal limits each Remote
session (idle close, hard cap) on its side; the tunnel itself has no
timers here.

A Wake (a poll reply carrying only a one-time nonce) opens the tunnel the
same way, but with a proof that echoes the nonce (:meth:`TunnelClient.wake`):
that is how an agent that isn't holding its tunnel is reached. A forged
Wake can therefore only cause a tunnel attempt the Portal refuses.

Inside, the protocol package's :class:`~sp_rtk_portal_protocol.Tunnel`
carries the Portal's streams. A stream names no destination (the framing
has no field for one): every stream is connected to ``127.0.0.1:8080`` and
nowhere else. Each stream carries one HTTP request, or one websocket:

- the request line must be origin-form (``GET /path HTTP/1.1``), so a
  request can't ask the base's UI to act as a proxy;
- the ``Host`` header is replaced by :data:`HOST_HEADER`;
- a plain request is sent with ``Connection: close``, so the UI closes the
  connection after its response and the stream ends there;
- after that the bytes pass through untouched.

Any frame the protocol package refuses (an unknown type, a payload on a
new-stream frame, ...) or a text message closes the whole tunnel, and the
agent logs why.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import socket
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from ssl import SSLContext
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from sp_rtk_portal_protocol import FrameError, Stream, Tunnel, TunnelClosed
from sp_rtk_portal_protocol.tunnel import MAX_DATA
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from sp_rtk_base.portal_agent.doh import Names
from sp_rtk_base.portal_agent.protocol import TunnelEvent

logger = logging.getLogger(__name__)

APP_HOST = "127.0.0.1"
APP_PORT = 8080
"""The base's own UI: the only place a stream ever goes."""
HOST_HEADER = b"localhost:8080"
MAX_HEAD = 64 * 1024
"""The largest request head a stream may start with."""
MAX_NONCE = 256
TUNNEL_EVENTS = 20
"""How many tunnel opens and closes the status keeps."""

_REQUEST_LINE = re.compile(rb"^[A-Z]+ /[^ ]* HTTP/1\.[01]$")
_HOP_BY_HOP = (b"host", b"connection", b"keep-alive")

TunnelState = Literal["closed", "opening", "open"]


class WebSocket(Protocol):
    """What the tunnel needs of a websocket client connection (the
    ``websockets`` library's ``ClientConnection``)."""

    async def send(self, message: bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


Connect = Callable[[str, dict[str, str]], Awaitable[WebSocket]]
"""Open a websocket to ``url`` with these request headers."""


async def connect_websocket(
    url: str,
    headers: dict[str, str],
    *,
    names: Names | None = None,
    ssl: SSLContext | None = None,
) -> WebSocket:
    """Open the tunnel websocket. With ``names`` (the agent's
    :class:`~.doh.DohResolver`) the TCP connection goes to the address they
    give for the URL's host; TLS (SNI, the certificate check) and the
    ``Host`` header stay on the host name. ``ssl`` is for tests."""
    host = urlsplit(url).hostname or ""
    address = await names.address(host) if names is not None else None
    extra: dict[str, Any] = {}
    if address is not None:
        extra["host"] = address
    if ssl is not None:
        extra["ssl"] = ssl
    try:
        return await _connect(url, headers, extra)
    except (OSError, asyncio.TimeoutError):
        if address is not None and names is not None:
            names.forget(host)
        raise


async def _connect(
    url: str, headers: dict[str, str], extra: dict[str, Any]
) -> WebSocket:
    return await connect(
        url,
        additional_headers=headers,
        max_size=MAX_DATA + 64,
        open_timeout=15,
        # The agent's own PING frames keep the tunnel alive (every
        # ``ping_interval``); the library's pings would only add traffic.
        ping_interval=None,
        # IPv4 only: the agent's unit gives it no IPv6 (passed through to
        # the event loop's create_connection, so only A records are used).
        family=socket.AF_INET,
        **extra,
    )


def rewrite_head(head: bytes) -> bytes | None:
    """A stream's request head as the base's UI gets it, or ``None`` if it
    isn't an origin-form HTTP/1.x request."""
    lines = head.split(b"\r\n")
    request_line, fields = lines[0], [line for line in lines[1:] if line]
    if not _REQUEST_LINE.match(request_line) or request_line.startswith(b"CONNECT "):
        return None
    names = [f.split(b":", 1)[0].strip().lower() for f in fields]
    if any(b":" not in f for f in fields):
        return None
    upgrade = b"upgrade" in names
    kept = [
        f
        for f, name in zip(fields, names, strict=True)
        if name != b"host" and (upgrade or name not in _HOP_BY_HOP)
    ]
    out = [request_line, b"Host: " + HOST_HEADER, *kept]
    if not upgrade:
        out.append(b"Connection: close")
    return b"\r\n".join(out) + b"\r\n\r\n"


class TunnelClient:
    """The agent's end of the tunnel: at most one at a time."""

    def __init__(
        self,
        *,
        headers: Callable[[str, str | None], dict[str, str]],
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        connect: Connect = connect_websocket,
        app_port: int = APP_PORT,
        ping_interval: Callable[[], float] = lambda: 50,
    ) -> None:
        self._headers = headers
        self._now = now
        self._sleep = sleep
        self._connect = connect
        self._app_port = app_port
        self._ping_interval = ping_interval
        self.state: TunnelState = "closed"
        self.since: datetime | None = None
        self.held = False
        """The tunnel open now was opened to hold, not on a Wake."""
        self._tunnel: Tunnel | None = None
        self._task: asyncio.Task[None] | None = None
        self.events: deque[TunnelEvent] = deque(maxlen=TUNNEL_EVENTS)
        """Opens and closes (not refused or failed attempts)."""

    @property
    def streams(self) -> int:
        return self._tunnel.streams if self._tunnel is not None else 0

    def hold(self, url: str) -> None:
        """Open the tunnel at ``url`` (the checked tunnel URL) with no
        nonce, to hold it open. Ignored while a tunnel is open or opening."""
        if self.state == "closed":
            self._start(None, url)

    def wake(self, nonce: str, url: str) -> None:
        """Open the tunnel at ``url`` (the checked tunnel URL), echoing
        ``nonce``. Ignored while a tunnel is open or opening."""
        if self.state != "closed":
            logger.info("Ignoring a Wake: the tunnel is already %s", self.state)
            return
        if not nonce or len(nonce) > MAX_NONCE:
            logger.warning("Ignoring a Wake with a malformed nonce")
            return
        self._start(nonce, url)

    def _start(self, nonce: str | None, url: str) -> None:
        self.held = False
        self._set("opening")
        self._task = asyncio.ensure_future(self._run(nonce, url))

    def _set(self, state: TunnelState) -> None:
        previous = self.state
        self.state, self.since = state, self._now()
        if previous == "opening" and state == "open":
            self._event("opened")
        elif previous == "open" and state == "closed":
            self._event("closed")

    def _event(self, event: Literal["opened", "closed"]) -> None:
        assert self.since is not None
        self.events.append(
            TunnelEvent(id=secrets.token_hex(8), event=event, at=self.since)
        )

    async def _run(self, nonce: str | None, url: str) -> None:
        try:
            ws = await self._connect(url, self._headers(url, nonce))
        except InvalidStatus as exc:
            logger.warning(
                "The Portal refused the tunnel (HTTP %s)",
                exc.response.status_code,
            )
            self._set("closed")
            return
        except (OSError, asyncio.TimeoutError, WebSocketException) as exc:
            logger.warning("Couldn't open the tunnel: %s", exc)
            self._set("closed")
            return
        tunnel = Tunnel(ws.send, role="agent", on_new_stream=self._serve)
        self._tunnel = tunnel
        self.held = nonce is None
        self._set("open")
        logger.info("Tunnel open%s", "" if self.held else " (Woken)")
        receive = asyncio.ensure_future(self._receive(ws, tunnel))
        ping = asyncio.ensure_future(self._ping(tunnel))
        try:
            await asyncio.wait({receive, ping}, return_when=asyncio.FIRST_COMPLETED)
            for task in (receive, ping):
                if task.done():
                    task.result()
        except FrameError as exc:
            logger.warning(
                "Closing the tunnel: refused a frame from the Portal: %s", exc
            )
        except (WebSocketException, OSError, TunnelClosed) as exc:
            logger.info("Tunnel closed: %s", exc or type(exc).__name__)
        finally:
            for task in (receive, ping):
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            tunnel.close()
            with contextlib.suppress(Exception):
                await ws.close()
            self._tunnel = None
            self._set("closed")

    @staticmethod
    async def _receive(ws: WebSocket, tunnel: Tunnel) -> None:
        while True:
            message = await ws.recv()
            if isinstance(message, str):
                raise FrameError("a text message (frames are binary)")
            await tunnel.feed(message)

    async def _ping(self, tunnel: Tunnel) -> None:
        """Ping the Portal every ``ping_interval``: it keeps the tunnel
        (and any NAT on the way) alive; the Portal never pings."""
        while True:
            await self._sleep(self._ping_interval())
            await tunnel.ping()

    async def _serve(self, stream: Stream) -> None:
        head = bytearray()
        while b"\r\n\r\n" not in head:
            chunk = await stream.read()
            if not chunk or len(head) + len(chunk) > MAX_HEAD:
                return
            head += chunk
        end = head.index(b"\r\n\r\n") + 4
        rewritten = rewrite_head(bytes(head[:end]))
        if rewritten is None:
            logger.warning("Refused a stream: not an origin-form HTTP request")
            return
        reader, writer = await asyncio.open_connection(APP_HOST, self._app_port)
        try:
            writer.write(rewritten + bytes(head[end:]))
            await writer.drain()
            pumps = [
                asyncio.ensure_future(_to_app(stream, writer)),
                asyncio.ensure_future(_to_portal(reader, stream)),
            ]
            _, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


async def _to_app(stream: Stream, writer: asyncio.StreamWriter) -> None:
    while data := await stream.read():
        writer.write(data)
        await writer.drain()


async def _to_portal(reader: asyncio.StreamReader, stream: Stream) -> None:
    while data := await reader.read(MAX_DATA):
        await stream.write(data)
