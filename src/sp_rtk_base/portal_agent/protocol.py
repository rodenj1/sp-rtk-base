"""The control socket's wire format, shared by the agent and the app.

One JSON object per line each way, on a Unix socket:

- request ``{"op": "status"}``
- reply ``{"ok": true, "status": {"state": "not_connected"}}``
- or ``{"ok": false, "error": "<reason>"}`` (``unknown op``, ``bad request``,
  ``refused``)

Later tickets add ops (start a Claim, pause/resume, Un-claim) and status
fields; a reader ignores fields it doesn't know.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

SOCKET_ENV = "SP_RTK_BASE_PORTAL_AGENT_SOCKET"
"""Overrides :data:`DEFAULT_SOCKET_PATH` (development and e2e)."""

DEFAULT_SOCKET_PATH = Path("/run/sp-rtk-base-portal-agent/control.sock")
"""Where the agent listens: its unit's ``RuntimeDirectory``."""

AgentState = Literal["not_connected", "pairing", "connected", "paused"]


class AgentStatus(BaseModel):
    """What the agent says about itself."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    state: AgentState


def socket_path() -> Path:
    """The control socket's path: :data:`SOCKET_ENV`, else the default."""
    value = os.environ.get(SOCKET_ENV)
    return Path(value) if value else DEFAULT_SOCKET_PATH
