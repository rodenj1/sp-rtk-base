"""The Uplink rule: the interface IPv4 traffic actually leaves by.

The Uplink is the IPv4 egress interface the kernel picks for a UDP socket
``connect()``-ed to a public IPv4 address. Connecting a datagram socket
sends nothing: it only makes the kernel route it and pick a source
address, which :func:`uplink` then matches to an interface. Policy routing
(a VPN) is honoured, unlike reading the default route.

Split in two: :func:`picked_source` is the thin host shell (an ``AF_INET``
socket, no netlink, so it fits the Portal agent's unit), and
:func:`uplink` is the pure rule.

Shared by the Portal agent (its primary LAN address) and the main app.
Standard library only, so the agent can import it without pulling the
app's dependencies in.
"""

from __future__ import annotations

import errno
import socket
from collections.abc import Collection, Iterable
from dataclasses import dataclass

PROBE: tuple[str, int] = ("1.1.1.1", 443)
"""A public IPv4 address to route towards. Nothing is ever sent to it."""


@dataclass(frozen=True)
class Uplink:
    """The interface outbound IPv4 traffic leaves by. ``network_link`` is
    false for an interface that isn't a Network link (wg0, tun0)."""

    interface: str
    network_link: bool


def uplink(
    source: str | None,
    addresses: Iterable[tuple[str, str]],
    network_links: Collection[str],
) -> Uplink | None:
    """The Uplink, from the source address the kernel picked.

    ``source`` is what :func:`picked_source` returns: ``None`` when the
    network is unreachable. ``addresses`` are ``(interface, IPv4 address)``
    rows; an address label such as ``eth0:1`` counts as ``eth0``.
    ``network_links`` names the interfaces that are Network links.

    No Uplink (``None``) when the network is unreachable, or when the
    source address is on none of ``addresses`` (an address that came or
    went between the two reads).
    """
    if source is None:
        return None
    for label, address in addresses:
        if address == source:
            name = label.split(":", 1)[0]
            return Uplink(interface=name, network_link=name in network_links)
    return None


def picked_source(probe: tuple[str, int] = PROBE) -> str | None:
    """The source address the kernel picks for traffic to ``probe``, or
    ``None`` when the network is unreachable. Sends no packet.

    Raises:
        OSError: any other failure to route.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(probe)
        except OSError as exc:
            if exc.errno == errno.ENETUNREACH:  # pragma: no cover - host routes
                return None
            raise  # pragma: no cover - host routes
        address: str = sock.getsockname()[0]
        return address
