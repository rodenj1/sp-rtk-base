"""The agent looks the Portal host up over DNS-over-HTTPS.

A home network's DNS may answer the Portal's name with a LAN address (a
Firewalla intercepting port 53 and pointing ``*.big-spray.com`` at a LAN
load balancer), which the agent's unit denies. Over DoH to public
resolvers it gets Cloudflare's public answer instead. These tests run the
agent's :class:`~sp_rtk_base.portal_agent.doh.DohResolver` against fake DoH
servers on 127.0.0.1 with certificates from a test authority.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from sp_rtk_base.portal_agent.doh import DohResolver
from tests.fixtures.fake_doh import A, FakeDoh, fake_doh
from tests.fixtures.tls import TestCa

pytestmark = pytest.mark.asyncio

PORTAL = "sp-rtk-base.big-spray.com"
CLOUDFLARE = "104.21.39.51"
CLOUDFLARE_2 = "172.67.169.142"
LAN_BALANCER = "192.168.50.180"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(scope="module")
def ca() -> TestCa:
    return TestCa()


@pytest_asyncio.fixture()
async def doh(ca: TestCa) -> AsyncIterator[FakeDoh]:
    async with fake_doh(ca) as server:
        yield server


@pytest_asyncio.fixture()
async def doh2(ca: TestCa) -> AsyncIterator[FakeDoh]:
    async with fake_doh(ca) as server:
        yield server


@pytest.fixture()
def clock() -> Clock:
    return Clock()


def resolver(ca: TestCa, clock: Clock, *servers: FakeDoh) -> DohResolver:
    return DohResolver(
        endpoints=tuple(s.url for s in servers),
        verify=ca.client_context(),
        clock=clock,
        timeout=0.5,
    )


async def test_resolves_the_portal_host_to_its_public_address(
    ca: TestCa, clock: Clock, doh: FakeDoh
) -> None:
    doh.records[PORTAL] = [(CLOUDFLARE, 300), (CLOUDFLARE_2, 300)]
    names = resolver(ca, clock, doh)
    try:
        assert await names.address(PORTAL) == CLOUDFLARE
    finally:
        await names.aclose()

    assert doh.queries == [(PORTAL, A)]


async def test_drops_a_private_answer_and_logs_it(
    ca: TestCa, clock: Clock, doh: FakeDoh, caplog: pytest.LogCaptureFixture
) -> None:
    doh.records[PORTAL] = [(LAN_BALANCER, 300), (CLOUDFLARE, 300)]
    names = resolver(ca, clock, doh)
    try:
        with caplog.at_level(logging.WARNING):
            assert await names.address(PORTAL) == CLOUDFLARE
    finally:
        await names.aclose()

    assert LAN_BALANCER in caplog.text
    assert "isn't a public address" in caplog.text


@pytest.mark.parametrize("address", [LAN_BALANCER, "127.0.0.1", "169.254.1.1"])
async def test_only_private_answers_are_no_answer(
    ca: TestCa, clock: Clock, doh: FakeDoh, address: str
) -> None:
    doh.records[PORTAL] = [(address, 300)]
    names = resolver(ca, clock, doh)
    try:
        assert await names.address(PORTAL) is None
    finally:
        await names.aclose()


@pytest.mark.parametrize("reply", ["error", "silent", "garbage", "nxdomain"])
async def test_asks_the_next_endpoint_when_one_fails(
    ca: TestCa, clock: Clock, doh: FakeDoh, doh2: FakeDoh, reply: str
) -> None:
    if reply != "nxdomain":
        doh.reply = reply
        doh.records[PORTAL] = [(CLOUDFLARE, 300)]
    doh2.records[PORTAL] = [(CLOUDFLARE_2, 300)]
    names = resolver(ca, clock, doh, doh2)
    try:
        assert await names.address(PORTAL) == CLOUDFLARE_2
    finally:
        await names.aclose()


async def test_an_endpoint_whose_certificate_doesnt_match_is_a_failure(
    clock: Clock, doh: FakeDoh
) -> None:
    doh.records[PORTAL] = [(CLOUDFLARE, 300)]
    names = DohResolver(
        endpoints=(doh.url,), verify=TestCa().client_context(), clock=clock
    )
    try:
        assert await names.address(PORTAL) is None
    finally:
        await names.aclose()
    assert doh.queries == []


async def test_falls_back_to_system_dns_logging_it_once(
    ca: TestCa, clock: Clock, doh: FakeDoh, caplog: pytest.LogCaptureFixture
) -> None:
    doh.reply = "error"
    names = resolver(ca, clock, doh)
    try:
        with caplog.at_level(logging.INFO):
            assert await names.address(PORTAL) is None
            clock.t += 20
            assert await names.address(PORTAL) is None
            clock.t += 600
            assert await names.address(PORTAL) is None
            fallbacks = [r for r in caplog.records if "system DNS" in r.message]
            assert len(fallbacks) == 1
            assert fallbacks[0].levelno == logging.WARNING

            doh.reply = "ok"
            doh.records[PORTAL] = [(CLOUDFLARE, 300)]
            clock.t += 600
            assert await names.address(PORTAL) == CLOUDFLARE
    finally:
        await names.aclose()

    assert "DoH answers again" in caplog.text


async def test_doesnt_retry_doh_on_every_poll_while_it_fails(
    ca: TestCa, clock: Clock, doh: FakeDoh
) -> None:
    doh.reply = "error"
    names = resolver(ca, clock, doh)
    try:
        await names.address(PORTAL)
        asked = len(doh.queries)
        for _ in range(10):
            clock.t += 20
            await names.address(PORTAL)
    finally:
        await names.aclose()

    # Not again in the 200 s since: DoH is tried again after 300 s.
    assert len(doh.queries) == asked


async def test_caches_the_answer_for_its_ttl(
    ca: TestCa, clock: Clock, doh: FakeDoh
) -> None:
    doh.records[PORTAL] = [(CLOUDFLARE, 600)]
    names = resolver(ca, clock, doh)
    try:
        assert await names.address(PORTAL) == CLOUDFLARE
        for _ in range(29):  # a poll every 20 s
            clock.t += 20
            assert await names.address(PORTAL) == CLOUDFLARE
        assert len(doh.queries) == 1

        doh.records[PORTAL] = [(CLOUDFLARE_2, 600)]
        clock.t += 21  # 601 s on
        assert await names.address(PORTAL) == CLOUDFLARE_2
    finally:
        await names.aclose()

    assert len(doh.queries) == 2


@pytest.mark.parametrize(
    ("ttl", "kept_s"),
    [(0, 300), (5, 300), (86400, 3600)],
)
async def test_keeps_an_answer_within_sane_bounds(
    ca: TestCa, clock: Clock, doh: FakeDoh, ttl: int, kept_s: int
) -> None:
    doh.records[PORTAL] = [(CLOUDFLARE, ttl)]
    names = resolver(ca, clock, doh)
    try:
        await names.address(PORTAL)
        clock.t += kept_s - 1
        await names.address(PORTAL)
        assert len(doh.queries) == 1
        clock.t += 1
        await names.address(PORTAL)
    finally:
        await names.aclose()

    assert len(doh.queries) == 2


async def test_forgetting_an_address_looks_it_up_again(
    ca: TestCa, clock: Clock, doh: FakeDoh
) -> None:
    doh.records[PORTAL] = [(CLOUDFLARE, 300)]
    names = resolver(ca, clock, doh)
    try:
        await names.address(PORTAL)
        names.forget(PORTAL)
        await names.address(PORTAL)
    finally:
        await names.aclose()

    assert len(doh.queries) == 2


@pytest.mark.parametrize("host", ["127.0.0.1", "203.0.113.7", "localhost"])
async def test_a_local_override_by_ip_or_localhost_skips_doh(
    ca: TestCa, clock: Clock, doh: FakeDoh, host: str
) -> None:
    names = resolver(ca, clock, doh)
    try:
        assert await names.address(host) is None
    finally:
        await names.aclose()

    assert doh.queries == []


async def test_follows_an_alias_to_its_addresses(
    ca: TestCa, clock: Clock, doh: FakeDoh
) -> None:
    doh.cnames[PORTAL] = "edge.example.net"
    doh.records["edge.example.net"] = [(CLOUDFLARE, 300)]
    names = resolver(ca, clock, doh)
    try:
        assert await names.address(PORTAL) == CLOUDFLARE
    finally:
        await names.aclose()
