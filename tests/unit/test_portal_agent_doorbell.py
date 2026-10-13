"""The agent's Doorbell (ticket #100): keepalives to a checked Doorbell
host, Doorbell mode once the Portal hears them, and a ring that Wakes it.

The agent runs against the fake Portal on virtual time, with its UDP faked
(:class:`~tests.fixtures.fake_udp.FakeUdp`). The real Portal, agent and a
stub Doorbell server ring together in sp-rtk-portal's seam-1 suite.
"""

from __future__ import annotations

import itertools
import logging
import tempfile
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sp_rtk_portal_protocol import (
    PROTOCOL_VERSION,
    AgentConfig,
    BaseKey,
    LiveSession,
    doorbell,
)

from sp_rtk_base.portal_agent.client import pause
from sp_rtk_base.portal_agent.state import Claim, save_claim
from tests.fixtures.fake_portal import FakeClock, FakePortal
from tests.fixtures.fake_tunnel import TunnelEndpoint
from tests.fixtures.fake_udp import FakeUdp
from tests.fixtures.running_agent import Running

pytestmark = pytest.mark.asyncio

BELL = "bell.example.com"
BELL_IP = "93.184.215.14"
PORT = 4500
NONCE = "r1ng-n0nce"


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


@pytest.fixture()
def udp() -> FakeUdp:
    return FakeUdp()


@pytest_asyncio.fixture()
async def endpoint(clock: FakeClock) -> AsyncIterator[TunnelEndpoint]:
    e = TunnelEndpoint(clock)
    await e.start()
    try:
        yield e
    finally:
        await e.stop()


@pytest_asyncio.fixture()
async def running(
    dirs: tuple[Path, Path],
    portal: FakePortal,
    clock: FakeClock,
    key: BaseKey,
    udp: FakeUdp,
    endpoint: TunnelEndpoint,
) -> AsyncIterator[Running]:
    sock, state = dirs
    at = clock.now().isoformat()
    save_claim(
        state,
        key,
        Claim(
            base_id="k3mnp4qrst",
            account="op@example.com",
            portal="portal.test",
            claimed_at=at,
            last_contact=at,
        ),
    )
    r = Running(sock, state, portal, clock)
    await r.start(open_udp=udp, tunnel_connect=endpoint.connector())
    try:
        yield r
    finally:
        await r.stop()


async def _ring_the_bell(running: Running, portal: FakePortal, clock: FakeClock):
    """The Portal names a public Doorbell host and the agent picks it up."""
    running.dns.records[BELL] = [BELL_IP]
    portal.config = AgentConfig(revision=2, doorbell_host=BELL, doorbell_port=PORT)
    await clock.run(seconds=40)  # applied, then in use at the next beat


async def _heard(running: Running, portal: FakePortal, clock: FakeClock) -> str:
    """... and the Portal hears it: the next Check-in says so."""
    await _ring_the_bell(running, portal, clock)
    portal.doorbell_heard = True
    portal.ring_key = doorbell.new_ring_key()
    await running.until_status(lambda s: s.wake_mode == "doorbell", "Doorbell mode")
    return portal.ring_key


# --- No Doorbell ----------------------------------------------------------------


async def test_without_a_doorbell_host_the_agent_sends_no_udp(
    running: Running, udp: FakeUdp, clock: FakeClock
) -> None:
    await clock.run(minutes=10)

    assert udp.sockets == [] and udp.sent == []
    status = await running.status()
    assert (status.doorbell, status.wake_mode) == ("off", "polling")


@pytest.mark.parametrize("address", ["192.168.1.5", "127.0.0.1", "169.254.1.1"])
async def test_a_doorbell_host_resolving_to_a_private_address_gets_no_udp(
    running: Running,
    portal: FakePortal,
    udp: FakeUdp,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    address: str,
) -> None:
    running.dns.records[BELL] = [address]
    portal.config = AgentConfig(revision=2, doorbell_host=BELL, doorbell_port=PORT)
    portal.doorbell_heard = True  # a lying Portal changes nothing
    with caplog.at_level(logging.WARNING):
        await clock.run(minutes=10)

    assert udp.sent == []
    assert portal.check_ins[-1].body["doorbell"] == "ignored"
    assert (await running.status()).wake_mode == "polling"
    assert BELL in caplog.text and address in caplog.text


