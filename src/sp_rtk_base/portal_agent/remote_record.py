"""The base's own record of Remote sessions (ADR 0005, ticket #203).

:class:`RemoteRecord` turns what the Portal says about the base's Remote
sessions into :class:`~.protocol.RemoteEvent`\\ s the app raises as Event log
entries: each Wake the agent acts on, each Remote session's start (who,
Operator or Shared user) and each end (who, why). The Portal reports live
sessions with ids and recent ends with their reasons; a poll reply carries
both; a held tunnel's control messages carry the same shapes, an end at a
time (:meth:`RemoteRecord.ended`).
The record keeps each session's start and end once, however often the
Portal repeats them, and an end it never saw live still gets its start. A
session that leaves the Portal's list with no end (a Portal restart) ends
with reason :data:`~.protocol.REMOTE_SESSION_ENDED_UNKNOWN`.

Event ids are stable (``<base id>/session/<id>/started``), so an agent
restart that hears the same sessions again doesn't log them twice in an app
that already raised them.
"""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime

from pydantic import ValidationError
from sp_rtk_portal_protocol import EndedSession, LiveSession

from sp_rtk_base.portal_agent.protocol import (
    REMOTE_SESSION_ENDED_UNKNOWN,
    RemoteEvent,
    RemoteSession,
)

logger = logging.getLogger(__name__)

REMOTE_EVENTS = 50
"""How many Wakes, starts and ends the status keeps (newest)."""

_REMEMBERED = 200
"""How many session ids are remembered as started, or ended."""


class RemoteRecord:
    def __init__(self, base_id: str, now: Callable[[], datetime]) -> None:
        self._base_id = base_id
        self._now = now
        self.live: list[RemoteSession] = []
        """Live Remote sessions, as the Portal last reported them."""
        self.events: deque[RemoteEvent] = deque(maxlen=REMOTE_EVENTS)
        """Wakes, starts and ends, oldest first."""
        self._started: dict[str, None] = {}
        self._ended: dict[str, None] = {}

    def wake(self, nonce: str) -> None:
        """The agent acts on a Wake (the same nonce is one Wake)."""
        digest = hashlib.sha256(nonce.encode()).hexdigest()[:16]
        event_id = f"{self._base_id}/wake/{digest}"
        if not any(e.id == event_id for e in self.events):
            self.events.append(RemoteEvent(id=event_id, event="wake", at=self._now()))

    def heard(
        self,
        live: Iterable[LiveSession],
        ended: Iterable[EndedSession],
        *,
        paused: bool = False,
    ) -> None:
        """The Portal's list of live Remote sessions and its recent ends.
        ``paused``: remote access is paused here, so none stays live
        whatever the Portal says (its next reply says how each ended)."""
        sessions = _sessions(live)
        ends = _ends(ended)
        ended_ids = {end.id for end, _ in ends}
        for session in sessions:
            if session.id and session.id not in ended_ids:
                self._start(session)
        for end, session in ends:
            self._end_with_start(end, session)
        still = {s.id for s in sessions}
        for gone in self.live:
            if gone.id and gone.id not in still and gone.id not in ended_ids:
                self._end(gone, REMOTE_SESSION_ENDED_UNKNOWN, self._now())
        self.live = [] if paused else sessions

    def ended(self, end: EndedSession) -> None:
        """One Remote session ended (the tunnel's Session ended message)."""
        for e, session in _ends([end]):
            self._end_with_start(e, session)
            self.live = [s for s in self.live if s.id != session.id]

    def forget_live(self) -> None:
        """Remote access paused here: no session is live any more (the
        Portal's next reply says how each ended)."""
        self.live = []

    def _start(self, session: RemoteSession) -> None:
        if _remember(self._started, session.id):
            self._log(session, "session_started", session.since)

    def _end_with_start(self, end: EndedSession, session: RemoteSession) -> None:
        """An end the Portal reported, logged with its start (once each),
        so a session never seen live still has one."""
        self._start(session)
        self._end(session, end.reason, datetime.fromisoformat(end.at))

    def _end(self, session: RemoteSession, reason: str, at: datetime) -> None:
        if _remember(self._ended, session.id):
            self._log(session, "session_ended", at, reason)

    def _log(
        self,
        session: RemoteSession,
        event: str,
        at: datetime,
        reason: str | None = None,
    ) -> None:
        kind = "started" if event == "session_started" else "ended"
        self.events.append(
            RemoteEvent.model_validate(
                {
                    "id": f"{self._base_id}/session/{session.id}/{kind}",
                    "event": event,
                    "at": at,
                    "who": session.who,
                    "shared": session.shared,
                    "reason": reason,
                }
            )
        )


def _remember(seen: dict[str, None], session_id: str) -> bool:
    """Note ``session_id``; ``False`` if it was already noted."""
    if session_id in seen:
        return False
    seen[session_id] = None
    while len(seen) > _REMEMBERED:
        del seen[next(iter(seen))]
    return True


def _sessions(reported: Iterable[LiveSession]) -> list[RemoteSession]:
    """The live Remote sessions the Portal lists, skipping any malformed."""
    sessions: list[RemoteSession] = []
    for s in reported:
        try:
            sessions.append(RemoteSession.model_validate(s.to_json()))
        except ValidationError:
            logger.warning("Ignoring a malformed Remote session from the Portal")
    return sessions


def _ends(reported: Iterable[EndedSession]) -> list[tuple[EndedSession, RemoteSession]]:
    """The ends the Portal lists, each with its session, skipping any
    malformed or without an id."""
    ends: list[tuple[EndedSession, RemoteSession]] = []
    for e in reported:
        try:
            session = RemoteSession.model_validate(e.to_json())
            datetime.fromisoformat(e.at)
        except (ValidationError, ValueError):
            logger.warning("Ignoring a malformed Remote session end from the Portal")
            continue
        if session.id:
            ends.append((e, session))
    return ends
