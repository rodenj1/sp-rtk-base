"""Let the Portal agent reach a DNS resolver on the LAN, and nothing else there.

The agent's unit denies every private, link-local and unique-local address
(``IPAddressDeny``). A Pi whose ``/etc/resolv.conf`` names a resolver on the
LAN (a home router, say) would then resolve nothing, so ``install.sh`` runs
this module and writes what it prints into a drop-in for the unit: an
``IPAddressAllow`` for exactly those resolver addresses. Loopback resolvers
(systemd-resolved's ``127.0.0.53``) are already allowed.

Usage: ``python -m sp_rtk_base.portal_agent.resolver /etc/resolv.conf``;
prints the drop-in, or nothing when none is needed.
"""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

DENIED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
    )
)
"""The unit's ``IPAddressDeny`` ranges."""


def lan_resolvers(resolv_conf: str) -> list[str]:
    """The ``nameserver`` addresses in ``resolv_conf`` that the unit denies."""
    found: list[str] = []
    for line in resolv_conf.splitlines():
        words = line.split()
        if len(words) < 2 or words[0] != "nameserver":
            continue
        try:
            address = ipaddress.ip_address(words[1].split("%", 1)[0])
        except ValueError:
            continue
        if any(address.version == net.version and address in net for net in DENIED):
            text = str(address)
            if text not in found:
                found.append(text)
    return found


def drop_in(resolv_conf: str) -> str:
    """The unit drop-in allowing the LAN resolvers, or ``""`` if none."""
    resolvers = lan_resolvers(resolv_conf)
    if not resolvers:
        return ""
    return (
        "# Written by install.sh: this host's DNS resolver is on the LAN, which\n"
        "# the Portal agent is otherwise denied. Re-run install.sh if it changes.\n"
        "[Service]\n"
        f"IPAddressAllow={' '.join(resolvers)}\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else Path("/etc/resolv.conf")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    sys.stdout.write(drop_in(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
