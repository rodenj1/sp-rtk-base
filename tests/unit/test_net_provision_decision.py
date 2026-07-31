"""Tests for the headless network-provisioning decision core.

Every test feeds an observable :class:`NetworkState` into
:func:`decide` and asserts the returned :class:`ProvisionAction`.
Nothing here inspects internal state, timers, or nmcli — that is the
whole point of the pure-function seam (issue #7).
"""

from __future__ import annotations

import pytest

from sp_rtk_base.models.net_provision_models import (
    Connectivity,
    NetProvisionConfig,
    NetworkState,
    ProvisionAction,
)
from sp_rtk_base.services.net_provision import decide


def _config(**overrides: object) -> NetProvisionConfig:
    """A valid config with test-friendly thresholds."""
    values: dict[str, object] = {
        "ap_password": "sticker-secret",
        "boot_wait_seconds": 45.0,
        "fallback_window_seconds": 300.0,
        "rescan_interval_seconds": 120.0,
    }
    values.update(overrides)
    return NetProvisionConfig(**values)  # type: ignore[arg-type]


class TestConnected:
    """A device with a working network is left alone."""

    def test_full_connectivity_is_idle(self) -> None:
        """Internet-reachable device stays idle."""
        state = NetworkState(
            connectivity=Connectivity.FULL,
            seconds_since_boot=10_000.0,
            seconds_disconnected=0.0,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE


class TestUnprovisionedBootWait:
    """A device that has never been provisioned opens the AP after boot-wait.

    Story 13: the delay exists so a slow-DHCP Ethernet link is not cut
    off prematurely.  Once it expires with still no network, the AP must
    come up — an installer needs a way in even when the cable leads to a
    dead switch.
    """

    def test_idle_while_still_within_boot_wait(self) -> None:
        """No network, but boot-wait has not expired yet."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=20.0,
            seconds_disconnected=20.0,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE

    def test_starts_ap_once_boot_wait_expires(self) -> None:
        """No network past boot-wait and never provisioned -> AP."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=60.0,
            seconds_disconnected=60.0,
        )
        assert decide(state, _config()) is ProvisionAction.START_AP

    def test_boot_wait_boundary_is_inclusive(self) -> None:
        """Exactly at the boot-wait threshold the AP starts."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=45.0,
            seconds_disconnected=45.0,
        )
        assert decide(state, _config()) is ProvisionAction.START_AP

    def test_just_below_boot_wait_boundary_is_idle(self) -> None:
        """One tick before the threshold nothing happens yet."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=44.9,
            seconds_disconnected=44.9,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE


class TestProvisionedFallbackWindow:
    """A provisioned device tolerates an outage before re-opening the AP.

    Stories 9 and 11: a router reboot or transient drop must not require
    a site visit, so the saved profile buys a longer grace period than
    the boot-wait an unprovisioned unit gets.
    """

    def test_idle_during_a_transient_outage(self) -> None:
        """Saved profile + short outage -> keep letting NetworkManager retry."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=200_000.0,
            seconds_disconnected=90.0,
            saved_wifi_known=True,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE

    def test_starts_ap_once_the_window_expires(self) -> None:
        """Saved profile unreachable for the full window -> AP."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=200_000.0,
            seconds_disconnected=600.0,
            saved_wifi_known=True,
        )
        assert decide(state, _config()) is ProvisionAction.START_AP

    def test_fallback_window_boundary_is_inclusive(self) -> None:
        """Exactly at the window the AP opens."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=200_000.0,
            seconds_disconnected=300.0,
            saved_wifi_known=True,
        )
        assert decide(state, _config()) is ProvisionAction.START_AP

    def test_just_below_the_window_is_idle(self) -> None:
        """One tick before the window nothing happens yet."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=200_000.0,
            seconds_disconnected=299.9,
            saved_wifi_known=True,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE

    def test_provisioned_device_gets_the_window_not_boot_wait_at_boot(self) -> None:
        """Booting with a saved profile waits the window, not boot-wait.

        The site WiFi may simply be slow to appear after a shared power
        cut; a provisioned unit should not hijack its own radio 45 s in.
        """
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=60.0,
            seconds_disconnected=60.0,
            saved_wifi_known=True,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE


