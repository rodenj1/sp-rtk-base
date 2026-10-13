"""One agent process's worth for unit tests: a ``PortalAgent`` behind its
control socket, talking to a :class:`~tests.fixtures.fake_portal.FakePortal`
on virtual time (:class:`~tests.fixtures.fake_portal.FakeClock`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sp_rtk_portal_protocol import PROTOCOL_VERSION, LanAddress

from sp_rtk_base.portal_agent.client import read_status, unclaim
from sp_rtk_base.portal_agent.control import PortalAgent, serve
from sp_rtk_base.portal_agent.doh import SystemNames
from sp_rtk_base.portal_agent.protocol import AgentStatus
from tests.fixtures.fake_portal import ORIGIN, FakeClock, FakePortal

LAN = [LanAddress(address="192.168.1.20", interface="eth0", primary=True)]


class FakeDns:
    """``resolve(host, port)`` for the agent: answers from ``records``."""

    def __init__(self) -> None:
        self.records: dict[str, list[str]] = {}
        self.asked: list[str] = []

    async def __call__(self, host: str, port: int) -> list[str]:
        self.asked.append(host)
        if host not in self.records:
            raise OSError(f"{host}: Name or service not known")
        return list(self.records[host])


class Running:
    def __init__(
        self, sock: Path, state: Path, portal: FakePortal, clock: FakeClock
    ) -> None:
        self.sock, self.state, self.portal, self.clock = sock, state, portal, clock
        self.dns = FakeDns()
        self.lan: list[LanAddress] = list(LAN)
        self.protocol_version = PROTOCOL_VERSION
        self.agent: PortalAgent | None = None
        self.server: asyncio.Server | None = None

    async def start(self, **kwargs: Any) -> None:
        self.agent = PortalAgent(
            self.state,
            portal_url=ORIGIN,
            transport=self.portal.transport(),
            now=self.clock.now,
            sleep=self.clock.sleep,
            name="pi-north",
            resolve=self.dns,
            names=SystemNames(),
            lan_addresses=lambda: list(self.lan),
            protocol_version=self.protocol_version,
            **kwargs,
        )
        self.server = await serve(self.sock, self.agent)
        await asyncio.sleep(0.01)  # a Claimed agent's first poll

    async def stop(self) -> None:
        assert self.agent and self.server
        self.server.close()
        await self.server.wait_closed()
        await self.agent.close()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def status(self) -> AgentStatus:
        return await read_status(self.sock)

    async def unclaim(self) -> AgentStatus:
        """The local page's Un-claim."""
        return await unclaim(self.sock)

    async def until(self, state: str) -> AgentStatus:
        """Let virtual time run until the agent is in ``state``."""
        return await self.until_status(lambda s: s.state == state, state)

    async def until_status(
        self, check: Callable[[AgentStatus], bool], what: str = "it"
    ) -> AgentStatus:
        status = None
        for _ in range(500):
            status = await read_status(self.sock)
            if check(status):
                return status
            if not await self.clock.step():
                await asyncio.sleep(0.01)
        raise AssertionError(f"agent never reached {what}: {status}")

    async def until_true(self, check: Callable[[], bool]) -> None:
        """Let virtual time run until ``check()``."""
        for _ in range(500):
            if check():
                return
            if not await self.clock.step():
                await asyncio.sleep(0.01)
        raise AssertionError("never happened")
