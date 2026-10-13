"""The app's side of the control socket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from sp_rtk_base.portal_agent.protocol import AgentStatus

TIMEOUT_S = 2.0
CLAIM_TIMEOUT_S = 15.0
"""Starting a Claim, pausing or resuming waits for the Portal (the agent
gives it 10 s)."""


class AgentUnavailableError(Exception):
    """The agent couldn't be asked: not running, refused us, or garbled."""


class ClaimFailedError(Exception):
    """The agent couldn't start a Claim. The message is for the Operator."""


async def _request(
    path: Path, request: dict[str, Any], timeout: float = TIMEOUT_S
) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), TIMEOUT_S
        )
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise AgentUnavailableError("The Portal agent is not running") from exc
    except (OSError, asyncio.TimeoutError) as exc:
        raise AgentUnavailableError(f"Couldn't reach the Portal agent: {exc}") from exc
    try:
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise AgentUnavailableError(f"The Portal agent didn't answer: {exc}") from exc
    finally:
        writer.close()
    try:
        reply = json.loads(line)
    except ValueError as exc:
        raise AgentUnavailableError("The Portal agent didn't answer") from exc
    if not isinstance(reply, dict):
        raise AgentUnavailableError("The Portal agent answered garbage")
    reply = cast(dict[str, Any], reply)
    if reply.get("ok") is not True:
        if reply.get("error") == "not claimed":
            raise AgentUnavailableError("This base is not Claimed")
        if isinstance(reply.get("message"), str):
            raise ClaimFailedError(reply["message"])
        raise AgentUnavailableError(f"The Portal agent {reply.get('error', 'failed')}")
    return reply


def _status(reply: dict[str, Any]) -> AgentStatus:
    try:
        return AgentStatus.model_validate(reply.get("status"))
    except ValidationError as exc:
        raise AgentUnavailableError("The Portal agent answered garbage") from exc


async def read_status(path: Path) -> AgentStatus:
    """Ask the agent at ``path`` for its status."""
    return _status(await _request(path, {"op": "status"}))


async def start_claim(path: Path) -> AgentStatus:
    """Start a Claim ("Connect to Portal"): the agent asks for a pairing code.

    Raises :class:`ClaimFailedError` (e.g. the Portal can't be reached) or
    :class:`AgentUnavailableError`.
    """
    return _status(await _request(path, {"op": "start_claim"}, CLAIM_TIMEOUT_S))


async def pause(path: Path) -> AgentStatus:
    """Pause remote access: the agent keeps its Claim and keeps polling, but
    reports ``paused`` and ignores Wakes. Raises
    :class:`AgentUnavailableError` (also when the base isn't Claimed)."""
    return _status(await _request(path, {"op": "pause"}, CLAIM_TIMEOUT_S))


async def resume(path: Path) -> AgentStatus:
    """Resume remote access (only ever from here: the Portal can't)."""
    return _status(await _request(path, {"op": "resume"}, CLAIM_TIMEOUT_S))
