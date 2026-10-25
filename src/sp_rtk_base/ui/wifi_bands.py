"""What the Network page says about Wi-Fi bands (spec #147).

The decisions live here, tested, so the page itself (``ui/pages/network.py``,
outside the coverage gate) only lays them out.  Everything about Wi-Fi
bands is hidden unless the adapter is known to have 5 GHz: on a
2.4 GHz-only base there's no choice to show.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sp_rtk_base.models.net_provision_models import (
    SavedWifiConnection,
    WifiBand,
    WifiNetwork,
    band_join_security,
    band_signals_of,
)

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
    if not shows_band_control(has_5ghz=has_5ghz):
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


def shows_band_control(*, has_5ghz: bool | None) -> bool:
    """Whether saved-network entries offer the Band button at all."""
    return has_5ghz is True


def band_badge(saved: SavedWifiConnection, *, has_5ghz: bool | None) -> str | None:
    """A saved entry's lock badge, such as "2.4 GHz only"; ``None`` for an
    Auto network, so an unlocked list stays uncluttered."""
    if not shows_band_control(has_5ghz=has_5ghz) or saved.band is WifiBand.AUTO:
        return None
    return f"{saved.band.label} only"


@dataclass(frozen=True)
class BandOption:
    """One choice in the Band dialog, with what the page's scan hears now."""

    band: WifiBand
    label: str
    hint: str
    disabled: bool = False


def band_options(
    name: str, scan: Sequence[WifiNetwork], *, active: bool = False
) -> list[BandOption]:
    """The Band dialog's choices for saved network ``name``: Auto, then
    each band with whether the page's last scan hears ``name`` there.

    For the ``active`` network, a band the scan doesn't hear is disabled,
    so the operator can't lock the base out of the network it is on.
    """
    heard = band_signals_of(name, scan) or {}
    options = [BandOption(WifiBand.AUTO, WifiBand.AUTO.label, _AUTO_HINT)]
    for band in _HEARD_ORDER:
        signal = heard.get(band)
        hint = _NOT_HEARD_HINT if signal is None else f"Heard now at {signal}%."
        options.append(
            BandOption(band, band.label, hint, disabled=active and signal is None)
        )
    return options


def join_band_choices(
    network: WifiNetwork | None, *, has_5ghz: bool | None
) -> dict[WifiBand, str]:
    """The join form's Wi-Fi band select, Auto first (its default), each
    band labelled with what the page's scan hears, such as "5 GHz (41%)"
    or "5 GHz (not heard)" (issue #153).

    ``network`` is the scanned network being joined, or ``None`` for a
    hidden one, which has no scan data and so no hints. Empty without a
    5 GHz adapter, or for a network whose security a band join can't
    build (see :func:`band_join_security`): the form then shows no
    select and joins on Auto.
    """
    if not shows_band_control(has_5ghz=has_5ghz):
        return {}
    if network is not None and band_join_security(network.security) is None:
        return {}
    choices = {WifiBand.AUTO: WifiBand.AUTO.label}
    for band in _HEARD_ORDER:
        if network is None:
            choices[band] = band.label
        elif (signal := network.band_signals.get(band)) is None:
            choices[band] = f"{band.label} (not heard)"
        else:
            choices[band] = f"{band.label} ({signal}%)"
    return choices


def band_change_warning(
    name: str,
    band: WifiBand,
    *,
    on_portal: bool,
    ap_ssid: str,
    fallback_minutes: str,
) -> str:
    """The confirm dialog's warning before a band change reconnects the
    active network ``name``.

    The operator's own session drops with the link. On the Portal the base
    drops off for longer, since it has to rejoin and reach the Portal
    again. ``ap_ssid`` and ``fallback_minutes`` are the setup hotspot's,
    read fresh by the caller.
    """
    target = "either band" if band is WifiBand.AUTO else band.label
    if on_portal:
        return (
            f"'{name}' reconnects to apply this, so the base will drop off the "
            "Portal for about a minute and this page will stop updating. If it "
            f"can't join on {target}, it puts the old band back by itself and "
            "reappears."
        )
    return (
        f"'{name}' reconnects to apply this, so this console session will drop "
        f"for about half a minute. If it can't join on {target}, it puts the old "
        f"band back by itself; failing that, '{ap_ssid}' appears in about "
        f"{fallback_minutes} min."
    )


def reverted_note(saved: SavedWifiConnection) -> str | None:
    """Why the last band change to ``saved`` was undone; ``None`` unless it
    was Reverted."""
    reverted = saved.last_band_change
    if reverted is None:
        return None
    return (
        f"Your change to {reverted.tried.label} was undone: the base couldn't "
        f"join '{saved.name}' on {reverted.tried.label}, so it went back to "
        f"{reverted.band.label}."
    )


# How much more strongly, in points of scan signal, the active network must
# be heard on the other band before the status card suggests switching.
# NetworkManager maps -100..-40 dBm linearly onto 0..100%, so 20 points is
# about 12 dB: wider than one Wi-Fi link strength level (Marginal spans
# -68..-77 dBm) and well clear of the few dB one scan differs from the
# next.  A smaller gap rarely changes the level, and isn't worth giving up
# 5 GHz's faster link or a console session for.
STRONGER_BAND_MARGIN = 20


@dataclass(frozen=True)
class BandSuggestion:
    """The status card's suggestion to switch the active network's band."""

    band: WifiBand
    text: str
    button: str


def stronger_band(
    active: SavedWifiConnection,
    frequency_mhz: int | None,
    scan: Sequence[WifiNetwork],
    *,
    has_5ghz: bool | None,
) -> BandSuggestion | None:
    """A suggestion to switch the ``active`` network to the band the page's
    last ``scan`` hears it on more strongly, by more than
    ``STRONGER_BAND_MARGIN``, than the band it's on; ``None`` otherwise.

    The band it's on comes from the link's ``frequency_mhz``.  Without a
    frequency reading, a locked network is on its lock; an Auto one is on
    an unknown band and gets no suggestion.
    """
    if not shows_band_control(has_5ghz=has_5ghz):
        return None
    on_band = None if frequency_mhz is None else WifiBand.from_frequency(frequency_mhz)
    if on_band is None and active.band is not WifiBand.AUTO:
        on_band = active.band
    if on_band is None:
        return None
    heard = band_signals_of(active.name, scan) or {}
    other = WifiBand.GHZ_5 if on_band is WifiBand.GHZ_2_4 else WifiBand.GHZ_2_4
    on_signal, other_signal = heard.get(on_band), heard.get(other)
    if on_signal is None or other_signal is None:
        return None
    if other_signal <= on_signal + STRONGER_BAND_MARGIN:
        return None
    return BandSuggestion(
        band=other,
        text=(
            f"'{active.name}' is heard more strongly on {other.label} "
            f"({other_signal}% vs {on_signal}%)."
        ),
        button=f"Use {other.label}",
    )


_AUTO_HINT = "Either band; the base picks."
_NOT_HEARD_HINT = "Not heard right now."
