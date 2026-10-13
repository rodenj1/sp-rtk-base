"""What the local Portal page says, from what the agent answers (#83)."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sp_rtk_base.portal_agent.control import serve
from sp_rtk_base.ui.portal_status import connect, countdown, portal_view


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


# --- Claim (ticket #86) -------------------------------------------------------


async def _answering(sock_path: Path, status: dict[str, object]) -> asyncio.Server:
    import json

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readline()
        writer.write((json.dumps({"ok": True, "status": status}) + "\n").encode())
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, str(sock_path))


@pytest.mark.asyncio
async def test_pairing_shows_the_code_its_expiry_and_where_to_approve(
    sock_path: Path,
) -> None:
    server = await _answering(
        sock_path,
        {
            "state": "pairing",
            "user_code": "BCDFGHJK",
            "approve_url": "https://p/claim?code=BCDFGHJK",
            "expires_at": "2026-01-01T12:10:00+00:00",
        },
    )
    try:
        view = await portal_view(sock_path)
    finally:
        server.close()

    assert view.headline == "Pairing"
    assert view.code == "BCDF-GHJK"
    assert view.approve_url == "https://p/claim?code=BCDFGHJK"
    assert view.expires_at == datetime(2026, 1, 1, 12, 10, tzinfo=timezone.utc)
    assert not view.can_connect
    assert "Approve on Portal" in view.detail


@pytest.mark.asyncio
async def test_connected_shows_account_portal_id_and_last_contact(
    sock_path: Path,
) -> None:
    server = await _answering(
        sock_path,
        {
            "state": "connected",
            "base_id": "k3mnp4qrst",
            "account": "op@example.com",
            "portal": "sp-rtk-base.big-spray.com",
            "last_contact": "2026-01-01T12:03:00+00:00",
        },
    )
    try:
        view = await portal_view(sock_path)
    finally:
        server.close()

    assert view.headline == "Connected"
    assert view.facts == (
        ("Account", "op@example.com"),
        ("Portal", "sp-rtk-base.big-spray.com"),
        ("Base id", "k3mnp4qrst"),
        ("Last contact", "2026-01-01 12:03 UTC"),
    )
    assert not view.can_connect


@pytest.mark.asyncio
async def test_a_failed_pairing_says_why_and_offers_connect_again(
    sock_path: Path,
) -> None:
    server = await _answering(
        sock_path, {"state": "not_connected", "message": "The code expired."}
    )
    try:
        view = await portal_view(sock_path)
    finally:
        server.close()

    assert view.headline == "Not connected"
    assert view.detail.startswith("The code expired.")
    assert view.can_connect


@pytest.mark.parametrize(
    ("seconds", "text"), [(600, "10:00"), (599.4, "9:59"), (5, "0:05"), (-3, "0:00")]
)
def test_countdown(seconds: float, text: str) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    assert countdown(now + timedelta(seconds=seconds), now) == text


@pytest.mark.asyncio
async def test_connect_starts_a_claim_or_says_why_not(sock_path: Path) -> None:
    assert await connect(sock_path) == ("The Portal agent is not running")

    server = await _answering(sock_path, {"state": "pairing", "user_code": "BCDFGHJK"})
    try:
        assert await connect(sock_path) is None
    finally:
        server.close()


@pytest.mark.asyncio
async def test_connect_passes_on_the_agents_reason(sock_path: Path) -> None:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readline()
        writer.write(
            b'{"ok": false, "error": "claim failed", "message": "Couldn\'t reach the Portal (x)."}\n'
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, str(sock_path))
    try:
        assert await connect(sock_path) == "Couldn't reach the Portal (x)."
    finally:
        server.close()


# --- Polling (ticket #87) -----------------------------------------------------

CONNECTED = {
    "state": "connected",
    "base_id": "k3mnp4qrst",
    "account": "op@example.com",
    "portal": "sp-rtk-base.big-spray.com",
    "last_contact": "2026-01-01T12:03:00+00:00",
}


async def _view_of(sock_path: Path, status: dict[str, object]):
    server = await _answering(sock_path, status)
    try:
        return await portal_view(sock_path)
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "mode"),
    [
        ({"wake_mode": "polling", "poll_interval": 20}, "Polling every 20 s"),
        (
            {"wake_mode": "polling", "poll_interval": 60, "metered": True},
            "Polling every 60 s (metered)",
        ),
        ({"wake_mode": "doorbell", "poll_interval": 20}, "Doorbell"),
    ],
)
async def test_connected_shows_the_wake_mode(
    sock_path: Path, extra: dict[str, object], mode: str
) -> None:
    view = await _view_of(sock_path, CONNECTED | extra)

    assert ("Last contact", "2026-01-01 12:03 UTC") in view.facts
    assert ("Wake mode", mode) in view.facts


@pytest.mark.asyncio
async def test_notices_from_the_portal_are_shown(sock_path: Path) -> None:
    view = await _view_of(
        sock_path, CONNECTED | {"notices": ["Protocol 1 ends on 2027-06-01."]}
    )

    assert view.notices == ("Protocol 1 ends on 2027-06-01.",)


@pytest.mark.asyncio
async def test_an_agent_too_old_for_the_portal_says_to_upgrade(
    sock_path: Path,
) -> None:
    view = await _view_of(
        sock_path,
        CONNECTED | {"needs_upgrade": True, "message": "Protocol 1 ended."},
    )

    assert view.headline == "Upgrade sp-rtk-base to keep using the Portal"
    assert "Protocol 1 ended." in view.detail
    assert ("Base id", "k3mnp4qrst") in view.facts  # still Claimed
    assert not view.can_connect