# --- Keepalives -----------------------------------------------------------------


async def test_the_agent_sends_a_signed_keepalive_every_20_s_with_jitter(
    running: Running, portal: FakePortal, udp: FakeUdp, clock: FakeClock, key: BaseKey
) -> None:
    await _ring_the_bell(running, portal, clock)
    sent_from = clock.now()
    await clock.run(minutes=5)

    assert len(udp.sockets) == 1  # one socket: one NAT mapping
    times: list[datetime] = []
    for packet, to, _ in udp.sent:
        assert to == (BELL_IP, PORT)
        at = doorbell.read_keepalive(packet).at
        heard = doorbell.verify_keepalive(packet, key.public_jwk, now=at)
        assert heard.thumbprint == key.thumbprint
        times.append(at)
    assert times[0] <= sent_from
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(times)]
    assert all(15 <= g <= 25 for g in gaps), gaps
    assert len(set(gaps)) > 1  # jittered
    # Still polling until the Portal says it hears them.
    assert portal.check_ins[-1].body["doorbell"] == "unavailable"
    assert (await running.status()).wake_mode == "polling"


async def test_the_doorbell_host_is_checked_again_before_each_keepalive(
    running: Running,
    portal: FakePortal,
    udp: FakeUdp,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _ring_the_bell(running, portal, clock)
    before = len(running.dns.asked)
    await clock.run(minutes=1)
    assert len(running.dns.asked) - before >= 2  # one per keepalive

    running.dns.records[BELL] = ["192.168.1.1"]
    sent = len(udp.sent)
    with caplog.at_level(logging.WARNING):
        await clock.run(minutes=2)

    assert len(udp.sent) == sent
    assert "192.168.1.1" in caplog.text
    assert (await running.status()).doorbell == "ignored"


# --- Doorbell mode ------------------------------------------------------------------


async def test_once_the_portal_hears_the_doorbell_the_agent_stops_polling(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    await _heard(running, portal, clock)
    await clock.run(seconds=30)  # the Check-in reporting the change
    polls = len(portal.agent_polls)

    await clock.run(minutes=10)

    assert len(portal.agent_polls) == polls
    assert portal.check_ins[-1].body["doorbell"] == "ok"
    status = await running.status()
    assert (status.doorbell, status.wake_mode) == ("ok", "doorbell")


async def test_when_the_portal_stops_hearing_it_the_agent_polls_again(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    await _heard(running, portal, clock)
    portal.doorbell_heard = False

    status = await running.until_status(lambda s: s.wake_mode == "polling", "polling")
    polls = len(portal.agent_polls)
    await clock.run(minutes=1)

    assert status.doorbell == "unavailable"
    assert len(portal.agent_polls) >= polls + 3


async def test_in_doorbell_mode_the_agent_polls_while_its_tunnel_is_open(
    running: Running,
    portal: FakePortal,
    clock: FakeClock,
    udp: FakeUdp,
    endpoint: TunnelEndpoint,
) -> None:
    """Only poll replies say who is connected: the local page's list of
    Remote sessions stays current while the tunnel is open."""
    ring_key = await _heard(running, portal, clock)
    await clock.run(seconds=30)
    endpoint.nonce = NONCE
    udp.deliver(doorbell.ring(ring_key, NONCE, now=clock.now()), (BELL_IP, PORT))
    await endpoint.wait_open()
    await running.until_status(lambda s: s.tunnel == "open", "tunnel open")
    portal.sessions = [
        LiveSession(who="sam@example.com", since=clock.now().isoformat())
    ]
    polls = len(portal.agent_polls)

    await clock.run(minutes=1)

    assert len(portal.agent_polls) >= polls + 3  # every 20 s
    status = await running.status()
    assert [s.who for s in status.sessions] == ["sam@example.com"]
    assert status.wake_mode == "doorbell"

    portal.sessions = []
    await clock.run(seconds=20)
    assert (await running.status()).sessions == []


async def test_check_ins_say_whether_remote_access_is_paused(
    running: Running, portal: FakePortal, clock: FakeClock
) -> None:
    await _heard(running, portal, clock)
    await clock.run(minutes=5)
    assert portal.check_ins[-1].body["paused"] is False

    await pause(running.sock)
    await clock.run(minutes=5)

    assert portal.check_ins[-1].body["paused"] is True


async def test_a_426_stops_the_keepalives_until_the_portal_accepts_the_agent(
    running: Running, portal: FakePortal, clock: FakeClock, udp: FakeUdp
) -> None:
    """Too old for the Portal: nothing may Wake the base until it is
    accepted again, so no return path is held open for a ring."""
    await _heard(running, portal, clock)
    portal.min_version = PROTOCOL_VERSION + 1
    await running.until_status(lambda s: s.needs_upgrade, "needs upgrade")
    sent = len(udp.sent)

    await clock.run(hours=1)

    assert len(udp.sent) == sent
    assert all(s.closed for s in udp.sockets)
    assert (await running.status()).doorbell == "off"

    portal.min_version = None
    await clock.run(days=1)  # the daily retry is accepted

    status = await running.until_status(lambda s: not s.needs_upgrade, "accepted")
    await clock.run(minutes=1)
    assert len(udp.sent) > sent
    assert (await running.status()).doorbell != "off"


# --- Rings ----------------------------------------------------------------------------


async def test_a_ring_opens_the_tunnel_echoing_its_nonce_without_a_poll(
    running: Running,
    portal: FakePortal,
    clock: FakeClock,
    udp: FakeUdp,
    endpoint: TunnelEndpoint,
) -> None:
    ring_key = await _heard(running, portal, clock)
    endpoint.nonce = NONCE

    udp.deliver(doorbell.ring(ring_key, NONCE, now=clock.now()), (BELL_IP, PORT))

    await endpoint.wait_open()
    [opened] = endpoint.opens
    assert opened.nonce == NONCE and opened.accepted


@pytest.mark.parametrize("forgery", ["other key", "other source", "garbage"])
async def test_a_forged_ring_is_ignored(
    running: Running,
    portal: FakePortal,
    clock: FakeClock,
    udp: FakeUdp,
    endpoint: TunnelEndpoint,
    forgery: str,
) -> None:
    ring_key = await _heard(running, portal, clock)
    packet, source = doorbell.ring(ring_key, NONCE, now=clock.now()), (BELL_IP, PORT)
    if forgery == "other key":
        packet = doorbell.ring(doorbell.new_ring_key(), NONCE, now=clock.now())
    elif forgery == "other source":
        source = ("203.0.113.66", PORT)
    else:
        packet = b"\x00" * 60

    udp.deliver(packet, source)
    await clock.run(seconds=5)

    assert endpoint.opens == []


async def test_once_the_doorbell_host_moves_into_the_lan_its_old_address_cannot_ring(
    running: Running,
    portal: FakePortal,
    clock: FakeClock,
    udp: FakeUdp,
    endpoint: TunnelEndpoint,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ring_key = await _heard(running, portal, clock)
    running.dns.records[BELL] = ["192.168.1.1"]
    await clock.run(seconds=30)  # the next keepalive finds it refused
    assert (await running.status()).doorbell == "ignored"
    endpoint.nonce = NONCE

    with caplog.at_level(logging.INFO):
        udp.deliver(doorbell.ring(ring_key, NONCE, now=clock.now()), (BELL_IP, PORT))
        await clock.run(seconds=5)

    assert "Rung by the Doorbell" not in caplog.text
    assert (await running.status()).tunnel == "closed"


async def test_a_ring_is_ignored_while_remote_access_is_paused(
    running: Running,
    portal: FakePortal,
    clock: FakeClock,
    udp: FakeUdp,
    endpoint: TunnelEndpoint,
) -> None:
    ring_key = await _heard(running, portal, clock)
    endpoint.nonce = NONCE
    await pause(running.sock)

    udp.deliver(doorbell.ring(ring_key, NONCE, now=clock.now()), (BELL_IP, PORT))
    await clock.run(seconds=5)

    assert endpoint.opens == []
    assert (await running.status()).tunnel == "closed"
