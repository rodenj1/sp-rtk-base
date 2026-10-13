"""The control socket's wire format, shared by the agent and the app.

One JSON object per line each way, on a Unix socket:

- request ``{"op": "status"}``
- reply ``{"ok": true, "status": {"state": "not_connected", ...}}``
- request ``{"op": "start_claim"}``: ask the Portal for a pairing code (the
  "Connect to Portal" button). Replies with the status, now ``pairing``.
- or ``{"ok": false, "error": "<reason>"}`` (``unknown op``, ``bad request``,
  ``refused``), or ``{"ok": false, "error": "claim failed", "message":
  "<for the Operator>"}``

Later tickets add ops (pause/resume, Un-claim) and status fields; a reader
ignores fields it doesn't know. Once Claimed the status also carries the
polling fields (``wake_mode``, ``poll_interval``, ``metered``,
``notices``, ``needs_upgrade``, ...).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

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
    wake_mode: Literal["polling", "doorbell"] | None = None
    poll_interval: int | None = None
    """Seconds between polls now (the metered one when ``metered``)."""
    metered: bool = False
    """The Operator marked the base metered on the Portal."""
    doorbell: str | None = None
    """Doorbell health, as last Checked in (``off``, ``ignored``, ...)."""
    config_revision: int | None = None
    """The agent config revision in use; ``None`` on the built-in defaults."""
    tunnel_url: str | None = None
    notices: list[str] = []
    """Notices from the Portal (e.g. a deprecation date)."""
    needs_upgrade: bool = False
    """The Portal no longer supports this agent's protocol version (426); it
    retries once a day. ``message`` is the Portal's explanation."""


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
