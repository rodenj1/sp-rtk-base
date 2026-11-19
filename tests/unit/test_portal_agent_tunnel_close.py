"""Closing the tunnel client always ends it, whatever the tunnel is doing
at that moment (the agent's shutdown and Un-claim wait on it).

A cancellation that lands while the client tidies up after a drop must not
be swallowed: the hold would go on to sleep before its next try, and
``close()`` (and so the agent's shutdown) would wait for it forever. Each
test drives :class:`TunnelClient` with a fake websocket and a sleep that
never ends on its own, so only a cancellation stops it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from sp_rtk_base.portal_agent.tunnel import TunnelClient

pytestmark = pytest.mark.asyncio

URL = "wss://portal.test/agent/tunnel"
PROMPTLY_S = 2.0


class DroppingWebSocket:
    """A tunnel websocket that stays open until :meth:`drop`."""

    def __init__(self) -> None:
        self._dropped = asyncio.get_running_loop().create_future()

    def drop(self) -> None:
        self._dropped.set_exception(OSError("the connection dropped"))

    async def send(self, message: Any) -> None:
        pass

    async def recv(self) -> bytes:
        return await self._dropped

    async def close(self) -> None:
        pass


class LingeringSleep:
    """A sleep that never ends on its own. The first one cancelled (the
    tunnel's ping) takes until :attr:`release` to finish, so the client is
    caught waiting for it after a drop."""

    def __init__(self) -> None:
        self.lingering = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not self.lingering.is_set():
                self.lingering.set()
                await self.release.wait()
            raise


async def held(ws: DroppingWebSocket, sleep: LingeringSleep) -> TunnelClient:
    async def connect(url: str, headers: dict[str, str]) -> Any:
        return ws

    client = TunnelClient(
        headers=lambda url, nonce: {},
        now=lambda: datetime.now(timezone.utc),
        sleep=sleep,
        connect=connect,
    )
    client.hold(lambda: URL, keep=lambda: True)
    await client.opened()
    assert client.state == "open"
    return client


async def until_closed(client: TunnelClient) -> None:
    async def closed() -> None:
        while client.state != "closed":
            await asyncio.sleep(0.001)

    await asyncio.wait_for(closed(), PROMPTLY_S)


async def test_closing_while_it_tidies_up_after_a_drop_ends_the_hold():
    ws, sleep = DroppingWebSocket(), LingeringSleep()
    client = await held(ws, sleep)

    ws.drop()
    await asyncio.wait_for(sleep.lingering.wait(), PROMPTLY_S)
    closing = asyncio.ensure_future(client.close())
    await asyncio.sleep(0)  # the cancellation reaches the tidying up
    sleep.release.set()

    # Not wait_for: its own cancellation of a stuck close() could end it.
    done, _ = await asyncio.wait({closing}, timeout=PROMPTLY_S)
    assert closing in done, "close() never returned"
    assert client.state == "closed"


async def test_a_caller_cancelled_while_closing_the_tunnel_stays_cancelled():
    ws, sleep = DroppingWebSocket(), LingeringSleep()
    client = await held(ws, sleep)
    after_close: list[str] = []

    async def caller() -> None:
        await client.close()
        after_close.append("carried on")
        await asyncio.Event().wait()

    task = asyncio.ensure_future(caller())
    await asyncio.wait_for(sleep.lingering.wait(), PROMPTLY_S)
    task.cancel()
    sleep.release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, PROMPTLY_S)
    assert after_close == []
    await until_closed(client)  # the hold ends all the same
