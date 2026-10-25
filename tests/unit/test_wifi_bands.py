"""Tests for the Network page's Wi-Fi band presenter (spec #147)."""

from __future__ import annotations

import pytest

from sp_rtk_base.models.net_provision_models import (
    BandChangeReverted,
    SavedWifiConnection,
    WifiBand,
    WifiNetwork,
)
from sp_rtk_base.ui import wifi_bands
from sp_rtk_base.ui.wifi_bands import BandHeard, BandOption, BandSuggestion


def _network(band_signals: dict[WifiBand, int]) -> WifiNetwork:
    return WifiNetwork(
        ssid="FarmShop",
        signal=max(band_signals.values(), default=50),
        security="WPA2",
        band_signals=band_signals,
    )


BOTH = _network({WifiBand.GHZ_5: 41, WifiBand.GHZ_2_4: 82})
ONLY_5 = _network({WifiBand.GHZ_5: 55})
NEITHER = _network({})


class TestBandSummary:
    def test_a_network_heard_on_both_bands_lists_both_lowest_first(self) -> None:
        assert wifi_bands.band_summary(BOTH, has_5ghz=True) == "2.4 GHz + 5 GHz"

    def test_a_network_heard_on_one_band_names_it(self) -> None:
        assert wifi_bands.band_summary(ONLY_5, has_5ghz=True) == "5 GHz"

    def test_no_summary_when_heard_on_neither_band(self) -> None:
        """Only on a frequency outside both bands, such as 6 GHz."""
        assert wifi_bands.band_summary(NEITHER, has_5ghz=True) is None

    @pytest.mark.parametrize("has_5ghz", [False, None])
    def test_no_summary_unless_the_adapter_has_5ghz(
        self, has_5ghz: bool | None
    ) -> None:
        assert wifi_bands.band_summary(BOTH, has_5ghz=has_5ghz) is None


class TestBandsHeard:
    def test_one_reading_per_band_heard_lowest_band_first(self) -> None:
        assert wifi_bands.bands_heard(BOTH, has_5ghz=True) == [
            BandHeard(band=WifiBand.GHZ_2_4, signal=82, text="2.4 GHz: 82%"),
            BandHeard(band=WifiBand.GHZ_5, signal=41, text="5 GHz: 41%"),
        ]

    def test_only_the_band_heard(self) -> None:
        assert wifi_bands.bands_heard(ONLY_5, has_5ghz=True) == [
            BandHeard(band=WifiBand.GHZ_5, signal=55, text="5 GHz: 55%"),
        ]

    @pytest.mark.parametrize("has_5ghz", [False, None])
    def test_nothing_unless_the_adapter_has_5ghz(self, has_5ghz: bool | None) -> None:
        assert wifi_bands.bands_heard(BOTH, has_5ghz=has_5ghz) == []


def _saved(band: WifiBand, *, active: bool = False) -> SavedWifiConnection:
    return SavedWifiConnection(name="Barn-AP", active=active, band=band)


class TestShowsBandControl:
    def test_shown_when_the_adapter_has_5ghz(self) -> None:
        assert wifi_bands.shows_band_control(has_5ghz=True) is True

    @pytest.mark.parametrize("has_5ghz", [False, None])
    def test_hidden_unless_the_adapter_has_5ghz(self, has_5ghz: bool | None) -> None:
        assert wifi_bands.shows_band_control(has_5ghz=has_5ghz) is False


class TestBandBadge:
    @pytest.mark.parametrize(
        ("band", "badge"),
        [(WifiBand.GHZ_2_4, "2.4 GHz only"), (WifiBand.GHZ_5, "5 GHz only")],
    )
    def test_a_locked_network_shows_its_band(self, band: WifiBand, badge: str) -> None:
        assert wifi_bands.band_badge(_saved(band), has_5ghz=True) == badge

    def test_an_auto_network_shows_no_badge(self) -> None:
        assert wifi_bands.band_badge(_saved(WifiBand.AUTO), has_5ghz=True) is None

    @pytest.mark.parametrize("has_5ghz", [False, None])
    def test_no_badge_unless_the_adapter_has_5ghz(self, has_5ghz: bool | None) -> None:
        saved = _saved(WifiBand.GHZ_2_4)
        assert wifi_bands.band_badge(saved, has_5ghz=has_5ghz) is None


