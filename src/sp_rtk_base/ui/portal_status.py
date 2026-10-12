"""What the local Portal page says (spec #82).

The page reads the Portal agent's status over its control socket.
``ui/pages/*`` is excluded from the coverage gate, so the wording is decided
here, in a covered module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sp_rtk_base.portal_agent.client import AgentUnavailableError, read_status
from sp_rtk_base.portal_agent.protocol import AgentState
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
    "pairing": "",
    "connected": "",
    "paused": "",
}


@dataclass(frozen=True)
class PortalView:
    headline: str
    detail: str
    agent_running: bool
    can_connect: bool


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
    return PortalView(
        headline=HEADLINES[status.state],
        detail=DETAILS[status.state],
        agent_running=True,
        can_connect=status.state == "not_connected",
    )
