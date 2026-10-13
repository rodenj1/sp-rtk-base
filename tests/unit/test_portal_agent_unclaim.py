"""Un-claim from the agent's side (ticket #93, spec #82).

Local: the page asks the agent to Un-claim; it sends a signed best-effort
release, then wipes its key and goes idle. On the Portal: the agent's next
request gets 401 ``unclaimed``, and it does the same. Either way the next
Claim makes a new key. The real Portal + agent do both in sp-rtk-portal's
seam-1 suite.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sp_rtk_portal_protocol import RELEASE_PATH, BaseKey

from sp_rtk_base.portal_agent.client import start_claim
from sp_rtk_base.portal_agent.state import Claim, read_base_id, save_claim
from tests.fixtures.fake_portal import FakeClock, FakePortal
from tests.fixtures.running_agent import Running

pytestmark = pytest.mark.asyncio

BASE_ID = "k3mnp4qrst"


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


@pytest.fixture()
def key() -> BaseKey:
    return BaseKey.generate()


@pytest_asyncio.fixture()
async def running(
    dirs: tuple[Path, Path], portal: FakePortal, clock: FakeClock, key: BaseKey
) -> AsyncIterator[Running]:
    sock, state = dirs
    at = clock.now().isoformat()
    save_claim(
        state,
        key,
        Claim(
            base_id=BASE_ID,
            account="op@example.com",
            portal="portal.test",
            claimed_at=at,
            last_contact=at,
        ),
    )
    r = Running(sock, state, portal, clock)
    await r.start()
    try:
        yield r
    finally:
        await r.stop()


def wiped(state: Path) -> bool:
    """Nothing of the Claim is left: no key, Claim, id or agent config."""
    return not state.exists() or not any(state.iterdir())


# --- Local Un-claim ----------------------------------------------------------------


async def test_a_local_un_claim_sends_a_signed_release_then_wipes_the_key(
    running: Running, portal: FakePortal, key: BaseKey
) -> None:
    assert (running.state / "agent_config.json").exists()

    status = await running.unclaim()

    [release] = portal.releases
    assert release.path == RELEASE_PATH
    assert release.thumbprint == key.thumbprint
    assert release.body == {}
    assert status.state == "not_connected"
    assert status.base_id is None
    assert wiped(running.state)
    assert read_base_id(running.state) is None
    assert (await running.status()).state == "not_connected"


async def test_after_a_local_un_claim_the_agent_contacts_nothing(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    await running.unclaim()
    seen = len(portal.requests)

    await clock.run(hours=1)

    assert len(portal.requests) == seen


async def test_a_local_un_claim_wipes_the_key_even_if_the_portal_is_unreachable(
    running: Running, portal: FakePortal
) -> None:
    portal.down = True

    status = await running.unclaim()

    assert status.state == "not_connected"
    assert wiped(running.state)


async def test_a_local_un_claim_the_portal_already_forgot_still_wipes_the_key(
    running: Running, portal: FakePortal
) -> None:
    portal.unclaimed = True

    status = await running.unclaim()

    assert status.state == "not_connected"
    assert wiped(running.state)


async def test_un_claim_on_an_unclaimed_agent_does_nothing(
    dirs: tuple[Path, Path], portal: FakePortal, clock: FakeClock
) -> None:
    r = Running(*dirs, portal, clock)
    await r.start()
    try:
        status = await r.unclaim()
    finally:
        await r.stop()

    assert status.state == "not_connected"
    assert portal.requests == []


async def test_a_restarted_agent_stays_unclaimed(running: Running) -> None:
    await running.unclaim()

    await running.restart()

    assert (await running.status()).state == "not_connected"


async def test_claiming_again_makes_a_new_key(
    running: Running, portal: FakePortal, key: BaseKey
) -> None:
    await running.unclaim()

    pairing = await start_claim(running.sock)
    portal.approve(base_id="n3wbas3idx")
    status = await running.until("connected")

    assert pairing.user_code
    assert status.base_id == "n3wbas3idx"
    new_key = BaseKey.from_pem((running.state / "base_key.pem").read_bytes())
    assert new_key.thumbprint != key.thumbprint
    assert portal.agent_polls[-1].thumbprint == new_key.thumbprint


# --- Un-claimed on the Portal --------------------------------------------------------


async def test_a_401_unclaimed_wipes_the_key_and_the_agent_goes_idle(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    portal.unclaimed = True

    status = await running.until("not_connected")

    assert wiped(running.state)
    assert status.message
    assert "Un-claimed on the Portal" in status.message
    assert portal.releases == []
    seen = len(portal.requests)
    await clock.run(hours=1)
    assert len(portal.requests) == seen
