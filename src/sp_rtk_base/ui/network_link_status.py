"""What the dashboard's Network card says for a Network link snapshot.

The wording and colours live here, tested, so the card itself
(``ui/components/network_card.py``, outside the coverage gate) only lays
them out.  The snapshot has already decided every value: a missing one
is shown as "—", and a Not supported one (``None`` in the snapshot) isn't
shown at all.  Never "signal": that word belongs to Signal Quality.
"""

from __future__ import annotations

from dataclasses import dataclass

from sp_rtk_base.models.network_link_models import (
    LinkKind,
    LinkState,
    NetworkLink,
    NetworkLinkSnapshot,
)
from sp_rtk_base.services.network_link.monitor import WAITING_REASON
from sp_rtk_base.ui.formatting import format_byte_rate

NONE = "—"

REACHABILITY_NOTE = (
    "Link health only: whether the caster and Portal are reachable "
    "shows under Destinations."
)

KIND_LABEL: dict[LinkKind, str] = {"ethernet": "Ethernet", "wifi": "Wi-Fi"}
KIND_ICON: dict[LinkKind, str] = {"ethernet": "settings_ethernet", "wifi": "wifi"}
_STATE_COLOR: dict[LinkState, str] = {
    "Connected": "positive",
    "Disconnected": "grey-5",
    "No reading": "warning",
}


@dataclass(frozen=True)
class Shown:
    """A value as the card shows it: its text, Quasar colour and the
    smaller line beneath it, if any."""

    text: str
    color: str = "white"
    sub: str | None = None


def uplink(snapshot: NetworkLinkSnapshot) -> Shown:
    """The header's Uplink: which link outbound traffic leaves by."""
    if snapshot.uplink_no_reading:
        return Shown("No reading", "warning")
    if snapshot.uplink is None:
        return Shown("No Uplink", "negative")
    name = snapshot.uplink.interface
    if not snapshot.uplink.network_link:
        return Shown(f"{name} (not a Network link)")
    label = next(
        (KIND_LABEL[link.kind] for link in snapshot.links if link.name == name),
        "Network link",
    )
    return Shown(f"{label} ({name})")


def freshness(snapshot: NetworkLinkSnapshot) -> Shown:
    """How old the readings are, a warning once they're too old to trust."""
    age = snapshot.last_sample_age_seconds
    if age is None:
        return Shown(WAITING_REASON, "warning")
    if snapshot.stale:
        return Shown(f"No reading for {age:.0f} s", "warning")
    return Shown(f"Updated {age:.0f} s ago", "grey-6")


def heading(link: NetworkLink) -> str:
    """``Wi-Fi · wlan0``."""
    return f"{KIND_LABEL[link.kind]} · {link.name}"


def state(link: NetworkLink) -> Shown:
    """Connected / Disconnected / No reading, with the reason beneath."""
    return Shown(link.state, _STATE_COLOR[link.state], link.reason)


def speed(link: NetworkLink) -> Shown | None:
    """Ethernet speed with duplex beneath; ``None`` when Not supported."""
    if link.ethernet is None or link.ethernet.speed_mbps is None:
        return None
    mbps = link.ethernet.speed_mbps.value
    duplex = link.ethernet.full_duplex
    sub = None
    if duplex is not None:
        sub = (
            NONE
            if duplex.value is None
            else ("Full duplex" if duplex.value else "Half duplex")
        )
    return Shown(NONE if mbps is None else f"{mbps} Mbit/s", sub=sub)


def address(link: NetworkLink) -> Shown:
    """IPv4 address, with IPv6 beneath."""
    return Shown(link.ipv4 or NONE, sub=link.ipv6)


def traffic(link: NetworkLink) -> Shown:
    """↓ received and ↑ sent per second."""
    if link.throughput is None:
        return Shown(NONE)
    down = format_byte_rate(link.throughput.rx_bytes_per_second)
    up = format_byte_rate(link.throughput.tx_bytes_per_second)
    return Shown(f"↓{down} ↑{up}")


def counters(link: NetworkLink) -> Shown:
    """Errors / drops totals, with link changes beneath."""
    c = link.counters
    changes = link.link_changes
    return Shown(
        f"{c.rx_errors + c.tx_errors:,} / {c.rx_drops + c.tx_drops:,}",
        sub=f"{changes:,} link change{'' if changes == 1 else 's'}",
    )
