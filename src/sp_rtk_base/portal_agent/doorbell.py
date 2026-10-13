"""The agent's Doorbell (ADR 0002): keepalives to the Doorbell host from the
agent config, and rings back down that path.

**Keepalives.** While the agent config names a Doorbell host, the agent
sends a keepalive signed by its base key to it every
:data:`~sp_rtk_portal_protocol.DOORBELL_INTERVAL` seconds, with jitter, all
from one UDP socket: that holds a return path open through the base's
router. The Doorbell server relays each one to the Portal, which counts it as
a sign of life.

**The host is checked before every packet.** A Portal could name a Doorbell
host that resolves into the base's LAN and so steer the agent's UDP at it.
The agent resolves the host before each keepalive and refuses it if any
answer is not a global address (private, loopback, link-local, carrier-grade
NAT, ...): it logs that, sends nothing and stays on polling. Re-resolving
every time means a later DNS change can't slip a LAN address in. The unit's
``IPAddressDeny`` blocks the LAN ranges as well.

**Health and wake mode.** ``off`` (no host), ``ignored`` (the host was
refused), ``unavailable`` (keepalives go out, but the Portal hasn't said it
hears them) or ``ok``: the latest Check-in reply said the Portal hears them
and gave the ring key. Only ``ok`` is Doorbell mode, in which the agent stops
polling; any change of health makes the agent Check in at its next beat, so
the Portal's list follows.

**Rings.** A datagram from the Doorbell address that ``open_ring`` accepts
with the ring key carries a Wake's nonce; the agent then opens its tunnel
exactly as for a polled Wake (the nonce echoed in a request signed by its
base key, to its own checked tunnel URL). Anything else is dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import random
import socket
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from sp_rtk_portal_protocol import (
    DOORBELL_IGNORED,
    DOORBELL_INTERVAL,
    DOORBELL_JITTER,
    DOORBELL_OFF,
    DOORBELL_OK,
    DOORBELL_UNAVAILABLE,
    BaseKey,
    DoorbellError,
)
from sp_rtk_portal_protocol import doorbell as packets

logger = logging.getLogger(__name__)

Resolve = Callable[[str, int], Awaitable[list[str]]]
"""``resolve(host, port)`` -> the addresses it resolves to; ``OSError`` if
none."""

OnDatagram = Callable[[bytes, tuple[str, int]], None]


class Datagrams(Protocol):
    """An open UDP socket (an asyncio ``DatagramTransport`` is one)."""

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None: ...

    def close(self) -> None: ...


OpenUdp = Callable[[str, OnDatagram], Awaitable[Datagrams]]
"""``open_udp(address, on_datagram)``: a UDP socket able to reach
``address`` (its family), handing every datagram it receives to
``on_datagram(data, (source address, port))``."""


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_DGRAM
    )
    return [str(info[4][0]) for info in infos]


class _Receiver(asyncio.DatagramProtocol):
    def __init__(self, on_datagram: OnDatagram) -> None:
        self._on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        self._on_datagram(data, (str(addr[0]), int(addr[1])))

    def error_received(self, exc: Exception) -> None:
        logger.info("Doorbell socket error: %s", exc)


async def open_udp(address: str, on_datagram: OnDatagram) -> Datagrams:
    v6 = ":" in address
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: _Receiver(on_datagram),
        local_addr=("::" if v6 else "0.0.0.0", 0),
        family=socket.AF_INET6 if v6 else socket.AF_INET,
    )
    return transport


async def checked_address(host: str, port: int, resolve: Resolve) -> str | None:
    """The address to send to, or ``None`` (logged) if ``host`` must be
    ignored. Call it before every use."""
    try:
        addresses = await resolve(host, port)
    except OSError as exc:
        logger.warning("Ignoring the Doorbell host %s: %s", host, exc)
        return None
    if not addresses:
        logger.warning("Ignoring the Doorbell host %s: no addresses", host)
        return None
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            ip = None
        if ip is None or not ip.is_global:
            logger.warning(
                "Ignoring the Doorbell host %s: it resolves to %s, which isn't "
                "a public address (private, loopback or link-local); staying "
                "on polling",
                host,
                address,
            )
            return None
    return addresses[0]


def jitter() -> float:
    return random.uniform(-DOORBELL_JITTER, DOORBELL_JITTER)


class Doorbell:
    """One Claimed base's Doorbell. :meth:`follow` it to the agent config's
    ``(host, port)`` each beat; tell it what each Check-in reply said with
    :meth:`portal_says`."""

    def __init__(
        self,
        *,
        key: BaseKey,
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]],
        resolve: Resolve,
        open_udp: OpenUdp,
        on_ring: Callable[[str], None],
        on_change: Callable[[], None],
        jitter: Callable[[], float] = jitter,
    ) -> None:
        self._key = key
        self._now = now
        self._sleep = sleep
        self._resolve = resolve
        self._open_udp = open_udp
        self._on_ring = on_ring
        self._on_change = on_change
        self._jitter = jitter
        self._target: tuple[str, int] | None = None
        self._usable = False
        self._heard = False
        self._ring_key: str | None = None
        self._socket: Datagrams | None = None
        self._socket_v6 = False
        self._sent_to: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def health(self) -> str:
        if self._target is None:
            return DOORBELL_OFF
        if not self._usable:
            return DOORBELL_IGNORED
        if self._heard and self._ring_key:
            return DOORBELL_OK
        return DOORBELL_UNAVAILABLE

    async def follow(self, target: tuple[str, int] | None) -> None:
        """Use ``target`` from now on. A new one gets its first keepalive
        at once (so the next Check-in reports on it), then one every beat."""
        if target == self._target:
            return
        before = self.health
        await self.close()
        self._target, self._heard, self._ring_key = target, False, None
        if target is not None:
            await self._keepalive()
            self._task = asyncio.create_task(self._keep_alive())
        self._changed(before)

    def portal_says(self, heard: bool, ring_key: str | None) -> None:
        """What the latest Check-in reply said about this base's Doorbell."""
        before = self.health
        self._heard, self._ring_key = heard, ring_key
        self._changed(before)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._sent_to = None
        self._usable = False

    def _changed(self, before: str) -> None:
        if self.health != before:
            logger.info("Doorbell: %s (was %s)", self.health, before)
            self._on_change()

    async def _keep_alive(self) -> None:
        while True:
            await self._sleep(DOORBELL_INTERVAL + self._jitter())
            before = self.health
            await self._keepalive()
            self._changed(before)

    async def _keepalive(self) -> None:
        assert self._target is not None
        host, port = self._target
        address = await checked_address(host, port, self._resolve)
        if address is None:
            # Rings are accepted only from where the last keepalive went:
            # once the host is refused, from nowhere.
            self._sent_to = None
            self._usable = False
            return
        try:
            sock = await self._socket_for(address)
            sock.sendto(packets.keepalive(self._key, now=self._now()), (address, port))
        except OSError as exc:
            logger.info("Couldn't send a Doorbell keepalive to %s: %s", host, exc)
        self._sent_to = address
        self._usable = True

    async def _socket_for(self, address: str) -> Datagrams:
        v6 = ":" in address
        if self._socket is not None and self._socket_v6 != v6:
            self._socket.close()
            self._socket = None
        if self._socket is None:
            self._socket = await self._open_udp(address, self._datagram)
            self._socket_v6 = v6
        return self._socket

    def _datagram(self, data: bytes, source: tuple[str, int]) -> None:
        if (
            self._ring_key is None
            or self._target is None
            or source != (self._sent_to, self._target[1])
        ):
            logger.info("Dropped a datagram from %s", source)
            return
        try:
            nonce = packets.open_ring(self._ring_key, data, now=self._now())
        except DoorbellError as exc:
            logger.info("Dropped a Doorbell datagram from %s: %s", source, exc)
            return
        logger.info("Rung by the Doorbell")
        self._on_ring(nonce)
