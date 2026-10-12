"""The app's side of the control socket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from sp_rtk_base.portal_agent.protocol import AgentStatus

TIMEOUT_S = 2.0


class AgentUnavailableError(Exception):
    """The agent couldn't be asked: not running, refused us, or garbled."""


async def _request(path: Path, request: dict[str, Any]) -> dict[str, Any]:
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
        line = await asyncio.wait_for(reader.readline(), TIMEOUT_S)
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
        raise AgentUnavailableError(f"The Portal agent {reply.get('error', 'failed')}")
    return reply


async def read_status(path: Path) -> AgentStatus:
    """Ask the agent at ``path`` for its status."""
    reply = await _request(path, {"op": "status"})
    try:
        return AgentStatus.model_validate(reply.get("status"))
    except ValidationError as exc:
        raise AgentUnavailableError("The Portal agent answered garbage") from exc
