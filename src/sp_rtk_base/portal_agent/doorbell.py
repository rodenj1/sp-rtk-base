"""The Doorbell host from the agent config, resolved and checked before use.

A Portal could name a Doorbell host that resolves into the base's LAN and so
steer the agent's UDP at it. The agent therefore resolves the host before
each use and refuses it if any answer is not a global address (private,
loopback, link-local, carrier-grade NAT, ...): it logs that and stays on
polling. Re-resolving every time means a later DNS change can't slip a LAN
address in. The unit's ``IPAddressDeny`` blocks the LAN ranges as well.

The Doorbell itself (keepalives, rings) is #100's; until then a usable host
is reported as :data:`~sp_rtk_portal_protocol.DOORBELL_UNAVAILABLE`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable

from sp_rtk_portal_protocol import (
    DOORBELL_IGNORED,
    DOORBELL_OFF,
    DOORBELL_UNAVAILABLE,
)

logger = logging.getLogger(__name__)

Resolve = Callable[[str, int], Awaitable[list[str]]]
"""``resolve(host, port)`` -> the addresses it resolves to; ``OSError`` if
none."""


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_DGRAM
    )
    return [str(info[4][0]) for info in infos]


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


async def health(doorbell: tuple[str, int] | None, resolve: Resolve) -> str:
    """Doorbell health for a Check-in (the ``DOORBELL_*`` values)."""
    if doorbell is None:
        return DOORBELL_OFF
    if await checked_address(*doorbell, resolve) is None:
        return DOORBELL_IGNORED
    return DOORBELL_UNAVAILABLE