class TestBandOptions:
    def test_each_band_hints_what_the_scan_hears_now(self) -> None:
        scan = [_named("Barn-AP", {WifiBand.GHZ_2_4: 82, WifiBand.GHZ_5: 41})]
        assert wifi_bands.band_options("Barn-AP", scan) == [
            BandOption(WifiBand.AUTO, "Auto", "Either band; the base picks."),
            BandOption(WifiBand.GHZ_2_4, "2.4 GHz", "Heard now at 82%."),
            BandOption(WifiBand.GHZ_5, "5 GHz", "Heard now at 41%."),
        ]

    def test_a_band_the_scan_doesnt_hear_says_so(self) -> None:
        scan = [_named("Barn-AP", {WifiBand.GHZ_5: 55})]
        options = wifi_bands.band_options("Barn-AP", scan)
        assert [o.hint for o in options] == [
            "Either band; the base picks.",
            "Not heard right now.",
            "Heard now at 55%.",
        ]

    def test_a_network_missing_from_the_scan_is_heard_on_neither_band(
        self,
    ) -> None:
        """Only the saved network's own SSID counts, not its neighbours'."""
        scan = [_named("Neighbour", {WifiBand.GHZ_2_4: 90, WifiBand.GHZ_5: 90})]
        options = wifi_bands.band_options("Barn-AP", scan)
        assert [o.hint for o in options] == [
            "Either band; the base picks.",
            "Not heard right now.",
            "Not heard right now.",
        ]

    def test_the_active_network_disables_a_band_the_scan_doesnt_hear(
        self,
    ) -> None:
        """Locking the network the base is on to a band it isn't heard on
        would strand the base; Auto is always open."""
        scan = [_named("FarmShop", {WifiBand.GHZ_2_4: 82})]
        options = wifi_bands.band_options("FarmShop", scan, active=True)
        assert [(o.band, o.disabled) for o in options] == [
            (WifiBand.AUTO, False),
            (WifiBand.GHZ_2_4, False),
            (WifiBand.GHZ_5, True),
        ]

    def test_a_network_that_isnt_active_disables_nothing(self) -> None:
        """It may be out of range on purpose: the operator is preparing it."""
        options = wifi_bands.band_options("Barn-AP", [], active=False)
        assert [o.disabled for o in options] == [False, False, False]


class TestBandChangeWarning:
    def test_on_the_local_console_the_session_drops_for_half_a_minute(
        self,
    ) -> None:
        text = wifi_bands.band_change_warning(
            "FarmShop",
            WifiBand.GHZ_2_4,
            on_portal=False,
            ap_ssid="sp-rtk-base-setup",
            fallback_minutes="5",
        )
        assert text == (
            "'FarmShop' reconnects to apply this, so this console session will "
            "drop for about half a minute. If it can't join on 2.4 GHz, it puts "
            "the old band back by itself; failing that, 'sp-rtk-base-setup' "
            "appears in about 5 min."
        )

    def test_on_the_portal_the_base_drops_off_for_a_minute(self) -> None:
        text = wifi_bands.band_change_warning(
            "FarmShop",
            WifiBand.GHZ_5,
            on_portal=True,
            ap_ssid="sp-rtk-base-setup",
            fallback_minutes="5",
        )
        assert text == (
            "'FarmShop' reconnects to apply this, so the base will drop off the "
            "Portal for about a minute and this page will stop updating. If it "
            "can't join on 5 GHz, it puts the old band back by itself and "
            "reappears."
        )

    def test_auto_reads_as_either_band(self) -> None:
        text = wifi_bands.band_change_warning(
            "FarmShop",
            WifiBand.AUTO,
            on_portal=True,
            ap_ssid="sp-rtk-base-setup",
            fallback_minutes="5",
        )
        assert "If it can't join on either band," in text


