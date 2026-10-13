"""Claiming the base from its agent (ticket #86, spec #82).

The agent is driven through its control socket, as the local Portal page
drives it, against a fake Portal that checks every request's signed proof
with the protocol package. The real Portal and the real agent pair in
sp-rtk-portal's seam-1 suite.
"""

from __future__ import annotations

import json
import stat
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sp_rtk_portal_protocol import PROTOCOL_VERSION, BaseKey

from sp_rtk_base import __version__
from sp_rtk_base.portal_agent.client import (
    ClaimFailedError,
    read_status,
    start_claim,
)
from tests.fixtures.fake_portal import ORIGIN, FakeClock, FakePortal
from tests.fixtures.running_agent import Running


@pytest.fixture()
def dirs() -> Iterator[tuple[Path, Path]]:
    with tempfile.TemporaryDirectory(prefix="agent-") as d:
        yield Path(d) / "control.sock", Path(d) / "state"


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def portal(clock: FakeClock) -> FakePortal:
    return FakePortal(clock)


@pytest_asyncio.fixture()
async def running(
    dirs: tuple[Path, Path], portal: FakePortal, clock: FakeClock
) -> AsyncIterator[Running]:
    r = Running(*dirs, portal, clock)
    await r.start()
    try:
        yield r
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_connect_shows_a_pairing_code_and_where_to_approve(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    status = await start_claim(running.sock)

    assert status.state == "pairing"
    assert status.user_code == "BCDFGHJK"
    assert status.approve_url == f"{ORIGIN}/claim?code=BCDFGHJK"
    assert status.expires_at is not None
    assert (status.expires_at - clock.now()).total_seconds() == pytest.approx(
        600, abs=10
    )
    [first, *_] = portal.requests
    assert first.path == "/agent/claims"
    assert first.version == str(PROTOCOL_VERSION)
    assert first.body == {"name": "pi-north", "software_version": __version__}


@pytest.mark.asyncio
async def test_approval_on_the_portal_connects_the_base(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    await start_claim(running.sock)

    portal.approve(base_id="k3mnp4qrst", account="op@example.com")
    status = await running.until("connected")

    assert status.base_id == "k3mnp4qrst"
    assert status.account == "op@example.com"
    assert status.portal == "portal.test"
    assert status.last_contact is not None
    # Every poll was signed with the key that started the Claim.
    assert {r.thumbprint for r in portal.requests} == {portal.requests[0].thumbprint}


@pytest.mark.asyncio
async def test_the_claim_is_kept_on_disk_with_a_private_key(
    running: Running, portal: FakePortal
) -> None:
    await start_claim(running.sock)
    portal.approve(base_id="k3mnp4qrst")
    await running.until("connected")

    key_file = running.state / "base_key.pem"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(running.state.stat().st_mode) == 0o700
    assert (
        BaseKey.from_pem(key_file.read_bytes()).thumbprint
        == portal.requests[0].thumbprint
    )
    assert (running.state / "base_id").read_text().strip() == "k3mnp4qrst"


@pytest.mark.asyncio
async def test_a_restarted_agent_keeps_its_key_and_id(
    running: Running, portal: FakePortal
) -> None:
    await start_claim(running.sock)
    portal.approve(base_id="k3mnp4qrst", account="op@example.com")
    before = await running.until("connected")
    key = (running.state / "base_key.pem").read_bytes()

    await running.stop()
    await running.start()
    after = await read_status(running.sock)

    assert after.state == "connected"
    assert after.base_id == "k3mnp4qrst"
    assert after.account == "op@example.com"
    assert after.last_contact == before.last_contact
    assert (running.state / "base_key.pem").read_bytes() == key


@pytest.mark.asyncio
async def test_an_unclaimed_agent_writes_nothing(running: Running) -> None:
    assert (await read_status(running.sock)).state == "not_connected"
    assert not running.state.exists()


@pytest.mark.asyncio
async def test_a_pairing_left_unapproved_expires(
    running: Running, portal: FakePortal
) -> None:
    await start_claim(running.sock)

    portal.outcome = "expired_token"
    status = await running.until("not_connected")

    assert (
        status.message
        == "The code expired before it was approved. Connect again for a new one."
    )
    assert not running.state.exists()


@pytest.mark.asyncio
async def test_a_denied_pairing_goes_back_to_not_connected(
    running: Running, portal: FakePortal
) -> None:
    await start_claim(running.sock)

    portal.deny()
    status = await running.until("not_connected")

    assert status.message == "The Portal refused the Claim. Connect again to retry."


@pytest.mark.asyncio
async def test_the_agent_gives_up_at_the_expiry_even_if_the_portal_is_unreachable(
    running: Running, portal: FakePortal
) -> None:
    await start_claim(running.sock)

    portal.down = True
    status = await running.until("not_connected")

    assert status.message is not None and "expired" in status.message


@pytest.mark.asyncio
async def test_a_brief_portal_outage_while_pairing_is_ridden_out(
    running: Running, portal: FakePortal
) -> None:
    await start_claim(running.sock)
    portal.down = True
    for _ in range(3):
        await running.clock.step()

    portal.down = False
    portal.approve()

    assert (await running.until("connected")).base_id == "abcdefghij"


@pytest.mark.asyncio
async def test_slow_down_lengthens_the_poll_interval(
    running: Running, portal: FakePortal
) -> None:
    portal.queued = ["authorization_pending", "slow_down", "slow_down"]

    await start_claim(running.sock)
    await running.until_true(lambda: len(portal.polls) >= 5)

    gaps = [
        (b.at - a.at).total_seconds() for a, b in zip(portal.polls, portal.polls[1:])
    ]
    # RFC 8628 §3.5: each slow_down adds 5 s to the interval, for good.
    assert gaps[:4] == [5, 10, 15, 15]


@pytest.mark.asyncio
async def test_a_portal_that_cant_be_reached_is_reported_and_nothing_kept(
    running: Running, portal: FakePortal
) -> None:
    portal.down = True

    with pytest.raises(ClaimFailedError, match="Couldn't reach the Portal"):
        await start_claim(running.sock)

    status = await read_status(running.sock)
    assert status.state == "not_connected"
    assert not running.state.exists()


@pytest.mark.asyncio
async def test_a_portal_error_on_start_is_reported(
    running: Running, portal: FakePortal
) -> None:
    portal.start_status = 503

    with pytest.raises(ClaimFailedError, match="Couldn't reach the Portal"):
        await start_claim(running.sock)


@pytest.mark.asyncio
async def test_connect_while_pairing_shows_the_same_code(
    running: Running, portal: FakePortal
) -> None:
    await start_claim(running.sock)

    again = await start_claim(running.sock)

    assert again.state == "pairing"
    assert len([r for r in portal.requests if r.path == "/agent/claims"]) == 1


@pytest.mark.asyncio
async def test_connect_on_a_claimed_base_is_refused(
    running: Running, portal: FakePortal
) -> None:
    await start_claim(running.sock)
    portal.approve()
    await running.until("connected")

    with pytest.raises(ClaimFailedError, match="already Claimed"):
        await start_claim(running.sock)


@pytest.mark.asyncio
async def test_a_damaged_claim_file_reads_as_not_connected(
    dirs: tuple[Path, Path], portal: FakePortal, clock: FakeClock
) -> None:
    sock, state = dirs
    state.mkdir()
    (state / "claim.json").write_text("{not json")
    r = Running(sock, state, portal, clock)
    await r.start()
    try:
        assert (await read_status(sock)).state == "not_connected"
    finally:
        await r.stop()


def test_claim_file_holds_what_the_page_shows(tmp_path: Path) -> None:
    from sp_rtk_base.portal_agent.state import Claim, load_claim, save_claim

    claim = Claim(
        base_id="k3mnp4qrst",
        account="op@example.com",
        portal="portal.test",
        claimed_at="2026-01-01T12:00:00+00:00",
        last_contact="2026-01-01T12:00:00+00:00",
    )
    key = BaseKey.generate()
    save_claim(tmp_path / "s", key, claim)

    loaded = load_claim(tmp_path / "s")
    assert loaded is not None
    assert loaded[1] == claim
    assert loaded[0].thumbprint == key.thumbprint
    assert (
        json.loads((tmp_path / "s" / "claim.json").read_text())["base_id"]
        == "k3mnp4qrst"
    )
