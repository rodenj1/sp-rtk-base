"""How the Portal agent resolves names without being let onto the LAN.

The agent's unit denies it every private and link-local IPv4 address and
all IPv6 but ``::1`` (``IPAddressDeny``), and bind-mounts its own
``/etc/sp-rtk-base-portal-agent/resolv.conf`` over ``/etc/resolv.conf``
(root's, so the app's user, which owns ``/etc/sp-rtk-base``, can't rewrite
it).
``install.sh`` runs this module to fill that file:

- **Public DNS answers from this host** (the normal case): the file lists the
  public IPv4 resolvers that answered a query, and nothing on the LAN is
  allowed. A home LAN's resolver is its router, and ``IPAddressAllow``
  can't be limited to port 53, so allowing it would open the router's
  admin page too.
- **Public DNS is blocked**: the agent falls back to this host's resolvers.
  A drop-in clears the bind mount and allows the host's IPv4 resolvers that
  the unit denies, and ``install.sh`` warns that the agent can then reach
  those addresses on any port.

Re-running converges: whichever of the two files is stale is removed.

Usage (as ``install.sh`` runs it)::

    python -m sp_rtk_base.portal_agent.resolver \\
        --host-resolv-conf /etc/resolv.conf \\
        --agent-resolv-conf /etc/sp-rtk-base-portal-agent/resolv.conf \\
        --drop-in /etc/systemd/system/sp-rtk-base-portal-agent.service.d/resolver.conf

It prints one message; the exit status is 0 for public DNS, 3 for the
fallback (``install.sh`` shows the message as a warning) and 2 for bad
arguments.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import os
import secrets
import socket
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

PUBLIC_RESOLVERS = ("1.1.1.1", "9.9.9.9", "8.8.8.8")
"""Cloudflare, Quad9, Google: three operators, so one outage or block
leaves two. IPv4 only, as the agent has no IPv6."""

DENIED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::/0",
    )
)
"""The unit's ``IPAddressDeny`` ranges (``::1`` is allowed back)."""

TIMEOUT_S = 2.0
"""How long one public resolver gets to answer the install-time check."""

FALLBACK = 3
"""``main``'s exit status when public DNS doesn't answer."""


@dataclass(frozen=True)
class Plan:
    """What install.sh lays down for the agent's DNS."""

    resolv_conf: str | None
    """The agent's own resolv.conf, or ``None``: remove it."""
    drop_in: str | None
    """The unit drop-in, or ``None``: remove it."""
    message: str
    warn: bool


def _denied(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address == ipaddress.ip_address("::1"):
        return False
    return any(address.version == net.version and address in net for net in DENIED)


def _host_resolvers(resolv_conf: str) -> list[ipaddress.IPv4Address]:
    """The IPv4 ``nameserver`` addresses in ``resolv_conf``, each once. An
    IPv6 resolver is no use to an agent that can't open IPv6 sockets."""
    found: list[ipaddress.IPv4Address] = []
    for line in resolv_conf.splitlines():
        words = line.split()
        if len(words) < 2 or words[0] != "nameserver":
            continue
        try:
            address = ipaddress.ip_address(words[1].split("%", 1)[0])
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address) and address not in found:
            found.append(address)
    return found


def plan(
    host_resolv_conf: str,
    answering: list[str],
    tried: tuple[str, ...] = PUBLIC_RESOLVERS,
) -> Plan:
    """The agent's DNS, given this host's resolv.conf and the public
    resolvers (of ``tried``) that ``answering`` says answered."""
    if answering:
        listed = ", ".join(answering)
        return Plan(
            resolv_conf=(
                "# Written by install.sh for the Portal agent, whose unit\n"
                "# bind-mounts it over /etc/resolv.conf: public resolvers that\n"
                "# answered from this host, so the agent never needs the LAN's.\n"
                "# Re-run install.sh to re-check.\n"
                + "".join(f"nameserver {a}\n" for a in answering)
                + "options timeout:2 attempts:2\n"
            ),
            drop_in=None,
            message=f"The Portal agent resolves names through public DNS ({listed}).",
            warn=False,
        )

    blocked = f"Public DNS ({', '.join(tried)}) doesn't answer from this host"
    resolvers = _host_resolvers(host_resolv_conf)
    lan = [str(a) for a in resolvers if _denied(a)]
    drop_in = (
        "# Written by install.sh: public DNS didn't answer from this host, so\n"
        "# the Portal agent uses the host's resolv.conf (the unit's bind mount\n"
        "# is cleared) and may reach the host's LAN resolver. Re-run install.sh\n"
        "# once public DNS answers.\n"
        "[Service]\n"
        "BindReadOnlyPaths=\n" + (f"IPAddressAllow={' '.join(lan)}\n" if lan else "")
    )
    if not resolvers:
        message = (
            f"{blocked}, and this host's resolv.conf names no IPv4 resolver: "
            "the Portal agent can't resolve names, so remote access won't work. "
            "Allow DNS (port 53) to a public resolver and re-run install.sh."
        )
    elif lan:
        message = (
            f"{blocked}, so the Portal agent uses this host's DNS resolver "
            f"{', '.join(lan)}, which is on the LAN. The agent can reach that "
            "address on any port (a router's admin page, say): systemd can't "
            "allow just port 53. Allow DNS (port 53) to a public resolver and "
            "re-run install.sh to close it."
        )
    else:
        message = (
            f"{blocked}, so the Portal agent uses this host's DNS resolver "
            f"({', '.join(str(a) for a in resolvers)}); no LAN address is opened "
            "for it. Re-run install.sh once public DNS answers."
        )
    return Plan(resolv_conf=None, drop_in=drop_in, message=message, warn=True)


