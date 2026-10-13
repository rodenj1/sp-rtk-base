"""A Claimed agent polls, Checks in and follows the agent config (ticket #87).

The agent runs behind its control socket against a fake Portal that checks
every request's proof, on virtual time. The real Portal and agent poll
together in sp-rtk-portal's seam-1 suite.
"""

from __future__ import annotations

import itertools
import json
import logging
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sp_rtk_portal_protocol import (
    CHECK_IN_PATH,
    POLL_PATH,
    PROTOCOL_VERSION,
    AgentConfig,
    BaseKey,
    LanAddress,
    Notice,
)

from sp_rtk_base import __version__
from sp_rtk_base.portal_agent.state import Claim, save_claim
from tests.fixtures.fake_portal import FakeClock, FakePortal, Seen
from tests.fixtures.running_agent import Running

pytestmark = pytest.mark.asyncio

DEFAULT_TUNNEL = "wss://portal.test/agent/tunnel"


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
def claimed(dirs: tuple[Path, Path], clock: FakeClock) -> Path:
    """A state directory holding a Claim, as a Claimed base's does."""
    state = dirs[1]
    at = clock.now().isoformat()
    save_claim(
        state,
        BaseKey.generate(),
        Claim(
            base_id="k3mnp4qrst",
            account="op@example.com",
            portal="portal.test",
            claimed_at=at,
            last_contact=at,
        ),
    )
    return state


@pytest_asyncio.fixture()
async def running(
    dirs: tuple[Path, Path], claimed: Path, portal: FakePortal, clock: FakeClock
) -> AsyncIterator[Running]:
    r = Running(dirs[0], claimed, portal, clock)
    await r.start()
    try:
        yield r
    finally:
        await r.stop()


def _gaps(seen: list[Seen]) -> list[float]:
    return [(b.at - a.at).total_seconds() for a, b in itertools.pairwise(seen)]


# --- Polls and Check-ins --------------------------------------------------------


