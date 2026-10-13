"""What the local Portal page says (spec #82).

The page reads the Portal agent's status over its control socket.
``ui/pages/*`` is excluded from the coverage gate, so the wording is decided
here, in a covered module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sp_rtk_base.portal_agent.client import (
    AgentUnavailableError,
    ClaimFailedError,
    read_status,
    start_claim,
)
from sp_rtk_base.portal_agent.protocol import AgentState, AgentStatus
from sp_rtk_base.update.host_setup import INSTALL_COMMAND

HEADLINES: dict[AgentState, str] = {
    "not_connected": "Not connected",
    "pairing": "Pairing",
    "connected": "Connected",
    "paused": "Paused",
}

DETAILS: dict[AgentState, str] = {
    "not_connected": (
        "This base isn't Claimed, so it never contacts the Portal. Connect it "
        "to open it remotely from sp-rtk-base.big-spray.com."
    ),
    "pairing": (
        "Press Approve on Portal, sign in, and approve this code there before "
        "it runs out. You can also type it in on the Portal's Claim page."
    ),
    "connected": "",
    "paused": "",
}


@dataclass(frozen=True)
class PortalView:
    headline: str
    detail: str
    agent_running: bool
    can_connect: bool
    # Pairing
    code: str | None = None
    approve_url: str | None = None
    expires_at: datetime | None = None
    # Connected: (label, value) rows
    facts: tuple[tuple[str, str], ...] = ()
    notices: tuple[str, ...] = ()
    """Notices from the Portal (e.g. a deprecation date)."""


def countdown(expires_at: datetime, now: datetime) -> str:
    """``m:ss`` left until ``expires_at``; ``0:00`` once past."""
    left = max(0, math.floor((expires_at - now).total_seconds()))
    return f"{left // 60}:{left % 60:02d}"


def _display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


UPGRADE_HEADLINE = "Upgrade sp-rtk-base to keep using the Portal"
UPGRADE_DETAIL = (
    "The Portal no longer supports this version of sp-rtk-base. The base "
    "stays Claimed and tries again once a day; after an upgrade it carries "
    "on by itself."
)


def wake_mode(status: AgentStatus) -> str | None:
    """How the Portal wakes this base, for the page."""
    if status.wake_mode == "doorbell":
        return "Doorbell"
    if status.wake_mode == "polling" and status.poll_interval:
        metered = " (metered)" if status.metered else ""
        return f"Polling every {status.poll_interval} s{metered}"
    return None


DOORBELL_HEALTH = {
    "ok": "Working",
    "unavailable": "Not heard by the Portal; polling meanwhile",
    "ignored": "Ignored: its address isn't public; polling",
}
"""Doorbell health for the page; no row when there is no Doorbell."""


def _view(status: AgentStatus) -> PortalView:
    headline = HEADLINES[status.state]
    detail = DETAILS[status.state]
    if status.state == "not_connected" and status.message:
        detail = f"{status.message} {detail}"
    facts: tuple[tuple[str, str], ...] = ()
    if status.state == "connected":
        facts = (
            ("Account", status.account or "?"),
            ("Portal", status.portal or "?"),
            ("Base id", status.base_id or "?"),
            (
                "Last contact",
                _utc(status.last_contact) if status.last_contact else "never",
            ),
        )
        mode = wake_mode(status)
        if mode:
            facts += (("Wake mode", mode),)
        health = DOORBELL_HEALTH.get(status.doorbell or "off")
        if health:
            facts += (("Doorbell", health),)
    if status.needs_upgrade:
        headline = UPGRADE_HEADLINE
        detail = (
            f"{status.message} {UPGRADE_DETAIL}" if status.message else UPGRADE_DETAIL
        )
    return PortalView(
        notices=tuple(status.notices),
        headline=headline,
        detail=detail,
        agent_running=True,
        can_connect=status.state == "not_connected",
        code=_display_code(status.user_code) if status.user_code else None,
        approve_url=status.approve_url,
        expires_at=status.expires_at,
        facts=facts,
    )


async def portal_view(path: Path) -> PortalView:
    """Ask the agent at ``path`` and say what the page shows."""
    try:
        status = await read_status(path)
    except AgentUnavailableError as exc:
        return PortalView(
            headline="Portal agent isn't running",
            detail=(
                f"{exc}. A base installed before the Portal agent gets it by "
                f"re-running install.sh once: {INSTALL_COMMAND}"
            ),
            agent_running=False,
            can_connect=False,
        )
    return _view(status)


async def connect(path: Path) -> str | None:
    """Start a Claim ("Connect to Portal"). ``None``, or why it didn't start."""
    try:
        await start_claim(path)
    except (AgentUnavailableError, ClaimFailedError) as exc:
        return str(exc)
    return None
