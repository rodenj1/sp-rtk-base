"""The control socket's wire format, shared by the agent and the app.

One JSON object per line each way, on a Unix socket:

- request ``{"op": "status"}``
- reply ``{"ok": true, "status": {"state": "not_connected", ...}}``
- request ``{"op": "start_claim"}``: ask the Portal for a pairing code (the
  "Connect to Portal" button). Replies with the status, now ``pairing``.
- request ``{"op": "pause"}`` / ``{"op": "resume"}``: pause or resume remote
  access (#92). Replies with the status (``paused`` / ``connected``), or
  ``{"ok": false, "error": "not claimed"}``.
- request ``{"op": "unclaim"}``: Un-claim the base here (and tell the
  Portal). Replies with the status, now ``not_connected``.
- or ``{"ok": false, "error": "<reason>"}`` (``unknown op``, ``bad request``,
  ``refused``), or ``{"ok": false, "error": "claim failed", "message":
  "<for the Operator>"}``

A reader ignores status fields it doesn't know. Once Claimed the status
also carries the polling fields (``poll_interval``,
``metered``, ``notices``, ``needs_upgrade``, ...), the tunnel's
(``tunnel``, ``tunnel_since``, ``tunnel_streams``, ``tunnel_events``),
the live Remote ``sessions`` and the ``remote_events`` the app raises as
Event log entries (Wakes, Remote session starts and ends).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict
from sp_rtk_portal_protocol import ConnectionMode as ClaimedMode

SOCKET_ENV = "SP_RTK_BASE_PORTAL_AGENT_SOCKET"
"""Overrides :data:`DEFAULT_SOCKET_PATH` (development and e2e)."""

DEFAULT_SOCKET_PATH = Path("/run/sp-rtk-base-portal-agent/control.sock")
"""Where the agent listens: its unit's ``RuntimeDirectory``."""

STATE_ENV = "SP_RTK_BASE_PORTAL_AGENT_STATE"
"""Overrides :data:`DEFAULT_STATE_DIR` (development and e2e). The app reads
it too: the identity endpoint serves the ``<id>`` the agent writes there."""

DEFAULT_STATE_DIR = Path("/var/lib/sp-rtk-base-portal-agent")
"""The agent's unit's ``StateDirectory``: the base key and the Claim. Never
touched by install.sh, so a reinstall keeps the Claim."""

PORTAL_URL_ENV = "SP_RTK_BASE_PORTAL_URL"
"""Overrides :data:`DEFAULT_PORTAL_URL`, for development only."""

DEFAULT_PORTAL_URL = "https://sp-rtk-base.big-spray.com"

AgentState = Literal["not_connected", "pairing", "connected", "paused"]


UPDATE_TO_KEEP_USING = "Update sp-rtk-base to keep using the Portal"
"""What the base says once the Portal no longer supports its protocol
version (HTTP 426): the local page's headline, and the agent's message when
the Portal's reply has none."""


MODE_UNCLAIMED: Final = "unclaimed"
"""The connection mode of a base that isn't Claimed: the Portal doesn't
reach it at all."""

ConnectionMode = ClaimedMode | Literal["unclaimed"]
"""How the Portal reaches the base: one of the protocol's connection modes
(``MODE_HOLDING``, ``MODE_RECONNECTING``, ``MODE_POLLING``) once Claimed,
else :data:`MODE_UNCLAIMED`. Paused is a flag on top, not a mode."""


class RemoteSession(BaseModel):
    """A live Remote session on this base, as the Portal last reported it.

    The protocol's ``LiveSession`` checked for the app: ``since`` is parsed
    into a datetime (the protocol sends an ISO string), and the control
    socket carries it as part of the pydantic :class:`AgentStatus`."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    who: str
    """The user's email."""
    since: datetime
    shared: bool = False
    """A Shared user rather than the Operator."""
    id: str = ""
    """The Portal's id for it; empty from a Portal older than protocol
    package 0.10.0."""


REMOTE_SESSION_ENDED_UNKNOWN = "unknown"
"""The end reason of a Remote session that left the Portal's list without
an end (a Portal restart, or a Portal too old to send ends)."""