class TestRevertedNote:
    def test_a_reverted_change_explains_what_happened(self) -> None:
        saved = SavedWifiConnection(
            name="FarmShop",
            active=True,
            last_band_change=BandChangeReverted(
                name="FarmShop", band=WifiBand.AUTO, tried=WifiBand.GHZ_5
            ),
        )
        assert wifi_bands.reverted_note(saved) == (
            "Your change to 5 GHz was undone: the base couldn't join 'FarmShop' "
            "on 5 GHz, so it went back to Auto."
        )

    def test_no_note_without_a_reverted_change(self) -> None:
        saved = SavedWifiConnection(name="FarmShop", active=True)
        assert wifi_bands.reverted_note(saved) is None


def _named(ssid: str, band_signals: dict[WifiBand, int]) -> WifiNetwork:
    return _network(band_signals).model_copy(update={"ssid": ssid})


class TestJoinBandChoices:
    """issue #153: the join form's Wi-Fi band select."""

    def test_each_band_carries_what_the_scan_hears(self) -> None:
        assert wifi_bands.join_band_choices(BOTH, has_5ghz=True) == {
            WifiBand.AUTO: "Auto",
            WifiBand.GHZ_2_4: "2.4 GHz (82%)",
            WifiBand.GHZ_5: "5 GHz (41%)",
        }

    def test_a_band_the_scan_doesnt_hear_says_so(self) -> None:
        assert wifi_bands.join_band_choices(ONLY_5, has_5ghz=True) == {
            WifiBand.AUTO: "Auto",
            WifiBand.GHZ_2_4: "2.4 GHz (not heard)",
            WifiBand.GHZ_5: "5 GHz (55%)",
        }

    def test_a_hidden_network_has_no_scan_data_so_no_hints(self) -> None:
        assert wifi_bands.join_band_choices(None, has_5ghz=True) == {
            WifiBand.AUTO: "Auto",
            WifiBand.GHZ_2_4: "2.4 GHz",
            WifiBand.GHZ_5: "5 GHz",
        }

    @pytest.mark.parametrize("has_5ghz", [False, None])
    def test_no_select_without_a_5ghz_adapter(self, has_5ghz: bool | None) -> None:
        assert wifi_bands.join_band_choices(BOTH, has_5ghz=has_5ghz) == {}
        assert wifi_bands.join_band_choices(None, has_5ghz=has_5ghz) == {}

    def test_auto_comes_first_so_the_select_defaults_to_it(self) -> None:
        choices = wifi_bands.join_band_choices(BOTH, has_5ghz=True)
        assert next(iter(choices)) is WifiBand.AUTO

    @pytest.mark.parametrize("security", ["WEP", "WPA2 802.1X", "OWE"])
    def test_no_select_for_security_a_band_join_cant_build(self, security: str) -> None:
        """Such a network joins on Auto, where NetworkManager works the
        security out itself."""
        network = BOTH.model_copy(update={"security": security})
        assert wifi_bands.join_band_choices(network, has_5ghz=True) == {}

    @pytest.mark.parametrize("security", ["", "WPA2 WPA3", "WPA3"])
    def test_open_and_wpa_personal_networks_get_the_select(self, security: str) -> None:
        network = BOTH.model_copy(update={"security": security})
        assert wifi_bands.join_band_choices(network, has_5ghz=True) != {}


def _active(band: WifiBand = WifiBand.AUTO) -> SavedWifiConnection:
    return SavedWifiConnection(name="FarmShop", active=True, band=band)


_ON_5GHZ_MHZ = 5180
_ON_2_4GHZ_MHZ = 2437


