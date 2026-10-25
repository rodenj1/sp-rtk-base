"""What the dashboard's Network card says for a Network link snapshot."""

from __future__ import annotations

import pytest

from sp_rtk_base.models.level_models import Level
from sp_rtk_base.models.network_link_models import (
    Counters,
    EthernetDetails,
    NetworkLink,
    NetworkLinkSnapshot,
    Supported,
    Throughput,
    WifiDetails,
)
from sp_rtk_base.ui.network_link_status import (
    Shown,
    address,
    counters,
    freshness,
    heading,
    speed,
    state,
    traffic,
    uplink,
    wifi_band,
    wifi_bitrate,
    wifi_network,
    wifi_strength,
)
from sp_rtk_base.uplink import Uplink

ETH0 = NetworkLink(name="eth0", kind="ethernet", state="Connected")
WLAN0 = NetworkLink(name="wlan0", kind="wifi", state="Connected")


def _snapshot(**fields: object) -> NetworkLinkSnapshot:
    defaults: dict[str, object] = {
        "links": (ETH0, WLAN0),
        "uplink_no_reading": False,
        "last_sample_age_seconds": 1.0,
        "no_reading": False,
    }
    return NetworkLinkSnapshot.model_validate(defaults | fields)


@pytest.mark.parametrize(
    ("the_uplink", "no_reading", "expected"),
    [
        (Uplink("eth0", True), False, Shown("Ethernet (eth0)")),
        (Uplink("wlan0", True), False, Shown("Wi-Fi (wlan0)")),
        (Uplink("wg0", False), False, Shown("wg0 (not a Network link)")),
        (None, False, Shown("No Uplink", "negative")),
        (None, True, Shown("No reading", "warning")),
    ],
)
def test_the_uplink_line(
    the_uplink: Uplink | None, no_reading: bool, expected: Shown
) -> None:
    snapshot = _snapshot(uplink=the_uplink, uplink_no_reading=no_reading)

    assert uplink(snapshot) == expected


@pytest.mark.parametrize(
    ("age", "no_reading", "expected"),
    [
        (None, True, Shown("Waiting for the first reading.", "warning")),
        (1.4, False, Shown("Updated 1 s ago", "grey-6")),
        (6.0, False, Shown("Updated 6 s ago", "grey-6")),
        (45.2, True, Shown("No reading for 45 s.", "warning")),
    ],
)
def test_the_freshness_line(
    age: float | None, no_reading: bool, expected: Shown
) -> None:
    snapshot = _snapshot(last_sample_age_seconds=age, no_reading=no_reading)

    assert freshness(snapshot) == expected


def test_a_link_heading_names_its_kind_and_interface() -> None:
    assert heading(ETH0) == "Ethernet · eth0"
    assert heading(WLAN0) == "Wi-Fi · wlan0"


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (ETH0, Shown("Connected", "positive")),
        (
            ETH0.model_copy(update={"state": "Disconnected", "reason": "No cable."}),
            Shown("Disconnected", "grey-5", "No cable."),
        ),
        (
            ETH0.model_copy(
                update={"state": "No reading", "reason": "No reading for 8 s."}
            ),
            Shown("No reading", "warning", "No reading for 8 s."),
        ),
    ],
)
def test_the_state_line(link: NetworkLink, expected: Shown) -> None:
    assert state(link) == expected


def _eth(
    speed_mbps: Supported[int] | None, full_duplex: Supported[bool] | None
) -> NetworkLink:
    return ETH0.model_copy(
        update={
            "ethernet": EthernetDetails(speed_mbps=speed_mbps, full_duplex=full_duplex)
        }
    )


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (
            _eth(Supported(value=100), Supported(value=True)),
            Shown("100 Mbit/s", sub="Full duplex"),
        ),
        (
            _eth(Supported(value=10), Supported(value=False)),
            Shown("10 Mbit/s", sub="Half duplex"),
        ),
        (_eth(Supported(value=1000), None), Shown("1000 Mbit/s")),
        (_eth(Supported(), Supported()), Shown("—", sub="—")),
    ],
)
def test_the_speed_shows_duplex_beneath(link: NetworkLink, expected: Shown) -> None:
    assert speed(link) == expected


def test_no_speed_is_shown_when_not_supported_or_for_wifi() -> None:
    assert speed(_eth(None, None)) is None
    assert speed(WLAN0) is None


def test_the_address_shows_ipv6_beneath_ipv4() -> None:
    link = ETH0.model_copy(update={"ipv4": "192.0.2.10/24", "ipv6": "2001:db8::1/64"})

    assert address(link) == Shown("192.0.2.10/24", sub="2001:db8::1/64")
    assert address(ETH0) == Shown("—")


def test_traffic_is_down_then_up_and_blank_without_two_samples() -> None:
    link = ETH0.model_copy(
        update={
            "throughput": Throughput(
                rx_bytes_per_second=4200.0, tx_bytes_per_second=300.0
            )
        }
    )

    assert traffic(link) == Shown("↓4.1 KB/s ↑300 B/s")
    assert traffic(ETH0) == Shown("—")


def test_counters_total_errors_and_drops_with_link_changes_beneath() -> None:
    link = ETH0.model_copy(
        update={
            "counters": Counters(rx_errors=2, tx_errors=1, rx_drops=1200, tx_drops=34),
            "link_changes": 3,
        }
    )

    assert counters(link) == Shown("3 / 1,234", sub="3 link changes")
    assert counters(ETH0) == Shown("0 / 0", sub="0 link changes")
    one = ETH0.model_copy(update={"link_changes": 1})
    assert counters(one).sub == "1 link change"