class RemoteEvent(BaseModel):
    """A Wake, or a Remote session's start or end: the base's own record of
    Remote sessions, which the app's Event log shows (ADR 0005)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    """Unique and stable (the same session's start always has the same
    id), so a reader can tell which ones it has seen."""
    event: Literal["wake", "session_started", "session_ended"]
    at: datetime
    """When it happened: the Wake heard, the session started or ended."""
    who: str | None = None
    """The user's email (sessions only)."""
    shared: bool = False
    """A Shared user rather than the Operator (sessions only)."""
    reason: str | None = None
    """Why it ended (``session_ended``): one of the protocol's
    ``END_REASONS``, :data:`REMOTE_SESSION_ENDED_UNKNOWN`, or a newer
    Portal's own word."""


class TunnelEvent(BaseModel):
    """The tunnel opened or closed (the app's Event log shows these)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    """Unique, so a reader can tell which ones it has seen."""
    event: Literal["opened", "closed"]
    at: datetime


class AgentStatus(BaseModel):
    """What the agent says about itself."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    state: AgentState
    message: str | None = None
    """Why a Claim didn't happen (``not_connected`` after a pairing)."""
    # Pairing
    user_code: str | None = None
    approve_url: str | None = None
    """The Portal's ``verification_uri_complete``: "Approve on Portal"."""
    expires_at: datetime | None = None
    # Connected
    base_id: str | None = None
    account: str | None = None
    """The Portal account the base is Claimed by (its email)."""
    portal: str | None = None
    """The Portal's host name."""
    last_contact: datetime | None = None
    """The last time the Portal answered the agent."""
    # Connected: polling (ticket #87)
    poll_interval: int | None = None
    """Seconds between polls now (the metered one when ``metered``)."""
    metered: bool = False
    """The Operator marked the base metered on the Portal."""
    config_revision: int | None = None
    """The agent config revision in use; ``None`` on the built-in defaults."""
    tunnel_url: str | None = None
    notices: list[str] = []
    """Notices from the Portal (e.g. a deprecation date)."""
    needs_upgrade: bool = False
    """The Portal no longer supports this agent's protocol version (426); it
    retries once a day. ``message`` is the Portal's explanation."""
    # Connected: the tunnel (tickets #88, #204)
    connection_mode: ConnectionMode | None = None
    """How the Portal reaches the base now (``None`` from an older agent)."""
    tunnel: Literal["closed", "opening", "open"] | None = None
    """The tunnel: held open while ``holding``; a polling base's opens on a
    Wake and is ``closed`` between Remote sessions."""
    tunnel_since: datetime | None = None
    """When the tunnel last changed state (opened, or closed)."""
    tunnel_streams: int = 0
    """Streams open through the tunnel now (requests and websockets)."""
    # Connected or paused: remote use (ticket #92)
    sessions: list[RemoteSession] = []
    """Live Remote sessions, as of the last poll."""
    tunnel_events: list[TunnelEvent] = []
    """The latest tunnel opens and closes since the agent started, oldest
    first (at most :data:`~.tunnel.TUNNEL_EVENTS`)."""
    remote_events: list[RemoteEvent] = []
    """The latest Wakes and Remote session starts and ends since the agent
    started, oldest first (at most :data:`~.remote_record.REMOTE_EVENTS`)."""


def socket_path() -> Path:
    """The control socket's path: :data:`SOCKET_ENV`, else the default."""
    value = os.environ.get(SOCKET_ENV)
    return Path(value) if value else DEFAULT_SOCKET_PATH


def state_path() -> Path:
    """The agent's state directory: :data:`STATE_ENV`, else the default."""
    value = os.environ.get(STATE_ENV)
    return Path(value) if value else DEFAULT_STATE_DIR


def portal_url() -> str:
    """The Portal's URL: :data:`PORTAL_URL_ENV`, else the built-in one."""
    return (os.environ.get(PORTAL_URL_ENV) or DEFAULT_PORTAL_URL).rstrip("/")
