"""How the agent finds the Portal: DNS-over-HTTPS to public resolvers.

A home network's DNS can answer the Portal's name with a LAN address: a
Firewalla, for one, intercepts every port-53 query (even one sent to
1.1.1.1) and may point ``*.big-spray.com`` at a LAN load balancer. The
agent's unit denies it the LAN (``IPAddressDeny``), so with that answer it
couldn't reach the Portal at all. So the agent asks for the Portal host's A
records over DoH (RFC 8484) instead, from endpoints named by IP address so
that DoH itself needs no DNS.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import ssl
import struct
import time
from collections.abc import Callable, Sequence
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

ENDPOINTS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query")
"""Cloudflare and Google, named by IP: their certificates cover these
addresses, and both answer RFC 8484 over HTTP/1.1. Two operators, so one
outage or block leaves the other."""

TIMEOUT_S = 3.0
"""How long one endpoint gets to answer."""

MIN_TTL_S = 300
MAX_TTL_S = 3600
"""An answer is kept for its TTL, within these bounds: polls every 20 s
don't each cost a lookup (Cloudflare's own TTL is 300 s), and a changed
address is picked up within the hour. A connection to a kept address that
fails forgets it sooner (:meth:`DohResolver.forget`)."""

RETRY_S = 300
"""After every endpoint failed, how long the agent uses the system resolver
before trying DoH again."""

_A, _IN = 1, 1


class DnsReplyError(Exception):
    """A DoH reply that isn't a usable answer."""


def is_public(address: str) -> bool:
    """Whether ``address`` is a global one: not private, loopback,
    link-local, carrier-grade NAT, ... (the check the Doorbell host gets
    too)."""
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_global
    except ValueError:
        return False


class Names(Protocol):
    """Where the agent connects for a host name: what its clients ask."""

    async def address(self, host: str) -> str | None:
        """The address to connect to, or ``None``: connect by name."""
        ...

    def forget(self, host: str) -> None:
        """Connecting to the address given for ``host`` failed."""
        ...


class SystemNames:
    """:class:`Names` that always connect by name (the system resolver)."""

    async def address(self, host: str) -> str | None:
        return None

    def forget(self, host: str) -> None:
        pass


class DohResolver:
    """The Portal host's public IPv4 address, looked up over DoH."""

    def __init__(
        self,
        *,
        endpoints: Sequence[str] = ENDPOINTS,
        verify: ssl.SSLContext | bool = True,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self._endpoints = tuple(endpoints)
        self._verify = verify
        self._clock = clock
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._kept_until: dict[str, tuple[str | None, float]] = {}
        """Per host: the address to use (``None``: the system resolver, DoH
        having failed) and until when."""
        self._lock = asyncio.Lock()
        self._failing = False
        """Whether the fallback has been logged (until DoH answers again)."""

    async def address(self, host: str) -> str | None:
        """The address to connect to for ``host``, or ``None``: connect by
        name (the system resolver)."""
        if _skips_doh(host):
            return None
        async with self._lock:
            now = self._clock()
            kept = self._kept_until.get(host)
            if kept is not None and kept[1] > now:
                return kept[0]
            address, ttl = await self._look_up(host)
            self._kept_until[host] = (address, self._clock() + ttl)
            return address

    def forget(self, host: str) -> None:
        """Look ``host`` up again next time: connecting to its address
        failed."""
        self._kept_until.pop(host, None)

    async def _look_up(self, host: str) -> tuple[str | None, float]:
        for endpoint in self._endpoints:
            try:
                answers = await self._ask(endpoint, host)
            except (httpx.HTTPError, DnsReplyError) as exc:
                logger.info(
                    "DoH lookup of %s at %s failed: %s",
                    host,
                    endpoint,
                    exc or type(exc).__name__,
                )
                continue
            public = [(a, ttl) for a, ttl in answers if self._kept(host, endpoint, a)]
            if public:
                if self._failing:
                    self._failing = False
                    logger.info("DoH answers again: resolving %s over DoH", host)
                ttl = min(ttl for _, ttl in public)
                return public[0][0], min(max(ttl, MIN_TTL_S), MAX_TTL_S)
        if not self._failing:
            self._failing = True
            logger.warning(
                "No public DoH answer for %s; using system DNS (retrying DoH "
                "every %d s)",
                host,
                RETRY_S,
            )
        return None, RETRY_S

    @staticmethod
    def _kept(host: str, endpoint: str, address: str) -> bool:
        if is_public(address):
            return True
        logger.warning(
            "Dropped the DoH answer %s for %s from %s: it isn't a public "
            "address (private, loopback or link-local)",
            address,
            host,
            endpoint,
        )
        return False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ask(self, endpoint: str, host: str) -> list[tuple[str, int]]:
        if self._client is None:
            self._client = httpx.AsyncClient(
                # IPv4 only, like every other agent client.
                transport=httpx.AsyncHTTPTransport(
                    local_address="0.0.0.0", verify=self._verify
                ),
                timeout=self._timeout,
            )
        message = query(host)
        dns = base64.urlsafe_b64encode(message).rstrip(b"=").decode()
        resp = await self._client.get(
            endpoint,
            params={"dns": dns},
            headers={"Accept": "application/dns-message"},
        )
        if resp.status_code != 200:
            raise DnsReplyError(f"HTTP {resp.status_code}")
        return a_records(resp.content)


def _skips_doh(host: str) -> bool:
    """A local development override: an IP literal or localhost."""
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def query(host: str) -> bytes:
    """A recursive query for ``host``'s A records, with ID 0 (RFC 8484
    §4.1: the ID is no use over HTTPS)."""
    try:
        labels = [p.encode("idna") for p in host.rstrip(".").split(".")]
    except UnicodeError as exc:
        raise DnsReplyError(f"{host!r} isn't a host name") from exc
    name = b"".join(bytes([len(p)]) + p for p in labels) + b"\x00"
    header = struct.pack("!HHHHHH", 0, 0x0100, 1, 0, 0, 0)
    return header + name + struct.pack("!HH", _A, _IN)


def _skip_name(message: bytes, i: int) -> int:
    while True:
        n = message[i]
        if n & 0xC0 == 0xC0:
            return i + 2
        if n == 0:
            return i + 1
        i += 1 + n


def a_records(message: bytes) -> list[tuple[str, int]]:
    """The A records ``(address, ttl)`` in the answer section of a reply
    (an alias chain's CNAMEs are skipped). Raises :class:`DnsReplyError`
    for anything but a successful reply."""
    try:
        rid, flags, qdcount, ancount = struct.unpack("!HHHH", message[:8])
        if rid != 0 or not flags & 0x8000:
            raise DnsReplyError("not a reply to the query")
        if flags & 0x000F:
            raise DnsReplyError(f"DNS error {flags & 0x000F}")
        i = 12
        for _ in range(qdcount):
            i = _skip_name(message, i) + 4
        records: list[tuple[str, int]] = []
        for _ in range(ancount):
            i = _skip_name(message, i)
            rtype, rclass, ttl, length = struct.unpack("!HHIH", message[i : i + 10])
            i += 10
            if i + length > len(message):
                raise DnsReplyError("a truncated reply")
            if rtype == _A and rclass == _IN and length == 4:
                records.append((".".join(str(b) for b in message[i : i + 4]), ttl))
            i += length
    except (struct.error, IndexError) as exc:
        raise DnsReplyError("a malformed reply") from exc
    if not records:
        raise DnsReplyError("no A records")
    return records
