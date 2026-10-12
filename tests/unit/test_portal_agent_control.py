"""The Portal agent's control socket (spec #82, ticket #83).

The agent answers status questions over a Unix socket permissioned to the
sp-rtk-base service user. These tests run the real agent server on a socket
in a temp dir and talk to it through the client the app uses.
"""

from __future__ import annotations

import asyncio
import os
import socket
import stat
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from sp_rtk_base.portal_agent.client import AgentUnavailableError, read_status
from sp_rtk_base.portal_agent.control import serve


@pytest.fixture()
def sock_path() -> Iterator[Path]:
    # AF_UNIX paths are capped at ~108 bytes; pytest's tmp_path can exceed it.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="agent-") as d:
        yield Path(d) / "control.sock"


@pytest_asyncio.fixture()
async def agent(sock_path: Path) -> AsyncIterator[Path]:
    server = await serve(sock_path)
    try:
        yield sock_path
    finally:
        server.close()
        await server.wait_closed()


async def _raw(path: Path, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(payload)
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 5)
    writer.close()
    await writer.wait_closed()
    return line


@pytest.mark.asyncio
async def test_an_unclaimed_agent_reports_not_connected(agent: Path) -> None:
    status = await read_status(agent)

    assert status.state == "not_connected"


@pytest.mark.asyncio
async def test_the_socket_is_readable_by_its_owner_only(agent: Path) -> None:
    mode = stat.S_IMODE(agent.stat().st_mode)

    assert mode == 0o600


@pytest.mark.asyncio
async def test_refuses_a_connection_from_another_user(sock_path: Path) -> None:
    server = await serve(sock_path, allowed_uids=frozenset({os.getuid() + 1}))
    try:
        with pytest.raises(AgentUnavailableError, match="refused"):
            await read_status(sock_path)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_an_unknown_request_gets_an_error(agent: Path) -> None:
    assert await _raw(agent, b'{"op": "launch"}\n') == (
        b'{"ok": false, "error": "unknown op"}\n'
    )


@pytest.mark.asyncio
async def test_a_request_that_is_not_json_gets_an_error(agent: Path) -> None:
    assert await _raw(agent, b"status please\n") == (
        b'{"ok": false, "error": "bad request"}\n'
    )


@pytest.mark.asyncio
async def test_no_agent_running_is_unavailable(sock_path: Path) -> None:
    with pytest.raises(AgentUnavailableError, match="not running"):
        await read_status(sock_path)


@pytest.mark.asyncio
async def test_a_stale_socket_file_is_replaced(sock_path: Path) -> None:
    sock_path.write_text("left over from a crash")

    server = await serve(sock_path)
    try:
        assert (await read_status(sock_path)).state == "not_connected"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_an_idle_agent_opens_no_network_socket_and_writes_nothing(
    sock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no Claim the agent only ever has its own Unix socket."""
    families: list[int] = []
    real_socket = socket.socket

    class AuditedSocket(real_socket):  # type: ignore[misc,valid-type]
        def __init__(self, family: int = -1, *args: object, **kwargs: object) -> None:
            super().__init__(family, *args, **kwargs)  # type: ignore[arg-type]
            families.append(self.family)

    monkeypatch.setattr(socket, "socket", AuditedSocket)
    monkeypatch.chdir(sock_path.parent)

    server = await serve(sock_path)
    try:
        for _ in range(3):
            await read_status(sock_path)
        await asyncio.sleep(0.2)
    finally:
        server.close()
        await server.wait_closed()

    assert families
    assert set(families) == {socket.AF_UNIX}
    assert {p.name for p in sock_path.parent.iterdir()} <= {sock_path.name}