def _parse_server(server: str) -> tuple[str, int]:
    """``ip`` or ``ip:port`` (the port is for tests) of an IPv4 resolver
    the agent may reach; ``ValueError`` otherwise."""
    host, _, port = server.partition(":")
    address = ipaddress.ip_address(host)
    if not isinstance(address, ipaddress.IPv4Address) or _denied(address):
        raise ValueError(f"{server} isn't an IPv4 resolver the agent may reach")
    return str(address), int(port) if port else 53


def _query() -> tuple[int, bytes]:
    qid = secrets.randbelow(0x10000)
    # A recursive query for the root's NS records: every resolver can answer.
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    return qid, header + b"\x00" + struct.pack("!HH", 2, 1)


def answers(server: str, timeout: float = TIMEOUT_S) -> bool:
    """Whether the resolver at ``server`` (``ip[:port]``) answers a query
    from this host within ``timeout`` seconds."""
    address = _parse_server(server)
    qid, query = _query()
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.send(query)
            while (left := deadline - time.monotonic()) > 0:
                sock.settimeout(left)
                reply = sock.recv(4096)
                if len(reply) < 12:
                    continue
                rid, flags = struct.unpack("!HH", reply[:4])
                if rid == qid and flags & 0x8000:
                    return flags & 0x000F == 0
    except OSError:
        return False
    return False


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o644)
    os.replace(tmp, path)


def _remove(path: Path, *, parent_if_empty: bool = False) -> None:
    path.unlink(missing_ok=True)
    if parent_if_empty:
        with contextlib.suppress(OSError):
            path.parent.rmdir()


def apply(p: Plan, agent_resolv_conf: Path, drop_in: Path) -> None:
    """Lay ``p`` down, removing whichever file it doesn't need."""
    if p.resolv_conf is None:
        _remove(agent_resolv_conf, parent_if_empty=True)
    else:
        _write(agent_resolv_conf, p.resolv_conf)
    if p.drop_in is None:
        _remove(drop_in, parent_if_empty=True)
    else:
        _write(drop_in, p.drop_in)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=__name__, description=__doc__)
    parser.add_argument("--host-resolv-conf", type=Path, default="/etc/resolv.conf")
    parser.add_argument("--agent-resolv-conf", type=Path, required=True)
    parser.add_argument("--drop-in", type=Path, required=True)
    parser.add_argument("--public", default=" ".join(PUBLIC_RESOLVERS))
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S)
    args = parser.parse_args(argv)

    tried = tuple(str(args.public).split())
    try:
        for server in tried:
            _parse_server(server)
    except ValueError as exc:
        print(f"Bad public resolver: {exc}", file=sys.stderr)
        return 2
    if not tried:
        print("No public resolvers given", file=sys.stderr)
        return 2

    timeout = float(args.timeout)
    with ThreadPoolExecutor(max_workers=len(tried)) as pool:
        ok = list(pool.map(lambda s: answers(s, timeout), tried))
    answering = [_parse_server(s)[0] for s, good in zip(tried, ok, strict=True) if good]
    try:
        host = Path(args.host_resolv_conf).read_text(encoding="utf-8", errors="replace")
    except OSError:
        host = ""

    p = plan(host, answering, tried)
    apply(p, Path(args.agent_resolv_conf), Path(args.drop_in))
    print(p.message)
    return FALLBACK if p.warn else 0


if __name__ == "__main__":
    sys.exit(main())