class TestStrongerBand:
    """The status card's suggestion to switch the active network's band
    (issue #154)."""

    def test_suggests_the_band_heard_noticeably_more_strongly(self) -> None:
        suggestion = wifi_bands.stronger_band(
            _active(), _ON_5GHZ_MHZ, [BOTH], has_5ghz=True
        )
        assert suggestion == BandSuggestion(
            band=WifiBand.GHZ_2_4,
            text="'FarmShop' is heard more strongly on 2.4 GHz (82% vs 41%).",
            button="Use 2.4 GHz",
        )

    def test_no_suggestion_at_exactly_the_margin(self) -> None:
        """20 points is the margin: 61% vs 41% isn't enough."""
        heard = _network({WifiBand.GHZ_2_4: 61, WifiBand.GHZ_5: 41})
        assert (
            wifi_bands.stronger_band(_active(), _ON_5GHZ_MHZ, [heard], has_5ghz=True)
            is None
        )

    def test_suggested_just_above_the_margin(self) -> None:
        heard = _network({WifiBand.GHZ_2_4: 62, WifiBand.GHZ_5: 41})
        suggestion = wifi_bands.stronger_band(
            _active(), _ON_5GHZ_MHZ, [heard], has_5ghz=True
        )
        assert suggestion is not None
        assert suggestion.text == (
            "'FarmShop' is heard more strongly on 2.4 GHz (62% vs 41%)."
        )

    def test_suggests_5ghz_when_that_is_the_stronger_band(self) -> None:
        heard = _network({WifiBand.GHZ_2_4: 30, WifiBand.GHZ_5: 75})
        suggestion = wifi_bands.stronger_band(
            _active(), _ON_2_4GHZ_MHZ, [heard], has_5ghz=True
        )
        assert suggestion is not None
        assert (suggestion.band, suggestion.button) == (WifiBand.GHZ_5, "Use 5 GHz")

    def test_no_suggestion_when_the_band_its_on_is_the_stronger(self) -> None:
        assert (
            wifi_bands.stronger_band(_active(), _ON_2_4GHZ_MHZ, [BOTH], has_5ghz=True)
            is None
        )

    def test_a_locked_network_is_suggested_the_other_band_too(self) -> None:
        suggestion = wifi_bands.stronger_band(
            _active(WifiBand.GHZ_5), _ON_5GHZ_MHZ, [BOTH], has_5ghz=True
        )
        assert suggestion is not None
        assert suggestion.band is WifiBand.GHZ_2_4

    def test_without_a_frequency_a_locked_network_is_on_its_lock(self) -> None:
        suggestion = wifi_bands.stronger_band(
            _active(WifiBand.GHZ_5), None, [BOTH], has_5ghz=True
        )
        assert suggestion is not None
        assert suggestion.band is WifiBand.GHZ_2_4

    def test_without_a_frequency_an_auto_network_gets_no_suggestion(self) -> None:
        assert wifi_bands.stronger_band(_active(), None, [BOTH], has_5ghz=True) is None

    def test_no_suggestion_when_heard_on_only_one_band(self) -> None:
        assert (
            wifi_bands.stronger_band(_active(), _ON_2_4GHZ_MHZ, [ONLY_5], has_5ghz=True)
            is None
        )

    def test_no_suggestion_when_the_scan_doesnt_hear_the_network(self) -> None:
        other = _named("Neighbour-5G", {WifiBand.GHZ_2_4: 90, WifiBand.GHZ_5: 10})
        assert (
            wifi_bands.stronger_band(_active(), _ON_5GHZ_MHZ, [other], has_5ghz=True)
            is None
        )

    @pytest.mark.parametrize("has_5ghz", [False, None])
    def test_no_suggestion_unless_the_adapter_has_5ghz(
        self, has_5ghz: bool | None
    ) -> None:
        assert (
            wifi_bands.stronger_band(_active(), _ON_5GHZ_MHZ, [BOTH], has_5ghz=has_5ghz)
            is None
        )