def _wlan(
    **fields: Supported[str] | Supported[int] | Supported[Level] | str | None,
) -> NetworkLink:
    return WLAN0.model_copy(update={"wifi": WifiDetails.model_validate(fields)})


ASSOCIATED = _wlan(
    ssid=Supported(value="Test"),
    bssid=Supported(value="02:00:00:00:00:01"),
    frequency_mhz=Supported(value=5745),
    tx_bitrate_bps=Supported(value=325_000_000),
    rx_bitrate_bps=Supported(value=433_300_000),
    tx_failed=Supported(value=11),
)
DISCONNECTED = _wlan(
    ssid=Supported(),
    bssid=Supported(),
    frequency_mhz=Supported(),
    tx_bitrate_bps=Supported(),
    rx_bitrate_bps=Supported(),
    tx_failed=Supported(value=11),
)


def test_the_wifi_network_shows_the_ssid_with_the_bssid_beneath() -> None:
    assert wifi_network(ASSOCIATED) == Shown("Test", sub="02:00:00:00:00:01")
    assert wifi_network(DISCONNECTED) == Shown("—", sub="—")


@pytest.mark.parametrize(
    ("mhz", "expected"),
    [
        (2412, Shown("2.4 GHz", sub="2412 MHz")),
        (5745, Shown("5 GHz", sub="5745 MHz")),
        (5949, Shown("5 GHz", sub="5949 MHz")),
        # Outside 2.4 and 5 GHz (6 GHz is out of scope): no band, MHz kept.
        (5955, Shown("—", sub="5955 MHz")),
        (None, Shown("—", sub="—")),
    ],
)
def test_the_wifi_band_shows_the_frequency_beneath(
    mhz: int | None, expected: Shown
) -> None:
    assert wifi_band(_wlan(frequency_mhz=Supported(value=mhz))) == expected


def test_the_wifi_bitrate_is_sent_then_received_in_mbit_per_s() -> None:
    assert wifi_bitrate(ASSOCIATED) == Shown("↑325 ↓433.3 Mbit/s")
    assert wifi_bitrate(DISCONNECTED) == Shown("—")
    assert wifi_bitrate(_wlan(tx_bitrate_bps=Supported(value=6_000_000))) == Shown(
        "↑6 Mbit/s"
    )


def test_wifi_details_are_not_shown_when_not_supported_or_for_ethernet() -> None:
    for show in (wifi_network, wifi_band, wifi_bitrate):
        assert show(_wlan()) is None
        assert show(ETH0) is None


def test_failed_sends_show_beneath_the_errors_and_drops_with_link_changes() -> None:
    assert counters(ASSOCIATED).sub == "0 link changes · 11 failed sends"
    one = _wlan(tx_failed=Supported(value=1))
    assert counters(one).sub == "0 link changes · 1 failed send"


def test_a_wifi_adapter_with_no_reading_says_so_under_the_state() -> None:
    link = _wlan(no_reading_reason="The Wi-Fi adapter gave no reading.")

    assert state(link) == Shown(
        "Connected", "positive", "The Wi-Fi adapter gave no reading."
    )


@pytest.mark.parametrize(
    ("level", "color"),
    [("Good", "positive"), ("Marginal", "warning"), ("Poor", "negative")],
)
def test_wifi_link_shows_the_level_in_its_colour_with_the_dbm_beneath(
    level: Level, color: str
) -> None:
    link = _wlan(
        link_strength=Supported[Level](value=level),
        signal_dbm=Supported[int](value=-54),
    )

    # A true minus sign (U+2212), not a hyphen.
    assert wifi_strength(link) == Shown(level, color, "\u221254 dBm")


@pytest.mark.parametrize(
    ("link", "reason"),
    [
        (
            WLAN0.model_copy(
                update={
                    "state": "Disconnected",
                    "reason": "Wi-Fi is disconnected.",
                    "wifi": WifiDetails(
                        link_strength=Supported[Level](), signal_dbm=Supported()
                    ),
                }
            ),
            "Wi-Fi is disconnected.",
        ),
        (
            WLAN0.model_copy(
                update={
                    "state": "No reading",
                    "reason": "Waiting for the first reading.",
                    "wifi": WifiDetails(
                        link_strength=Supported[Level](), signal_dbm=Supported()
                    ),
                }
            ),
            "Waiting for the first reading.",
        ),
        (
            _wlan(
                no_reading_reason="The Wi-Fi adapter gave no reading.",
                link_strength=Supported[Level](),
                signal_dbm=Supported[int](),
            ),
            "The Wi-Fi adapter gave no reading.",
        ),
        (
            # Associated, but the station gave no dBm.
            _wlan(link_strength=Supported[Level](), signal_dbm=Supported()),
            "The Wi-Fi adapter gave no reading.",
        ),
    ],
)
def test_wifi_link_with_no_value_shows_a_dash_with_the_reason_as_its_tooltip(
    link: NetworkLink, reason: str
) -> None:
    assert wifi_strength(link) == Shown("—", tooltip=reason)


def test_wifi_link_is_not_shown_when_not_supported_or_for_ethernet() -> None:
    assert wifi_strength(_wlan()) is None
    assert wifi_strength(ETH0) is None
