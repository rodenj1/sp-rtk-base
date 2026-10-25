"""Tests for the Network page's Wi-Fi band presenter (spec #147)."""

from __future__ import annotations

import pytest

from sp_rtk_base.models.net_provision_models import WifiBand, WifiNetwork
from sp_rtk_base.ui import wifi_bands
from sp_rtk_base.ui.wifi_bands import BandHeard


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
