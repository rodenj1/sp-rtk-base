"""The agent's control API on a Unix socket.

Permissioned to the sp-rtk-base service user twice over: the socket file
is ``0600`` (owned by the user the agent runs as, the service user), and
every connection's peer uid (``SO_PEERCRED``) must be an allowed one,
by default the agent's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import struct
from pathlib import Path
from typing import Any

from sp_rtk_base.portal_agent.protocol import AgentStatus

logger = logging.getLogger(__name__)

_MAX_REQUEST = 64 * 1024


class PortalAgent:
    """The agent's state. Unclaimed (the only state yet): idle."""

    def status(self) -> AgentStatus:
        return AgentStatus(state="not_connected")


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock: socket.socket | None = writer.get_extra_info("socket")
    if sock is None:  # pragma: no cover - asyncio always provides it
        return None
    creds = sock.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


def _encode(reply: dict[str, Any]) -> bytes:
    return (json.dumps(reply) + "\n").encode()


def _answer(agent: PortalAgent, line: bytes) -> dict[str, Any]:
    try:
        request = json.loads(line)
    except ValueError:
        return {"ok": False, "error": "bad request"}
    if not isinstance(request, dict):
        return {"ok": False, "error": "bad request"}
    if request.get("op") == "status":  # pyright: ignore[reportUnknownMemberType]
        return {"ok": True, "status": agent.status().model_dump()}
    return {"ok": False, "error": "unknown op"}


async def serve(
    path: Path,
    agent: PortalAgent | None = None,
    *,
    allowed_uids: frozenset[int] | None = None,
) -> asyncio.Server:
    """Listen on ``path`` (replacing a stale file) and answer requests.

    ``allowed_uids`` defaults to the agent's own uid.
    """
    the_agent = agent if agent is not None else PortalAgent()
    uids = allowed_uids if allowed_uids is not None else frozenset({os.getuid()})

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            uid = _peer_uid(writer)
            if uid not in uids:
                logger.warning("Refused a control connection from uid %s", uid)
                writer.write(_encode({"ok": False, "error": "refused"}))
                await writer.drain()
                return
            while True:
                line = await reader.readline()
                if not line:
                    return
                writer.write(_encode(_answer(the_agent, line)))
                await writer.drain()
        except (ConnectionError, asyncio.LimitOverrunError, ValueError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(handle, str(path), limit=_MAX_REQUEST)
    finally:
        os.umask(old_umask)
    path.chmod(0o600)
    return server
