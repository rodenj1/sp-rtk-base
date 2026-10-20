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
from sp_rtk_base.services.network_link.monitor import (
    WAITING_REASON,
    WIFI_NO_READING_REASON,
    no_reading_reason,
)
from sp_rtk_base.ui.components.level import LEVEL_COLOR
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
    """A value as the card shows it: its text, Quasar colour, the
    smaller line beneath it and its tooltip, if any."""

    text: str
    color: str = "white"
    sub: str | None = None
    tooltip: str | None = None


def uplink(snapshot: NetworkLinkSnapshot) -> Shown:
    """The header's Uplink: which link outbound traffic leaves by."""
    if snapshot.uplink_no_reading:
        return Shown("No reading", "warning")
    if snapshot.uplink is None:
        return Shown("No Uplink", "negative")
    name = snapshot.uplink.interface
    if not snapshot.uplink.network_link:
        return Shown(f"{name} (not a Network link)")
    link = snapshot.uplink_link()
    label = "Network link" if link is None else KIND_LABEL[link.kind]
    return Shown(f"{label} ({name})")


def freshness(snapshot: NetworkLinkSnapshot) -> Shown:
    """How old the readings are, a warning once they're too old to trust."""
    age = snapshot.last_sample_age_seconds
    if age is None:
        return Shown(WAITING_REASON, "warning")
    if snapshot.no_reading:
        return Shown(no_reading_reason(age), "warning")
    return Shown(f"Updated {age:.0f} s ago", "grey-6")


def heading(link: NetworkLink) -> str:
    """``Wi-Fi · wlan0``."""
    return f"{KIND_LABEL[link.kind]} · {link.name}"


def state(link: NetworkLink) -> Shown:
    """Connected / Disconnected / No reading, with the reason beneath (or
    why the Wi-Fi details have no value)."""
    reason = link.reason
    if reason is None and link.wifi is not None:
        reason = link.wifi.no_reading_reason
    return Shown(link.state, _STATE_COLOR[link.state], reason)


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


def wifi_network(link: NetworkLink) -> Shown | None:
    """The Wi-Fi network's SSID, with the BSSID beneath; ``None`` when Not
    supported."""
    if link.wifi is None or (link.wifi.ssid is None and link.wifi.bssid is None):
        return None
    ssid, bssid = link.wifi.ssid, link.wifi.bssid
    return Shown(
        NONE if ssid is None or ssid.value is None else ssid.value,
        sub=None if bssid is None else bssid.value or NONE,
    )


def wifi_strength(link: NetworkLink) -> Shown | None:
    """Wi-Fi link strength in its level colour, with the latest dBm
    beneath; "—" with the reason as a tooltip while it has no value, and
    ``None`` when Not supported."""
    if link.wifi is None or link.wifi.link_strength is None:
        return None
    level = link.wifi.link_strength.value
    if level is None:
        reason = link.reason or link.wifi.no_reading_reason or WIFI_NO_READING_REASON
        return Shown(NONE, tooltip=reason)
    dbm = None if link.wifi.signal_dbm is None else link.wifi.signal_dbm.value
    sub = None if dbm is None else f"{dbm} dBm"
    return Shown(level, LEVEL_COLOR[level], sub)


def _band(mhz: int) -> str:
    if mhz < 3000:
        return "2.4 GHz"
    if mhz < 5925:
        return "5 GHz"
    return "6 GHz"


def wifi_band(link: NetworkLink) -> Shown | None:
    """The Wi-Fi band, with the frequency beneath; ``None`` when Not
    supported."""
    if link.wifi is None or link.wifi.frequency_mhz is None:
        return None
    mhz = link.wifi.frequency_mhz.value
    if mhz is None:
        return Shown(NONE, sub=NONE)
    return Shown(_band(mhz), sub=f"{mhz} MHz")


def _mbits(bps: int) -> str:
    return f"{bps / 1_000_000:.1f}".removesuffix(".0")


def wifi_bitrate(link: NetworkLink) -> Shown | None:
    """↑ sent and ↓ received bitrate in Mbit/s; ``None`` when Not supported."""
    if link.wifi is None:
        return None
    rates = (("↑", link.wifi.tx_bitrate_bps), ("↓", link.wifi.rx_bitrate_bps))
    if all(rate is None for _, rate in rates):
        return None
    shown = [
        f"{arrow}{_mbits(rate.value)}"
        for arrow, rate in rates
        if rate is not None and rate.value is not None
    ]
    return Shown(f"{' '.join(shown)} Mbit/s" if shown else NONE)


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


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}{'' if number == 1 else 's'}"


def counters(link: NetworkLink) -> Shown:
    """Errors / drops totals, with link changes (and, for Wi-Fi, failed
    sends) beneath."""
    c = link.counters
    sub = _count(link.link_changes, "link change")
    failed = None if link.wifi is None else link.wifi.tx_failed
    if failed is not None and failed.value is not None:
        sub += f" · {_count(failed.value, 'failed send')}"
    return Shown(
        f"{c.rx_errors + c.tx_errors:,} / {c.rx_drops + c.tx_drops:,}", sub=sub
    )
