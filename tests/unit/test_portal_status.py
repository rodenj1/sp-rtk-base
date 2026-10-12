"""What the local Portal page says, from what the agent answers (#83)."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from sp_rtk_base.portal_agent.control import serve
from sp_rtk_base.ui.portal_status import portal_view


@pytest.fixture()
def sock_path() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="agent-") as d:
        yield Path(d) / "control.sock"


@pytest.mark.asyncio
async def test_an_unclaimed_base_is_not_connected_and_offers_connect(
    sock_path: Path,
) -> None:
    server = await serve(sock_path)
    try:
        view = await portal_view(sock_path)
    finally:
        server.close()
        await server.wait_closed()

    assert view.headline == "Not connected"
    assert view.can_connect
    assert view.agent_running


@pytest.mark.asyncio
async def test_no_agent_says_so_and_offers_nothing(sock_path: Path) -> None:
    view = await portal_view(sock_path)

    assert view.headline == "Portal agent isn't running"
    assert not view.agent_running
    assert not view.can_connect
    assert "install.sh" in view.detail


@pytest.mark.asyncio
async def test_another_state_is_named_and_offers_no_connect(sock_path: Path) -> None:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readline()
        writer.write(b'{"ok": true, "status": {"state": "paused"}}\n')
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, str(sock_path))
    try:
        view = await portal_view(sock_path)
    finally:
        server.close()

    assert view.headline == "Paused"
    assert not view.can_connect
