"""Tests for the network-provisioning models — config knobs and state.

These units ship to unattended field devices, so the constraints here
are the last line of defence against a config file that would make a Pi
either unreachable or an open hotspot (issue #7).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from sp_rtk_base.models.net_provision_models import (
    DEFAULT_AP_SSID,
    Connectivity,
    NetProvisionConfig,
    NetworkState,
    ProvisionAction,
)

_PASSWORD = "sticker-secret"

_THRESHOLDS = [
    "boot_wait_seconds",
    "fallback_window_seconds",
    "rescan_interval_seconds",
    "poll_interval_seconds",
]


class TestNetProvisionConfigDefaults:
    """Defaults must be field-ready without a config file entry."""

    def test_only_the_ap_password_is_required(self) -> None:
        """Everything except the sticker password has a usable default."""
        config = NetProvisionConfig(ap_password=_PASSWORD)
        assert config.ap_ssid == DEFAULT_AP_SSID
        assert config.fallback_window_seconds == 300.0
        assert config.rescan_interval_seconds == 120.0

    def test_ap_password_has_no_default(self) -> None:
        """No unit ever ships with a hotspot password baked into source."""
        with pytest.raises(ValidationError):
            NetProvisionConfig()  # type: ignore[call-arg]

    def test_boot_wait_default_is_in_the_specified_range(self) -> None:
        """Story 14 pins the default boot-wait to the 30-60 s range."""
        default = NetProvisionConfig(ap_password=_PASSWORD).boot_wait_seconds
        assert 30.0 <= default <= 60.0

    def test_rescan_interval_is_shorter_than_the_fallback_window(self) -> None:
        """A device in AP mode must look for the WiFi more than once.

        With the defaults reversed, a unit that dropped to AP mode during
        an outage would sit there without ever rescanning for as long as
        it took the operator to notice.
        """
        config = NetProvisionConfig(ap_password=_PASSWORD)
        assert config.rescan_interval_seconds < config.fallback_window_seconds

    def test_poll_interval_is_shorter_than_every_other_threshold(self) -> None:
        """The loop tick must be finer-grained than the windows it measures.

        A poll interval as long as (or longer than) boot-wait, the
        fallback window, or the rescan interval would make those knobs
        meaningless — the loop could sleep straight through a threshold.
        """
        config = NetProvisionConfig(ap_password=_PASSWORD)
        assert config.poll_interval_seconds < config.boot_wait_seconds
        assert config.poll_interval_seconds < config.rescan_interval_seconds
        assert config.poll_interval_seconds < config.fallback_window_seconds


class TestNetProvisionConfigValidation:
    """Reject values NetworkManager or 802.11 would refuse."""

    @pytest.mark.parametrize("password", ["", "short", "1234567"])
    def test_password_below_the_wpa2_minimum_is_rejected(self, password: str) -> None:
        """WPA2-PSK needs 8 characters; a shorter one fails at AP creation."""
        with pytest.raises(ValidationError):
            NetProvisionConfig(ap_password=password)

    def test_password_at_the_wpa2_minimum_is_accepted(self) -> None:
        """Exactly 8 characters is legal."""
        assert len(NetProvisionConfig(ap_password="12345678").ap_password) == 8

    def test_password_above_the_wpa2_maximum_is_rejected(self) -> None:
        """WPA2-PSK caps the passphrase at 63 characters."""
        with pytest.raises(ValidationError):
            NetProvisionConfig(ap_password="x" * 64)

    def test_password_at_the_wpa2_maximum_is_accepted(self) -> None:
        """Exactly 63 characters is legal."""
        assert len(NetProvisionConfig(ap_password="x" * 63).ap_password) == 63

    def test_empty_ssid_is_rejected(self) -> None:
        """An empty SSID would broadcast an unnamed setup network."""
        with pytest.raises(ValidationError):
            NetProvisionConfig(ap_ssid="", ap_password=_PASSWORD)

    def test_ssid_above_32_characters_is_rejected(self) -> None:
        """802.11 caps the SSID at 32 characters."""
        with pytest.raises(ValidationError):
            NetProvisionConfig(ap_ssid="s" * 33, ap_password=_PASSWORD)

    def test_ssid_at_32_characters_is_accepted(self) -> None:
        """Exactly 32 characters is legal."""
        assert len(NetProvisionConfig(ap_ssid="s" * 32, ap_password=_PASSWORD).ap_ssid)

    @pytest.mark.parametrize("field", _THRESHOLDS)
    @pytest.mark.parametrize("value", [0.0, -1.0])
    def test_non_positive_thresholds_are_rejected(
        self, field: str, value: float
    ) -> None:
        """A zero or negative threshold would make the loop spin or never fire."""
        values: dict[str, Any] = {"ap_password": _PASSWORD, field: value}
        with pytest.raises(ValidationError):
            NetProvisionConfig(**values)

    @pytest.mark.parametrize("field", _THRESHOLDS)
    def test_thresholds_are_overridable(self, field: str) -> None:
        """Every timing knob is tunable per deployment without code changes."""
        values: dict[str, Any] = {"ap_password": _PASSWORD, field: 7.5}
        assert getattr(NetProvisionConfig(**values), field) == 7.5


class TestNetworkStateValidation:
    """The adapter cannot hand the decision core a nonsensical clock."""

    @pytest.mark.parametrize(
        "field", ["seconds_since_boot", "seconds_disconnected", "seconds_in_ap"]
    )
    def test_negative_durations_are_rejected(self, field: str) -> None:
        """Elapsed time never runs backwards."""
        values: dict[str, Any] = {
            "uplink_connectivity": Connectivity.NONE,
            "seconds_since_boot": 0.0,
            "seconds_disconnected": 0.0,
            field: -1.0,
        }
        with pytest.raises(ValidationError):
            NetworkState(**values)

    def test_clocks_are_required(self) -> None:
        """Uptime and outage duration have no sensible default."""
        with pytest.raises(ValidationError):
            NetworkState(uplink_connectivity=Connectivity.NONE)  # type: ignore[call-arg]

    def test_a_fresh_device_defaults_to_client_mode_unprovisioned(self) -> None:
        """Defaults describe a just-booted, never-configured unit."""
        state = NetworkState(
            uplink_connectivity=Connectivity.NONE,
            seconds_since_boot=0.0,
            seconds_disconnected=0.0,
        )
        assert not state.ap_active
        assert state.seconds_in_ap == 0.0
        assert not state.saved_wifi_known
        assert not state.saved_wifi_visible


class TestEnumWireValues:
    """Stable string values — these cross a YAML file and log lines."""

    def test_connectivity_values_match_nmcli_vocabulary(self) -> None:
        """``nmcli networking connectivity`` prints exactly these words."""
        assert [c.value for c in Connectivity] == ["none", "portal", "limited", "full"]

    def test_action_values(self) -> None:
        """Action names the adapter and logs refer to."""
        assert [a.value for a in ProvisionAction] == [
            "idle",
            "start_ap",
            "stop_ap_and_connect",
            "rescan",
        ]
