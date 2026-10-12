"""The app's client for the agent's control socket, against fake agents
that misbehave (ticket #83)."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from sp_rtk_base.portal_agent import client
from sp_rtk_base.portal_agent.client import AgentUnavailableError, read_status
from sp_rtk_base.portal_agent.protocol import (
    DEFAULT_SOCKET_PATH,
    SOCKET_ENV,
    socket_path,
)

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@pytest.fixture()
def sock_path() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="agent-") as d:
        yield Path(d) / "control.sock"


async def _fake_agent(path: Path, reply: bytes | None) -> asyncio.Server:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readline()
        if reply is None:
            await asyncio.sleep(10)
        else:
            writer.write(reply)
            await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, str(path))


@pytest_asyncio.fixture()
async def fake(
    sock_path: Path,
) -> AsyncIterator[Callable[[bytes | None], Awaitable[Path]]]:
    servers: list[asyncio.Server] = []

    async def start(reply: bytes | None) -> Path:
        servers.append(await _fake_agent(sock_path, reply))
        return sock_path

    yield start
    for s in servers:
        s.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        b"not json\n",
        b"[1, 2]\n",
        b"",
        b'{"ok": true, "status": {"state": "exploded"}}\n',
        b'{"ok": false, "error": "refused"}\n',
    ],
)
async def test_a_garbled_or_failed_answer_is_unavailable(
    fake: Callable[[bytes | None], Awaitable[Path]], reply: bytes
) -> None:
    path = await fake(reply)

    with pytest.raises(AgentUnavailableError):
        await read_status(path)


@pytest.mark.asyncio
async def test_unknown_status_fields_are_ignored(
    fake: Callable[[bytes | None], Awaitable[Path]],
) -> None:
    path = await fake(b'{"ok": true, "status": {"state": "paused", "new": 1}}\n')

    assert (await read_status(path)).state == "paused"


@pytest.mark.asyncio
async def test_an_agent_that_never_answers_times_out(
    fake: Callable[[bytes | None], Awaitable[Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "TIMEOUT_S", 0.1)
    path = await fake(None)

    with pytest.raises(AgentUnavailableError, match="didn't answer"):
        await read_status(path)


@pytest.mark.asyncio
async def test_a_path_that_is_not_a_socket_is_unavailable(sock_path: Path) -> None:
    sock_path.write_text("x")

    with pytest.raises(AgentUnavailableError):
        await read_status(sock_path)


def test_the_socket_path_defaults_to_the_units_runtime_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SOCKET_ENV, raising=False)
    assert socket_path() == Path("/run/sp-rtk-base-portal-agent/control.sock")
    assert DEFAULT_SOCKET_PATH == socket_path()

    monkeypatch.setenv(SOCKET_ENV, "/tmp/x.sock")
    assert socket_path() == Path("/tmp/x.sock")