class TestApModeRescan:
    """While serving the AP, periodically drop it and look for the WiFi.

    A Pi has one radio: it cannot serve the AP and scan for the site
    network at the same time, so the only way back to client mode is to
    let go of the AP at intervals and look.
    """

    def test_idle_under_the_rescan_interval(self) -> None:
        """A fresh AP session is left up."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=500.0,
            seconds_disconnected=500.0,
            ap_active=True,
            seconds_in_ap=30.0,
            saved_wifi_known=True,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE

    def test_rescans_past_the_interval(self) -> None:
        """An AP session older than the interval triggers a look around."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=500.0,
            seconds_disconnected=500.0,
            ap_active=True,
            seconds_in_ap=200.0,
            saved_wifi_known=True,
        )
        assert decide(state, _config()) is ProvisionAction.RESCAN

    def test_rescan_boundary_is_inclusive(self) -> None:
        """Exactly at the interval the rescan happens."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=500.0,
            seconds_disconnected=500.0,
            ap_active=True,
            seconds_in_ap=120.0,
            saved_wifi_known=True,
        )
        assert decide(state, _config()) is ProvisionAction.RESCAN

    def test_just_below_the_rescan_boundary_is_idle(self) -> None:
        """One tick before the interval the AP keeps serving."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=500.0,
            seconds_disconnected=500.0,
            ap_active=True,
            seconds_in_ap=119.9,
            saved_wifi_known=True,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE

    def test_never_starts_an_ap_that_is_already_running(self) -> None:
        """An unprovisioned unit long past boot-wait does not re-START_AP."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=5_000.0,
            seconds_disconnected=5_000.0,
            ap_active=True,
            seconds_in_ap=10.0,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE


class TestRescanOutcome:
    """What the orchestrator does with what the rescan found."""

    def test_saved_wifi_seen_tears_down_the_ap_and_connects(self) -> None:
        """The site WiFi is back -> return to client mode (stories 9, 11)."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=5_000.0,
            seconds_disconnected=5_000.0,
            ap_active=True,
            seconds_in_ap=5.0,
            saved_wifi_known=True,
            saved_wifi_visible=True,
        )
        assert decide(state, _config()) is ProvisionAction.STOP_AP_AND_CONNECT

    def test_saved_wifi_absent_resumes_the_ap(self) -> None:
        """Nothing to connect to -> keep the AP up for reconfiguration."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=5_000.0,
            seconds_disconnected=5_000.0,
            ap_active=True,
            seconds_in_ap=5.0,
            saved_wifi_known=True,
            saved_wifi_visible=False,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE

    def test_visible_network_without_a_saved_profile_is_not_connected_to(self) -> None:
        """An unprovisioned unit has no credentials to connect with."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=5_000.0,
            seconds_disconnected=5_000.0,
            ap_active=True,
            seconds_in_ap=5.0,
            saved_wifi_known=False,
            saved_wifi_visible=True,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE

    def test_connecting_wins_over_a_due_rescan(self) -> None:
        """Knowing the network is in range beats looking again."""
        state = NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=5_000.0,
            seconds_disconnected=5_000.0,
            ap_active=True,
            seconds_in_ap=999.0,
            saved_wifi_known=True,
            saved_wifi_visible=True,
        )
        assert decide(state, _config()) is ProvisionAction.STOP_AP_AND_CONNECT


class TestEthernetArrivesDuringSetup:
    """Plugging in a cable mid-setup ends the AP session.

    Story 1: Ethernet just works.  If the installer gives up on WiFi and
    runs a cable, the device should stop hosting a setup hotspot.
    """

    def test_network_while_ap_is_up_tears_the_ap_down(self) -> None:
        """Connectivity appeared on another interface -> drop the AP."""
        state = NetworkState(
            connectivity=Connectivity.LIMITED,
            seconds_since_boot=5_000.0,
            seconds_disconnected=0.0,
            ap_active=True,
            seconds_in_ap=60.0,
        )
        assert decide(state, _config()) is ProvisionAction.STOP_AP_AND_CONNECT


class TestNetworkPresentNeverStartsAp:
    """Any usable network suppresses the AP entirely.

    Ethernet-first (story 1) needs no special case: NetworkManager
    prefers the wired route, so a working cable simply shows up as
    connectivity and the orchestrator stays out of the way (story 24).
    A LAN-only site with an on-premise caster reports ``limited`` and is
    perfectly operational — treating that as "no network" would take a
    working install offline.
    """

    @pytest.mark.parametrize(
        "connectivity",
        [Connectivity.FULL, Connectivity.LIMITED, Connectivity.PORTAL],
    )
    def test_no_ap_while_a_network_is_present(self, connectivity: Connectivity) -> None:
        """Long past every threshold, a connected device is still idle."""
        state = NetworkState(
            connectivity=connectivity,
            seconds_since_boot=100_000.0,
            seconds_disconnected=0.0,
            saved_wifi_known=True,
        )
        assert decide(state, _config()) is ProvisionAction.IDLE

    def test_only_none_counts_as_disconnected(self) -> None:
        """has_network is the single definition of "connected"."""
        assert not NetworkState(
            connectivity=Connectivity.NONE,
            seconds_since_boot=0.0,
            seconds_disconnected=0.0,
        ).has_network


