"""Event log entries from the Portal agent: the tunnel opened or closed,
Wakes, and Remote session starts and ends.

The Event log otherwise carries the Relay's own events. This source reads
the agent's status over its control socket every few seconds and raises an
entry for each tunnel open and close, Wake, and Remote session start (who,
Operator or Shared user) and end (who, why) it hasn't raised yet, whether
or not the Relay is running. Anyone on the base's LAN can then see when the
base was used remotely, and by whom: a record on the base, which the Portal
can't change (spec #82, ADR 0005). The entries share the Event log's
retention.

It also keeps the live Remote sessions from the last read
(:attr:`PortalEvents.live_sessions`), for pages that show them without a
socket round trip of their own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sp_rtk_base.portal_agent.client import AgentUnavailableError
from sp_rtk_base.portal_agent.protocol import (
    AgentStatus,
    RemoteEvent,
    RemoteSession,
    TunnelEvent,
)

logger = logging.getLogger(__name__)

INTERVAL_S = 2.0
"""How often the agent's status is read (a local socket round trip)."""

_REMEMBERED = 200
"""How many event ids are remembered as already raised."""

MESSAGES = {
    "opened": "Remote access tunnel to the Portal opened",
    "closed": "Remote access tunnel to the Portal closed",
}


ROLES = {False: ("operator", "Operator"), True: ("shared_user", "Shared user")}
"""``RemoteEvent.shared`` as the entry's ``role`` and in words."""

END_REASONS = {
    "left": "left",
    "idle": "idle: nobody had the Session bar open",
    "paused": "remote access paused on the base",
    "lease_expired": "its link's lease expired",
    "revoked": "access revoked on the Portal",
    "hard_cap": "reached its time limit",
    "unknown": "reason unknown",
}
"""End reasons in words; any other is shown as is."""


def log_entry(event: TunnelEvent | RemoteEvent) -> dict[str, Any]:
    """``event`` as an Event log entry (the Relay's event shape)."""
    if isinstance(event, TunnelEvent):
        return {
            "event_type": f"portal.tunnel_{event.event}",
            "message": MESSAGES[event.event],
            "timestamp": event.at.timestamp(),
            "payload": {},
        }
    if event.event == "wake":
        return {
            "event_type": "portal.wake",
            "message": "The Portal woke the base for a Remote session",
            "timestamp": event.at.timestamp(),
            "payload": {},
        }
    role, role_words = ROLES[event.shared]
    payload: dict[str, Any] = {"who": event.who, "role": role}
    if event.event == "session_started":
        message = f"Remote session started: {event.who} ({role_words})"
    else:
        reason = event.reason or "unknown"
        payload["reason"] = reason
        message = (
            f"Remote session ended: {event.who} ({role_words}),"
            f" {END_REASONS.get(reason, reason)}"
        )
    return {
        "event_type": f"portal.remote_{event.event}",
        "message": message,
        "timestamp": event.at.timestamp(),
        "payload": payload,
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
        self.live_sessions: list[RemoteSession] = []
        """The live Remote sessions as of the last read (none while the
        agent can't be read)."""
        self._task: asyncio.Task[None] | None = None

    async def check(self) -> None:
        """Read the agent's status once and raise any new entries."""
        try:
            status = await self._read_status()
        except AgentUnavailableError:
            self.live_sessions = []
            return  # not installed, not running, or restarting
        self.live_sessions = status.sessions
        # On a tie a Wake goes before the tunnel open it caused.
        events: list[TunnelEvent | RemoteEvent] = [
            *status.remote_events,
            *status.tunnel_events,
        ]
        for event in sorted(events, key=lambda e: e.at):
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
                logger.exception("Reading the Portal agent's events failed")
            await asyncio.sleep(self._interval)
