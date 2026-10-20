"""The base's private IPv4 addresses, for the Check-in.

Every RFC 1918 address on an interface that is up, with the one on the
Uplink marked primary (:mod:`sp_rtk_base.uplink`: where IPv4 traffic
actually leaves, VPNs included). The Portal uses them for Local access
(opening the base on its own LAN).

Read with ``SIOCGIFCONF``/``SIOCGIFFLAGS`` ioctls on an ``AF_INET`` socket,
and the Uplink with a connected ``AF_INET`` datagram socket: the agent's
unit allows only ``AF_INET`` and ``AF_UNIX`` sockets, so netlink
(``ip addr``) is out. Opening or connecting a datagram socket sends nothing.
"""

from __future__ import annotations

import array
import fcntl
import ipaddress
import logging
import socket
import struct

from sp_rtk_portal_protocol import LanAddress

from sp_rtk_base.uplink import picked_source, uplink

logger = logging.getLogger(__name__)

SIOCGIFCONF = 0x8912
SIOCGIFFLAGS = 0x8913
IFF_UP = 0x1
_IFREQ_SIZE = 40  # struct ifreq on 64-bit Linux
_MAX_INTERFACES = 64

PRIVATE = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def select(
    interfaces: list[tuple[str, str, bool]], source: str | None
) -> list[LanAddress]:
    """From ``(interface, address, up)`` rows: the private addresses on up
    interfaces, each once. The first on the Uplink, judged from the
    ``source`` address the kernel picked (``None``: unreachable), is
    primary and comes first."""
    # Only the Uplink's interface matters here, not whether it is a
    # Network link, so no Network links are named.
    egress = uplink(source, [(name, a) for name, a, _ in interfaces], ())
    uplink_interface = egress.interface if egress else None
    found: dict[str, str] = {}  # address -> interface, in order
    for name, address, up in interfaces:
        try:
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if up and any(ip in net for net in PRIVATE):
            found.setdefault(address, name.split(":", 1)[0])  # eth0:1 -> eth0
    primary = next((a for a, name in found.items() if name == uplink_interface), None)
    ordered = sorted(found.items(), key=lambda item: item[0] != primary)
    return [
        LanAddress(address=a, interface=name, primary=a == primary)
        for a, name in ordered
    ]


def _interfaces() -> list[tuple[str, str, bool]]:  # pragma: no cover - host ioctls
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        buf = array.array("B", b"\0" * (_IFREQ_SIZE * _MAX_INTERFACES))
        addr, _ = buf.buffer_info()
        packed = fcntl.ioctl(
            sock.fileno(),
            SIOCGIFCONF,
            struct.pack("iL", _IFREQ_SIZE * _MAX_INTERFACES, addr),
        )
        length = struct.unpack("iL", packed)[0]
        raw = buf.tobytes()[:length]
        rows: list[tuple[str, str, bool]] = []
        for offset in range(0, length, _IFREQ_SIZE):
            ifreq = raw[offset : offset + _IFREQ_SIZE]
            name = ifreq[:16].split(b"\0", 1)[0].decode(errors="replace")
            address = socket.inet_ntoa(ifreq[20:24])
            flags_req = struct.pack("16sH", name.encode()[:15], 0)
            try:
                flags = struct.unpack(
                    "16sH", fcntl.ioctl(sock.fileno(), SIOCGIFFLAGS, flags_req)[:18]
                )[1]
            except OSError:
                flags = 0
            rows.append((name, address, bool(flags & IFF_UP)))
        return rows


def lan_addresses() -> list[LanAddress]:
    """This host's private IPv4 addresses (empty if they can't be read)."""
    try:
        source = picked_source()
    except OSError as exc:  # pragma: no cover - host routes
        logger.warning("Couldn't judge the Uplink: %s", exc)
        source = None
    try:
        rows = _interfaces()
    except OSError as exc:  # pragma: no cover - host ioctls
        logger.warning("Couldn't list network interfaces: %s", exc)
        return []
    return select(rows, source)
