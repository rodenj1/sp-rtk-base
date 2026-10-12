"""A stand-in Portal that serves the base under a path prefix (ADR 0004).

It does what the Portal's base proxy does to sp-rtk-base, and nothing
more:

- Proxies ``<prefix>/<tail>`` (HTTP and websockets) to ``<target>/<tail>``
  with ``X-Forwarded-Prefix: <prefix>``.
- Strips ``Cookie`` and ``Set-Cookie`` both ways, so the base never sees
  a Portal cookie and never sets one.
- Stamps ``Access-Control-Allow-Origin: *`` and ``Content-Security-Policy:
  sandbox …`` on every response.
- Serves a parent page at ``/`` that frames ``<prefix>/<p>`` in
  ``<iframe sandbox="allow-scripts allow-forms allow-downloads">`` (no
  ``allow-same-origin``), so the base runs with an opaque origin.

It runs its own event loop on a background thread, so sync Playwright
tests can drive it.  Adapted from the research harness on the meta repo's
``research/sandbox-frame`` branch.
"""

from __future__ import annotations

import asyncio
import html
import socket
import threading
from types import TracebackType

import aiohttp
from aiohttp import web

PREFIX = "/b/test/s/Zk3v9QpL2xYt7RmN4bWc8sHd"
SANDBOX = "allow-scripts allow-forms allow-downloads"

#: Headers that belong to one hop, plus the cookies the Portal never forwards.
_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "host",
    "cookie",
    "set-cookie",
    "origin",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-accept",
}

_PARENT = """<!doctype html><html><head><title>Portal</title></head><body>
<div id="session-bar">Session bar (Portal)</div>
<iframe id="base" src="{src}" sandbox="{sandbox}"
  style="width:1280px;height:800px;border:0"></iframe>
</body></html>"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class PortalProxy:
    """Context manager: the stand-in Portal, listening on ``self.url``."""

    def __init__(self, target: str, prefix: str = PREFIX) -> None:
        self.target = target.rstrip("/")
        self.prefix = prefix
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._runner: web.AppRunner | None = None
        self._client: aiohttp.ClientSession | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> PortalProxy:
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._start(), self._loop).result(10)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        asyncio.run_coroutine_threadsafe(self._stop(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    async def _start(self) -> None:
        self._client = aiohttp.ClientSession(auto_decompress=True)
        app = web.Application(client_max_size=50 * 2**20)
        app.router.add_get("/", self._parent)
        app.router.add_route("*", self.prefix + "/{tail:.*}", self._proxy)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def _stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        if self._client is not None:
            await self._client.close()

    # -- handlers ----------------------------------------------------------

    def framed(self, path: str = "") -> str:
        """URL of the Portal page that frames the base's ``path``."""
        return f"{self.url}/?p={path.lstrip('/')}"

    async def _parent(self, req: web.Request) -> web.Response:
        src = f"{self.prefix}/{req.query.get('p', '')}"
        page = _PARENT.format(src=html.escape(src), sandbox=SANDBOX)
        resp = web.Response(text=page, content_type="text/html")
        # A Portal cookie the frame must never carry to the base.
        resp.set_cookie(
            "portal_session", "PORTAL-COOKIE", httponly=True, samesite="Lax"
        )
        return resp

    async def _proxy(self, req: web.Request) -> web.StreamResponse:
        assert self._client is not None
        tail = req.match_info["tail"]
        url = f"{self.target}/{tail}"
        if req.query_string:
            url += f"?{req.query_string}"
        headers = {k: v for k, v in req.headers.items() if k.lower() not in _HOP}
        headers["X-Forwarded-Prefix"] = self.prefix

        if req.headers.get("Upgrade", "").lower() == "websocket":
            return await self._proxy_ws(req, url.replace("http", "ws", 1), headers)

        async with self._client.request(
            req.method,
            url,
            headers=headers,
            data=await req.read(),
            allow_redirects=False,
        ) as upstream:
            body = await upstream.read()
            resp = web.Response(status=upstream.status, body=body)
            for k, v in upstream.headers.items():
                if k.lower() not in _HOP:
                    resp.headers.add(k, v)
            # What the Portal stamps on every base response: subresources
            # load into the opaque-origin frame, and the base is sandboxed
            # even if something loads it outside the frame.
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Content-Security-Policy"] = f"sandbox {SANDBOX}"
            return resp

    async def _proxy_ws(
        self, req: web.Request, url: str, headers: dict[str, str]
    ) -> web.WebSocketResponse:
        assert self._client is not None
        down = web.WebSocketResponse()
        await down.prepare(req)
        async with self._client.ws_connect(url, headers=headers) as up:

            async def pump_up() -> None:
                async for msg in down:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await up.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await up.send_bytes(msg.data)
                await up.close()

            async def pump_down() -> None:
                async for msg in up:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await down.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await down.send_bytes(msg.data)
                await down.close()

            await asyncio.gather(pump_up(), pump_down(), return_exceptions=True)
        return down
