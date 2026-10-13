"""The agent config block from the Portal, checked before the agent uses it.

Every poll and Check-in reply carries a revisioned block
(:class:`~sp_rtk_portal_protocol.AgentConfig`). The agent applies one only
when its revision differs from the one it holds, persists it
(:func:`~sp_rtk_base.portal_agent.state.save_agent_config`) and, until it has
one, uses the built-in defaults.

The Portal is trusted with timings only. Its two levers on the agent's
network behaviour are pinned here (spec #82, "The LAN behind the base"):

- the tunnel URL must be ``wss://`` on the Portal's own host (the built-in
  one, or the local override for development); anything else is ignored,
  logged, and the built-in tunnel URL is used;
- the Doorbell host is only kept as a name here: it is resolved and checked
  before each use (:mod:`.doorbell`).

A timing outside its sane range is ignored and logged too, so a bad block
can't make the agent hammer the Portal or go silent.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from sp_rtk_portal_protocol import TUNNEL_PATH, AgentConfig

logger = logging.getLogger(__name__)

DEFAULTS = AgentConfig(revision=0)
"""The built-in values (the protocol package's defaults)."""

RANGES: dict[str, tuple[int, int]] = {
    "poll_interval": (10, 3600),
    "metered_poll_interval": (10, 3600),
    "check_in_interval": (60, 86400),
    "tunnel_idle_timeout": (10, 86400),
    "tunnel_hard_cap": (60, 86400),
}
"""Seconds; a value outside its range is ignored."""


@dataclass(frozen=True)
class EffectiveConfig:
    """What the agent actually runs with."""

    revision: int | None
    """The block's revision; ``None`` while on the built-in defaults."""
    poll_interval: int
    metered_poll_interval: int
    check_in_interval: int
    doorbell: tuple[str, int] | None
    """Host and port, unresolved."""
    tunnel_url: str
    tunnel_idle_timeout: int
    tunnel_hard_cap: int

    def interval(self, metered: bool) -> int:
        return self.metered_poll_interval if metered else self.poll_interval


def default_tunnel_url(portal_url: str) -> str:
    """``wss://<Portal host>/agent/tunnel`` (``ws://`` only for a plain-HTTP
    development override)."""
    parts = urlsplit(portal_url)
    scheme = "ws" if parts.scheme == "http" else "wss"
    return f"{scheme}://{parts.netloc}{TUNNEL_PATH}"


def _tunnel_url_allowed(url: str, portal_url: str) -> bool:
    portal = urlsplit(portal_url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    scheme = "ws" if portal.scheme == "http" else "wss"
    return (
        parts.scheme == scheme
        and parts.username is None
        and parts.password is None
        and parts.hostname is not None
        and parts.hostname == portal.hostname
        and port == portal.port
    )


def effective(block: AgentConfig | None, portal_url: str) -> EffectiveConfig:
    """The config to run with: ``block`` checked field by field, the built-in
    defaults for anything missing or refused (``None``: all defaults)."""
    source = block if block is not None else DEFAULTS
    timings: dict[str, int] = {}
    for name, (low, high) in RANGES.items():
        value: int = getattr(source, name)
        if not low <= value <= high:
            logger.warning(
                "Ignoring agent config %s=%s (allowed %s..%s); using %s",
                name,
                value,
                low,
                high,
                getattr(DEFAULTS, name),
            )
            value = getattr(DEFAULTS, name)
        timings[name] = value

    tunnel_url = default_tunnel_url(portal_url)
    if source.tunnel_url and source.tunnel_url != tunnel_url:
        if _tunnel_url_allowed(source.tunnel_url, portal_url):
            tunnel_url = source.tunnel_url
        else:
            logger.warning(
                "Ignoring the tunnel URL %r from the Portal: it isn't wss:// on "
                "%s; using %s",
                source.tunnel_url,
                urlsplit(portal_url).netloc,
                tunnel_url,
            )

    doorbell = None
    if source.doorbell_host:
        port = source.doorbell_port
        if port is not None and 0 < port < 65536:
            doorbell = (source.doorbell_host, port)
        else:
            logger.warning(
                "Ignoring the Doorbell %r: bad port %r", source.doorbell_host, port
            )

    return EffectiveConfig(
        revision=block.revision if block is not None else None,
        doorbell=doorbell,
        tunnel_url=tunnel_url,
        **timings,
    )


def as_json(block: AgentConfig) -> dict[str, object]:
    return dataclasses.asdict(block)