class TestFieldLifecycle:
    """The installer's journey and the outage that follows it, in order."""

    def test_unboxing_to_online(self) -> None:
        """Power on with no cable -> AP -> installer picks WiFi -> online."""
        config = _config()
        cfg_boot = config.boot_wait_seconds
        timeline: list[tuple[NetworkState, ProvisionAction]] = [
            # Just powered on: give a slow wired link its chance.
            (
                NetworkState(
                    connectivity=Connectivity.NONE,
                    seconds_since_boot=5.0,
                    seconds_disconnected=5.0,
                ),
                ProvisionAction.IDLE,
            ),
            # Boot-wait expired with nothing plugged in: open the hotspot.
            (
                NetworkState(
                    connectivity=Connectivity.NONE,
                    seconds_since_boot=cfg_boot + 1.0,
                    seconds_disconnected=cfg_boot + 1.0,
                ),
                ProvisionAction.START_AP,
            ),
            # Installer is on the portal picking a network; leave the AP up.
            (
                NetworkState(
                    connectivity=Connectivity.NONE,
                    seconds_since_boot=cfg_boot + 30.0,
                    seconds_disconnected=cfg_boot + 30.0,
                    ap_active=True,
                    seconds_in_ap=29.0,
                ),
                ProvisionAction.IDLE,
            ),
            # Credentials accepted, site WiFi up: provisioning goes quiet.
            (
                NetworkState(
                    connectivity=Connectivity.FULL,
                    seconds_since_boot=cfg_boot + 60.0,
                    seconds_disconnected=0.0,
                    saved_wifi_known=True,
                ),
                ProvisionAction.IDLE,
            ),
        ]
        for state, expected in timeline:
            assert decide(state, config) is expected

    def test_router_reboot_then_recovery(self) -> None:
        """A provisioned unit rides out an outage, then self-heals."""
        config = _config()
        window = config.fallback_window_seconds
        rescan = config.rescan_interval_seconds
        timeline: list[tuple[NetworkState, ProvisionAction]] = [
            # WiFi just dropped: NetworkManager retries, we do nothing.
            (
                NetworkState(
                    connectivity=Connectivity.NONE,
                    seconds_since_boot=90_000.0,
                    seconds_disconnected=30.0,
                    saved_wifi_known=True,
                ),
                ProvisionAction.IDLE,
            ),
            # Still down past the tolerance window: offer a way back in.
            (
                NetworkState(
                    connectivity=Connectivity.NONE,
                    seconds_since_boot=90_000.0,
                    seconds_disconnected=window + 1.0,
                    saved_wifi_known=True,
                ),
                ProvisionAction.START_AP,
            ),
            # AP has been up a while: let go of the radio and look.
            (
                NetworkState(
                    connectivity=Connectivity.NONE,
                    seconds_since_boot=90_000.0,
                    seconds_disconnected=window + rescan,
                    ap_active=True,
                    seconds_in_ap=rescan,
                    saved_wifi_known=True,
                ),
                ProvisionAction.RESCAN,
            ),
            # The scan found nothing: resume the AP, try again later.
            (
                NetworkState(
                    connectivity=Connectivity.NONE,
                    seconds_since_boot=90_000.0,
                    seconds_disconnected=window + rescan + 5.0,
                    ap_active=True,
                    seconds_in_ap=1.0,
                    saved_wifi_known=True,
                    saved_wifi_visible=False,
                ),
                ProvisionAction.IDLE,
            ),
            # Router is back: the next scan sees it, so go be a client.
            (
                NetworkState(
                    connectivity=Connectivity.NONE,
                    seconds_since_boot=90_000.0,
                    seconds_disconnected=window + 2 * rescan,
                    ap_active=True,
                    seconds_in_ap=rescan,
                    saved_wifi_known=True,
                    saved_wifi_visible=True,
                ),
                ProvisionAction.STOP_AP_AND_CONNECT,
            ),
            # Online again, hotspot gone (story 9, no site visit needed).
            (
                NetworkState(
                    connectivity=Connectivity.FULL,
                    seconds_since_boot=90_100.0,
                    seconds_disconnected=0.0,
                    saved_wifi_known=True,
                ),
                ProvisionAction.IDLE,
            ),
        ]
        for state, expected in timeline:
            assert decide(state, config) is expected