async def test_a_claimed_agent_polls_every_20_s_and_checks_in_every_5_min(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    await clock.run(minutes=10)

    assert len(portal.agent_polls) == 31  # at 0, 20, ... 600 s
    assert set(_gaps(portal.agent_polls)) == {20}
    assert [c.at for c in portal.check_ins] == [
        portal.agent_polls[0].at,
        portal.agent_polls[15].at,
        portal.agent_polls[30].at,
    ]
    assert {r.version for r in portal.requests} == {str(PROTOCOL_VERSION)}
    assert portal.agent_polls[0].body == {"state": "connected"}


async def test_a_check_in_carries_the_version_doorbell_health_and_lan_addresses(
    running: Running, portal: FakePortal
) -> None:
    [check_in] = portal.check_ins

    assert check_in.body == {
        "software_version": __version__,
        "doorbell": "off",
        "lan_addresses": [
            {"address": "192.168.1.20", "interface": "eth0", "primary": True}
        ],
        "paused": False,
    }


async def test_the_agent_reports_its_last_contact_and_wake_mode(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    await clock.run(seconds=45)

    status = await running.status()
    assert status.state == "connected"
    assert status.last_contact == portal.agent_polls[-1].at
    assert status.wake_mode == "polling"
    assert status.poll_interval == 20
    assert not status.metered


async def test_last_contact_survives_a_restart(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    await clock.run(minutes=5)
    contacted = (await running.status()).last_contact

    portal.down = True
    await running.restart()

    assert (await running.status()).last_contact == contacted


async def test_a_metered_base_polls_every_60_s(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    portal.metered = True
    await clock.run(minutes=5)

    assert set(_gaps(portal.agent_polls[1:])) == {60}
    status = await running.status()
    assert status.metered
    assert status.poll_interval == 60


async def test_an_unreachable_portal_is_retried_at_the_poll_interval(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    portal.down = True
    await clock.run(seconds=60)
    portal.down = False
    await clock.run(seconds=20)

    assert [
        (p.at - portal.agent_polls[0].at).total_seconds() for p in portal.agent_polls
    ] == [0, 80]
    assert (await running.status()).last_contact == portal.agent_polls[-1].at


# --- Rate limits (ticket #98) ------------------------------------------------------


def _since_first(seen: list[Seen]) -> list[float]:
    return [(s.at - seen[0].at).total_seconds() for s in seen]


async def test_a_poll_refused_429_waits_the_retry_after_before_polling_again(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    portal.rate_limited[POLL_PATH] = 70
    await clock.run(seconds=20)  # this poll gets 429
    portal.rate_limited.clear()
    await clock.run(seconds=100)

    assert _since_first(portal.agent_polls) == [0, 20, 90, 110]
    assert (await running.status()).state == "connected"


async def test_a_poll_refused_429_without_retry_after_waits_twice_the_interval(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    portal.rate_limited[POLL_PATH] = None
    await clock.run(seconds=20)
    portal.rate_limited.clear()
    await clock.run(seconds=60)

    assert _since_first(portal.agent_polls) == [0, 20, 60, 80]


async def test_a_check_in_refused_429_is_retried_later_and_polls_go_on(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    await clock.run(seconds=10)
    portal.rate_limited[CHECK_IN_PATH] = 120
    await running.restart()  # a restart Checks in at once: refused
    start = portal.check_ins[-1].at
    portal.rate_limited.clear()
    await clock.run(seconds=140)

    assert [(c.at - start).total_seconds() for c in portal.check_ins[-2:]] == [0, 120]
    polls = [p for p in portal.agent_polls if p.at >= start]
    assert len(polls) == 8
    assert set(_gaps(polls)) == {20}
    assert (polls[0].at - start).total_seconds() == 0


# --- Agent config -----------------------------------------------------------------


async def test_without_a_config_the_agent_uses_its_built_in_defaults(
    dirs: tuple[Path, Path], claimed: Path, portal: FakePortal, clock: FakeClock
) -> None:
    portal.down = True
    r = Running(dirs[0], claimed, portal, clock)
    await r.start()
    try:
        status = await r.status()
    finally:
        await r.stop()

    assert status.config_revision is None
    assert status.poll_interval == 20
    assert status.tunnel_url == DEFAULT_TUNNEL


async def test_a_new_revision_is_applied_on_the_next_poll(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    portal.config = AgentConfig(revision=2, poll_interval=30, check_in_interval=600)
    await clock.run(seconds=20)  # this poll brings revision 2
    await clock.run(seconds=60)

    assert _gaps(portal.agent_polls) == [20, 30, 30]
    status = await running.status()
    assert status.config_revision == 2
    assert status.poll_interval == 30


async def test_an_unchanged_revision_is_not_applied_again(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    # Same revision as the agent holds, different values: ignored.
    portal.config = AgentConfig(revision=1, poll_interval=45)
    await clock.run(seconds=100)

    assert set(_gaps(portal.agent_polls)) == {20}
    assert (await running.status()).poll_interval == 20


async def test_the_config_is_kept_across_a_restart(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    portal.config = AgentConfig(revision=2, poll_interval=30)
    await clock.run(seconds=20)

    portal.down = True
    await running.restart()

    status = await running.status()
    assert status.config_revision == 2
    assert status.poll_interval == 30
    saved = json.loads((running.state / "agent_config.json").read_text())
    assert saved["revision"] == 2


async def test_a_config_value_out_of_range_is_ignored_and_logged(
    running: Running, portal: FakePortal, clock: FakeClock, caplog
) -> None:
    portal.config = AgentConfig(revision=2, poll_interval=1, check_in_interval=600)
    with caplog.at_level(logging.WARNING):
        await clock.run(seconds=20)

    status = await running.status()
    assert status.config_revision == 2
    assert status.poll_interval == 20
    assert "poll_interval" in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "wss://evil.example.com/agent/tunnel",
        "ws://portal.test/agent/tunnel",
        "https://portal.test/agent/tunnel",
        "wss://portal.test.evil.example.com/agent/tunnel",
        "wss://user@portal.test/agent/tunnel",
        "wss://portal.test:8443/agent/tunnel",
    ],
)
async def test_a_tunnel_url_off_the_portal_host_is_ignored_and_logged(
    running: Running, portal: FakePortal, clock: FakeClock, caplog, url: str
) -> None:
    portal.config = AgentConfig(revision=2, tunnel_url=url)
    with caplog.at_level(logging.WARNING):
        await clock.run(seconds=20)

    status = await running.status()
    assert status.config_revision == 2
    assert status.tunnel_url == DEFAULT_TUNNEL
    assert url in caplog.text


async def test_a_wss_tunnel_url_on_the_portal_host_is_used(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    portal.config = AgentConfig(revision=2, tunnel_url="wss://portal.test/t2")
    await clock.run(seconds=20)

    assert (await running.status()).tunnel_url == "wss://portal.test/t2"


# --- Doorbell host ----------------------------------------------------------------


async def _with_doorbell(running: Running, portal: FakePortal, clock: FakeClock):
    portal.config = AgentConfig(
        revision=2, doorbell_host="bell.example.com", doorbell_port=4500
    )
    await clock.run(seconds=20)  # applied
    await clock.run(minutes=5)  # the next Check-in reports on it


@pytest.mark.parametrize(
    "address", ["192.168.1.5", "10.1.2.3", "172.16.0.9", "127.0.0.1", "169.254.1.1"]
)
async def test_a_doorbell_host_resolving_to_a_private_address_is_ignored(
    running: Running,
    portal: FakePortal,
    clock: FakeClock,
    caplog,
    address: str,
) -> None:
    running.dns.records["bell.example.com"] = [address]
    with caplog.at_level(logging.WARNING):
        await _with_doorbell(running, portal, clock)

    assert portal.check_ins[-1].body["doorbell"] == "ignored"
    status = await running.status()
    assert status.wake_mode == "polling"
    assert status.doorbell == "ignored"
    assert "bell.example.com" in caplog.text and address in caplog.text


async def test_a_doorbell_host_is_resolved_again_before_each_use(
    running: Running, portal: FakePortal, clock: FakeClock, caplog
) -> None:
    running.dns.records["bell.example.com"] = ["93.184.215.14"]
    await _with_doorbell(running, portal, clock)
    assert portal.check_ins[-1].body["doorbell"] == "unavailable"

    # A later DNS change to a LAN address is caught.
    running.dns.records["bell.example.com"] = ["93.184.215.14", "192.168.1.1"]
    with caplog.at_level(logging.WARNING):
        await clock.run(minutes=5)

    assert portal.check_ins[-1].body["doorbell"] == "ignored"
    assert "192.168.1.1" in caplog.text


async def test_a_doorbell_host_that_doesnt_resolve_is_ignored(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    await _with_doorbell(running, portal, clock)

    assert portal.check_ins[-1].body["doorbell"] == "ignored"


# --- Notices and upgrades ---------------------------------------------------------


async def test_notices_from_the_portal_are_shown(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    portal.notices = [Notice(text="Protocol 1 is retired on 2027-06-01.")]
    await clock.run(seconds=20)

    assert (await running.status()).notices == ["Protocol 1 is retired on 2027-06-01."]

    portal.notices = []
    await clock.run(seconds=20)
    assert (await running.status()).notices == []


async def test_426_stops_polling_and_retries_once_a_day(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    portal.min_version = PROTOCOL_VERSION + 1
    await clock.run(seconds=20)
    refused = len(portal.requests)

    status = await running.status()
    assert status.state == "connected"
    assert status.needs_upgrade
    assert status.message == "Too old, upgrade."
    assert status.base_id == "k3mnp4qrst"  # keeps its Claim

    await clock.run(hours=23, minutes=59)
    assert len(portal.requests) == refused
    await clock.run(minutes=1)
    assert len(portal.requests) == refused + 1


async def test_after_an_upgrade_polling_resumes_with_the_same_claim(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    portal.min_version = PROTOCOL_VERSION + 1
    await clock.run(seconds=20)
    key = (running.state / "base_key.pem").read_bytes()

    running.protocol_version = PROTOCOL_VERSION + 1  # sp-rtk-base upgraded
    await running.restart()
    await clock.run(seconds=40)

    status = await running.status()
    assert not status.needs_upgrade
    assert status.base_id == "k3mnp4qrst"
    assert (running.state / "base_key.pem").read_bytes() == key
    assert portal.agent_polls[-1].version == str(PROTOCOL_VERSION + 1)
    assert set(_gaps(portal.agent_polls[-3:])) == {20}


async def test_a_lan_address_list_is_sent_as_the_agent_finds_it(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    running.lan = [
        LanAddress(address="10.0.0.7", interface="wlan0", primary=True),
        LanAddress(address="192.168.4.1", interface="eth0", primary=False),
    ]
    await clock.run(minutes=5)

    assert portal.check_ins[-1].body["lan_addresses"] == [
        {"address": "10.0.0.7", "interface": "wlan0", "primary": True},
        {"address": "192.168.4.1", "interface": "eth0", "primary": False},
    ]
