"""What the Network page says about Wi-Fi bands (spec #147).

The decisions live here, tested, so the page itself (``ui/pages/network.py``,
outside the coverage gate) only lays them out.  Everything about Wi-Fi
bands is hidden unless the adapter is known to have 5 GHz: on a
2.4 GHz-only base there's no choice to show.
"""

from __future__ import annotations

from dataclasses import dataclass

from sp_rtk_base.models.net_provision_models import WifiBand, WifiNetwork

# The bands a network can be heard on, in the order the console lists them.
_HEARD_ORDER = (WifiBand.GHZ_2_4, WifiBand.GHZ_5)


@dataclass(frozen=True)
class BandHeard:
    """One band a nearby network is heard on, and how strongly."""

    band: WifiBand
    signal: int
    text: str


def bands_heard(network: WifiNetwork, *, has_5ghz: bool | None) -> list[BandHeard]:
    """The bands ``network`` is heard on, 2.4 GHz first, each with the
    strongest signal on it; empty unless the adapter has 5 GHz."""
    if has_5ghz is not True:
        return []
    return [
        BandHeard(band=band, signal=signal, text=f"{band.label}: {signal}%")
        for band in _HEARD_ORDER
        if (signal := network.band_signals.get(band)) is not None
    ]


def band_summary(network: WifiNetwork, *, has_5ghz: bool | None) -> str | None:
    """A scan row's band summary, such as "2.4 GHz + 5 GHz"; ``None`` when
    there's nothing to show."""
    heard = bands_heard(network, has_5ghz=has_5ghz)
    return " + ".join(h.band.label for h in heard) or None
