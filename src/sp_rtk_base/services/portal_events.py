"""Event log entries from the Portal agent: the tunnel opened or closed.

The Event log otherwise carries the Relay's own events. This source reads
the agent's status over its control socket every few seconds and raises an
entry for each tunnel open and close it hasn't raised yet, whether or not
the Relay is running. Anyone on the base's LAN can then see when the base
was used remotely (spec #82).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sp_rtk_base.portal_agent.client import AgentUnavailableError
from sp_rtk_base.portal_agent.protocol import AgentStatus, TunnelEvent

logger = logging.getLogger(__name__)

INTERVAL_S = 2.0
"""How often the agent's status is read (a local socket round trip)."""

_REMEMBERED = 200
"""How many event ids are remembered as already raised."""

MESSAGES = {
    "opened": "Remote access tunnel to the Portal opened",
    "closed": "Remote access tunnel to the Portal closed",
}


def log_entry(event: TunnelEvent) -> dict[str, Any]:
    """``event`` as an Event log entry (the Relay's event shape)."""
    return {
        "event_type": f"portal.tunnel_{event.event}",
        "message": MESSAGES[event.event],
        "timestamp": event.at.timestamp(),
        "payload": {},
    }


class PortalEvents:
    def __init__(
        self,
        read_status: Callable[[], Awaitable[AgentStatus]],
        publish: Callable[[dict[str, Any]], None],
        *,
        interval: float = INTERVAL_S,
    ) -> None:
        self._read_status = read_status
        self._publish = publish
        self._interval = interval
        self._seen: dict[str, None] = {}
        self._task: asyncio.Task[None] | None = None

    async def check(self) -> None:
        """Read the agent's status once and raise any new entries."""
        try:
            status = await self._read_status()
        except AgentUnavailableError:
            return  # not installed, not running, or restarting
        for event in status.tunnel_events:
            if event.id in self._seen:
                continue
            self._seen[event.id] = None
            while len(self._seen) > _REMEMBERED:
                del self._seen[next(iter(self._seen))]
            self._publish(log_entry(event))

    def start(self) -> None:
        """Follow the agent until :meth:`stop`. Needs a running loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                logger.exception("Reading the Portal agent's tunnel events failed")
            await asyncio.sleep(self._interval)
