"""Tests for the nmcli adapter — state in / commands out (issue #8).

Every test isolates the subprocess/nmcli boundary, the same external
boundary that ``test_device_service`` / ``test_config_service`` mock
out. The boundary here is a plain callable (:data:`NmcliRunner`)
rather than a class, so a hand-rolled fake stands in for
``unittest.mock.MagicMock(spec=...)``: it records the exact argv it
was invoked with and returns a canned result. Nothing here touches a
real nmcli binary, NetworkManager, or the decision core — only the
adapter's reads and command dispatch are under test.
"""

from __future__ import annotations

from typing import Any

import pytest

from sp_rtk_base.models.net_provision_models import (
    BandChangeApplied,
    BandChangePending,
    BandChangeRefused,
    BandChangeReverted,
    BandChangeSaved,
    Connectivity,
    NetProvisionConfig,
    NetworkState,
    ProvisionAction,
    SavedWifiConnection,
    WifiBand,
    WifiNetwork,
)
from sp_rtk_base.services.net_provision import (
    NmcliAdapter,
    NmcliError,
    WifiConnectError,
)
from sp_rtk_base.services.net_provision.nmcli_adapter import _run_nmcli

AP_SSID = "sp-rtk-base-setup"
SAVED_SSID = "SiteWiFi"

ACTIVE_CONNECTIONS = ["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"]
CONNECTIVITY_CHECK = ["nmcli", "-t", "networking", "connectivity", "check"]
ALL_CONNECTIONS = ["nmcli", "-t", "-f", "TYPE,NAME", "connection", "show"]
SCAN = [
    "nmcli",
    "-t",
    "-f",
    "SSID,SIGNAL,SECURITY,FREQ",
    "device",
    "wifi",
    "list",
    "--rescan",
    "yes",
]
DEVICES = ["nmcli", "-t", "-f", "DEVICE,TYPE", "device"]
WLAN0_5GHZ = ["nmcli", "-g", "WIFI-PROPERTIES.5GHZ", "device", "show", "wlan0"]
AP_UP = ["nmcli", "connection", "up", "id", AP_SSID]
AP_DOWN = ["nmcli", "connection", "down", "id", AP_SSID]
SAVED_UP = ["nmcli", "connection", "up", "id", SAVED_SSID]


def _band_of(name: str) -> list[str]:
    """`nmcli` reading one saved profile's Wi-Fi band."""
    return ["nmcli", "-g", "802-11-wireless.band", "connection", "show", "id", name]


def _set_band(name: str, nm_value: str) -> list[str]:
    """`nmcli` writing one saved profile's Wi-Fi band."""
    return [
        "nmcli",
        "connection",
        "modify",
        "id",
        name,
        "802-11-wireless.band",
        nm_value,
    ]


def _scan_line(
    ssid: str, signal: int = 50, security: str = "WPA2", freq: int = 2412
) -> str:
    """One `nmcli -t -f SSID,SIGNAL,SECURITY,FREQ device wifi list` line.

    nmcli prints FREQ with its unit even in terse mode, e.g. ``2412 MHz``.
    """
    return f"{ssid}:{signal}:{security}:{freq} MHz"


# ---------------------------------------------------------------------------
# Fake subprocess boundary
# ---------------------------------------------------------------------------


class FakeCompletedProcess:
    """Minimal stand-in for ``subprocess.CompletedProcess[str]``."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeNmcli:
    """Records every invocation; returns a canned result per exact argv.

    Unregistered commands return an empty, successful result — tests
    only need to stub the calls their scenario cares about.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._responses: dict[tuple[str, ...], list[FakeCompletedProcess]] = {}

    def set_response(
        self,
        args: list[str],
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self._responses[tuple(args)] = [
            FakeCompletedProcess(stdout, stderr, returncode)
        ]

    def queue_responses(self, args: list[str], *results: FakeCompletedProcess) -> None:
        """Answer successive calls of ``args`` with ``results`` in turn; the
        last one repeats once the others are used up."""
        self._responses[tuple(args)] = list(results)

    def __call__(self, args: list[str]) -> FakeCompletedProcess:
        self.calls.append(args)
        queued = self._responses.get(tuple(args))
        if not queued:
            return FakeCompletedProcess()
        return queued.pop(0) if len(queued) > 1 else queued[0]


def _config(**overrides: Any) -> NetProvisionConfig:
    values: dict[str, Any] = {"ap_ssid": AP_SSID, "ap_password": "sticker-secret"}
    values.update(overrides)
    return NetProvisionConfig(**values)


def _adapter(fake: FakeNmcli, **config_overrides: Any) -> NmcliAdapter:
    return NmcliAdapter(_config(**config_overrides), runner=fake)


def _read_state(adapter: NmcliAdapter, **overrides: Any) -> NetworkState:
    values: dict[str, Any] = {
        "seconds_since_boot": 1000.0,
        "seconds_disconnected": 0.0,
        "seconds_in_ap": 0.0,
    }
    values.update(overrides)
    return adapter.read_state(**values)


# ---------------------------------------------------------------------------
# Reads: uplink connectivity
# ---------------------------------------------------------------------------


class TestUplinkConnectivity:
    """Connectivity mapping — the two rules issue #8 exists to enforce."""

    def test_hotspot_only_host_reports_no_uplink(self) -> None:
        """A device whose only active connection is its own AP has no
        uplink, even though NM reports `limited` for that state —
        reporting it as an uplink would make the orchestrator tear down
        the AP it just raised and flap it every tick."""
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{AP_SSID}\n")
        fake.set_response(CONNECTIVITY_CHECK, stdout="limited\n")
        state = _read_state(_adapter(fake))
        assert state.uplink_connectivity is Connectivity.NONE
        assert state.ap_active is True

    def test_loopback_alongside_ap_still_reports_no_uplink(self) -> None:
        """NetworkManager's loopback connection ("lo") is active on every
        host regardless of any real uplink (issue #33). A hotspot-only
        host with nothing else connected must short-circuit to NONE
        without even consulting the live connectivity check — "lo"
        polluting the active set must not defeat that short-circuit the
        way it did in production (field device flapped every ~12s for
        5+ minutes with Ethernet physically unplugged)."""
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{AP_SSID}\nlo\n")
        fake.set_response(CONNECTIVITY_CHECK, stdout="full\n")
        state = _read_state(_adapter(fake))
        assert state.uplink_connectivity is Connectivity.NONE
        assert fake.calls.count(CONNECTIVITY_CHECK) == 0

    def test_unknown_with_active_non_ap_connection_is_limited(self) -> None:
        """NM's `unknown` (connectivity checking disabled, common on Pi
        images) maps to `limited` when some non-AP connection is active."""
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{SAVED_SSID}\n")
        fake.set_response(CONNECTIVITY_CHECK, stdout="unknown\n")
        state = _read_state(_adapter(fake))
        assert state.uplink_connectivity is Connectivity.LIMITED

    def test_unknown_with_no_active_connection_is_none(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout="")
        fake.set_response(CONNECTIVITY_CHECK, stdout="unknown\n")
        state = _read_state(_adapter(fake))
        assert state.uplink_connectivity is Connectivity.NONE

    def test_unrecognized_connectivity_value_falls_back_to_none(self) -> None:
        """A future nmcli value this adapter doesn't know about must not
        be mistaken for a usable uplink."""
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{SAVED_SSID}\n")
        fake.set_response(CONNECTIVITY_CHECK, stdout="some-new-state\n")
        state = _read_state(_adapter(fake))
        assert state.uplink_connectivity is Connectivity.NONE

    @pytest.mark.parametrize("raw", ["full", "limited", "portal", "none"])
    def test_raw_value_passes_through_when_non_ap_connection_active(
        self, raw: str
    ) -> None:
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{SAVED_SSID}\n")
        fake.set_response(CONNECTIVITY_CHECK, stdout=f"{raw}\n")
        state = _read_state(_adapter(fake))
        assert state.uplink_connectivity is Connectivity(raw)

    def test_ethernet_up_alongside_active_ap_reports_uplink(self) -> None:
        """An installer plugging in a cable mid-setup: the AP is still up
        but a second, non-AP connection now exists — that counts as an
        uplink even though the AP connection is also active."""
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{AP_SSID}\nWired connection 1\n")
        fake.set_response(CONNECTIVITY_CHECK, stdout="full\n")
        state = _read_state(_adapter(fake))
        assert state.uplink_connectivity is Connectivity.FULL
        assert state.ap_active is True


# ---------------------------------------------------------------------------
# Reads: saved WiFi profile presence
# ---------------------------------------------------------------------------


class TestSavedWifiKnown:
    def test_no_wireless_profile_besides_ap_is_not_known(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{AP_SSID}\n")
        state = _read_state(_adapter(fake))
        assert state.saved_wifi_known is False

    def test_wireless_profile_other_than_ap_is_known(self) -> None:
        fake = FakeNmcli()
        fake.set_response(
            ALL_CONNECTIONS,
            stdout=f"802-11-wireless:{AP_SSID}\n802-11-wireless:{SAVED_SSID}\n",
        )
        state = _read_state(_adapter(fake))
        assert state.saved_wifi_known is True

    def test_wired_profile_does_not_count_as_saved_wifi(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout="802-3-ethernet:Wired connection 1\n")
        state = _read_state(_adapter(fake))
        assert state.saved_wifi_known is False

    def test_saved_wifi_name_is_the_non_ap_wireless_profile(self) -> None:
        """issue #25: the supervisor keys its durable failure count to
        this name — exposed off the same lookup that sets
        saved_wifi_known, not a second nmcli round trip."""
        fake = FakeNmcli()
        fake.set_response(
            ALL_CONNECTIONS,
            stdout=f"802-11-wireless:{AP_SSID}\n802-11-wireless:{SAVED_SSID}\n",
        )
        state = _read_state(_adapter(fake))
        assert state.saved_wifi_name == SAVED_SSID

    def test_saved_wifi_name_is_none_when_only_the_ap_profile_exists(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{AP_SSID}\n")
        state = _read_state(_adapter(fake))
        assert state.saved_wifi_name is None

    def test_saved_wifi_name_is_none_when_no_wireless_profile_exists(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout="802-3-ethernet:Wired connection 1\n")
        state = _read_state(_adapter(fake))
        assert state.saved_wifi_name is None


# ---------------------------------------------------------------------------
# Reads: saved WiFi visibility (non-sticky)
# ---------------------------------------------------------------------------


class TestSavedWifiVisible:
    def test_defaults_to_false_before_any_scan(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        state = _read_state(_adapter(fake))
        assert state.saved_wifi_visible is False

    def test_true_after_rescan_finds_saved_ssid(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        fake.set_response(
            SCAN, stdout=f"{_scan_line('OtherNetwork')}\n{_scan_line(SAVED_SSID)}\n"
        )
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.RESCAN)
        state = _read_state(adapter)
        assert state.saved_wifi_visible is True

    def test_false_after_rescan_does_not_find_saved_ssid(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        fake.set_response(SCAN, stdout=f"{_scan_line('OtherNetwork')}\n")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.RESCAN)
        state = _read_state(adapter)
        assert state.saved_wifi_visible is False

    def test_reset_to_false_when_ap_restarted(self) -> None:
        """A stale True would retry a failing network every tick and leave
        no AP window to reconfigure through — the flag must not survive a
        fresh START_AP, even though it survives RESCAN's own transient
        down/up (which is what sets it in the first place)."""
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        fake.set_response(SCAN, stdout=f"{_scan_line(SAVED_SSID)}\n")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.RESCAN)
        adapter.execute(ProvisionAction.START_AP)
        state = _read_state(adapter)
        assert state.saved_wifi_visible is False


class TestSavedWifiVisibleWithABandLock:
    """Issue #152: the saved network counts as seen only if the rescan
    hears it on a band its Wi-Fi band allows. Otherwise the loop would
    drop the setup hotspot every rescan interval for a `con up` that is
    bound to fail."""

    @staticmethod
    def _rescan(*, nm_band: str, freqs: list[int]) -> NetworkState:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        fake.set_response(_band_of(SAVED_SSID), stdout=f"{nm_band}\n")
        fake.set_response(
            SCAN,
            stdout="".join(f"{_scan_line(SAVED_SSID, freq=f)}\n" for f in freqs),
        )
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.RESCAN)
        return _read_state(adapter)

    @pytest.mark.parametrize(
        ("nm_band", "freqs"),
        [
            pytest.param("bg", [5180], id="2.4-ghz-lock-heard-only-on-5-ghz"),
            pytest.param("a", [2412], id="5-ghz-lock-heard-only-on-2.4-ghz"),
        ],
    )
    def test_a_lock_that_excludes_every_band_heard_is_not_visible(
        self, nm_band: str, freqs: list[int]
    ) -> None:
        assert self._rescan(nm_band=nm_band, freqs=freqs).saved_wifi_visible is False

    @pytest.mark.parametrize(
        ("nm_band", "freqs"),
        [
            pytest.param("bg", [2412], id="2.4-ghz-lock-heard-on-2.4-ghz"),
            pytest.param("a", [5180], id="5-ghz-lock-heard-on-5-ghz"),
            pytest.param("a", [2412, 5180], id="5-ghz-lock-heard-on-both"),
        ],
    )
    def test_a_lock_that_matches_a_band_heard_is_visible(
        self, nm_band: str, freqs: list[int]
    ) -> None:
        assert self._rescan(nm_band=nm_band, freqs=freqs).saved_wifi_visible is True

    @pytest.mark.parametrize(
        "freqs",
        [
            pytest.param([2412], id="heard-on-2.4-ghz"),
            pytest.param([5180], id="heard-on-5-ghz"),
        ],
    )
    def test_auto_is_visible_on_either_band(self, freqs: list[int]) -> None:
        assert self._rescan(nm_band="", freqs=freqs).saved_wifi_visible is True


class TestSavedWifiBand:
    """Issue #152: the state carries the saved network's Wi-Fi band, so
    the supervisor can key its connect-failure record on it."""

    def test_the_state_carries_the_saved_networks_band(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        fake.set_response(_band_of(SAVED_SSID), stdout="bg\n")
        assert _read_state(_adapter(fake)).saved_wifi_band is WifiBand.GHZ_2_4

    def test_an_unlocked_saved_network_is_auto(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        assert _read_state(_adapter(fake)).saved_wifi_band is WifiBand.AUTO

    def test_no_band_without_a_saved_network(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{AP_SSID}\n")
        state = _read_state(_adapter(fake))
        assert state.saved_wifi_band is None
        assert _band_of(AP_SSID) not in fake.calls


# ---------------------------------------------------------------------------
# Commands: IDLE / START_AP
# ---------------------------------------------------------------------------


class TestExecuteIdle:
    def test_issues_no_commands(self) -> None:
        fake = FakeNmcli()
        _adapter(fake).execute(ProvisionAction.IDLE)
        assert fake.calls == []


class TestExecuteStartAp:
    def test_scans_then_activates_ap_connection(self) -> None:
        """The radio is free right up until the AP comes up — this is the
        one moment a fresh scan is cheap, so the portal has a network list
        ready the instant an installer's phone associates."""
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout=f"{_scan_line(SAVED_SSID)}\n")
        _adapter(fake).execute(ProvisionAction.START_AP)
        assert fake.calls == [SCAN, AP_UP]

    def test_caches_the_pre_ap_scan_for_the_portal(self) -> None:
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout=f"{_scan_line(SAVED_SSID, signal=80)}\n")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.START_AP)
        assert adapter.latest_scan() == [
            WifiNetwork(
                ssid=SAVED_SSID,
                signal=80,
                security="WPA2",
                band_signals={WifiBand.GHZ_2_4: 80},
            )
        ]

    def test_raises_nmcli_error_when_ap_fails_to_come_up(self) -> None:
        fake = FakeNmcli()
        fake.set_response(AP_UP, stderr="Error: unknown connection.", returncode=10)
        with pytest.raises(NmcliError):
            _adapter(fake).execute(ProvisionAction.START_AP)


# ---------------------------------------------------------------------------
# Commands: RESCAN
# ---------------------------------------------------------------------------


class TestExecuteRescan:
    def test_issues_down_then_scan_then_up_in_order(self) -> None:
        """Single radio: the AP has to be let go of before the saved
        network can even be looked for, then resumed so decide() sees it
        active again on the next tick. The saved network's Wi-Fi band is
        read once it has been heard (issue #152), still before the AP
        comes back up."""
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        fake.set_response(SCAN, stdout=f"{_scan_line(SAVED_SSID)}\n")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.RESCAN)
        assert fake.calls == [
            AP_DOWN,
            ALL_CONNECTIONS,
            SCAN,
            _band_of(SAVED_SSID),
            AP_UP,
        ]

    def test_resumes_ap_even_without_a_saved_profile(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout="")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.RESCAN)
        assert fake.calls[-1] == AP_UP
        assert _read_state(adapter).saved_wifi_visible is False

    def test_caches_the_rescan_results_for_the_portal(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout="")
        fake.set_response(SCAN, stdout=f"{_scan_line('OtherNetwork', signal=40)}\n")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.RESCAN)
        assert adapter.latest_scan() == [
            WifiNetwork(
                ssid="OtherNetwork",
                signal=40,
                security="WPA2",
                band_signals={WifiBand.GHZ_2_4: 40},
            )
        ]

    def test_raises_nmcli_error_when_ap_fails_to_resume(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout="")
        fake.set_response(AP_UP, stderr="Error: unknown connection.", returncode=10)
        with pytest.raises(NmcliError):
            _adapter(fake).execute(ProvisionAction.RESCAN)


# ---------------------------------------------------------------------------
# Commands: STOP_AP_AND_CONNECT
# ---------------------------------------------------------------------------


class TestExecuteStopApAndConnect:
    def test_ethernet_already_up_tears_down_ap_without_connecting(self) -> None:
        """decide() also returns STOP_AP_AND_CONNECT when another
        interface (e.g. Ethernet) already provides an uplink — there is
        nothing to actively connect to, so the adapter must not attempt
        one."""
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{AP_SSID}\nWired connection 1\n")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.STOP_AP_AND_CONNECT)
        assert fake.calls == [ACTIVE_CONNECTIONS, AP_DOWN]

    def test_connects_to_saved_wifi_when_no_other_uplink(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{AP_SSID}\n")
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.STOP_AP_AND_CONNECT)
        assert fake.calls == [ACTIVE_CONNECTIONS, AP_DOWN, ALL_CONNECTIONS, SAVED_UP]

    def test_raises_distinct_error_on_connect_failure(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{AP_SSID}\n")
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        fake.set_response(
            SAVED_UP,
            stderr=(
                "Error: Connection activation failed: Secrets were "
                "required, but not provided."
            ),
            returncode=4,
        )
        adapter = _adapter(fake)
        with pytest.raises(WifiConnectError) as exc_info:
            adapter.execute(ProvisionAction.STOP_AP_AND_CONNECT)
        assert exc_info.value.ssid == SAVED_SSID

    def test_does_not_attempt_connect_when_no_saved_profile(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{AP_SSID}\n")
        fake.set_response(ALL_CONNECTIONS, stdout="")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.STOP_AP_AND_CONNECT)
        assert fake.calls == [ACTIVE_CONNECTIONS, AP_DOWN, ALL_CONNECTIONS]

    def test_skips_ap_teardown_when_ap_already_inactive(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout="Wired connection 1\n")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.STOP_AP_AND_CONNECT)
        assert fake.calls == [ACTIVE_CONNECTIONS]


# ---------------------------------------------------------------------------
# scan_networks() — public scan-with-metadata (issue #10)
# ---------------------------------------------------------------------------


class TestScanNetworks:
    def test_parses_ssid_signal_and_security(self) -> None:
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout=f"{_scan_line(SAVED_SSID, signal=64)}\n")
        networks = _adapter(fake).scan_networks()
        assert networks == [
            WifiNetwork(
                ssid=SAVED_SSID,
                signal=64,
                security="WPA2",
                band_signals={WifiBand.GHZ_2_4: 64},
            )
        ]

    def test_normalizes_open_network_security_dash_to_empty_string(self) -> None:
        """nmcli prints `--` in the SECURITY column for an open network —
        the portal shouldn't have to know nmcli-specific sentinels."""
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout=f"{_scan_line('Guest', security='--')}\n")
        networks = _adapter(fake).scan_networks()
        assert networks[0].security == ""

    def test_sorts_strongest_signal_first(self) -> None:
        fake = FakeNmcli()
        fake.set_response(
            SCAN,
            stdout=(
                f"{_scan_line('Weak', signal=20)}\n{_scan_line('Strong', signal=90)}\n"
            ),
        )
        networks = _adapter(fake).scan_networks()
        assert [n.ssid for n in networks] == ["Strong", "Weak"]

    def test_deduplicates_by_ssid_keeping_the_strongest_signal(self) -> None:
        """The same network can show up once per BSSID (repeater APs,
        band-steering); the portal wants one row per SSID to pick from."""
        fake = FakeNmcli()
        fake.set_response(
            SCAN,
            stdout=(
                f"{_scan_line(SAVED_SSID, signal=30)}\n"
                f"{_scan_line(SAVED_SSID, signal=75)}\n"
            ),
        )
        networks = _adapter(fake).scan_networks()
        assert networks == [
            WifiNetwork(
                ssid=SAVED_SSID,
                signal=75,
                security="WPA2",
                band_signals={WifiBand.GHZ_2_4: 75},
            )
        ]

    def test_an_ssid_heard_on_both_bands_reports_the_signal_on_each(self) -> None:
        fake = FakeNmcli()
        fake.set_response(
            SCAN,
            stdout=(
                f"{_scan_line('FarmShop', signal=82, freq=2437)}\n"
                f"{_scan_line('FarmShop', signal=41, freq=5180)}\n"
            ),
        )
        [network] = _adapter(fake).scan_networks()
        assert network.band_signals == {WifiBand.GHZ_2_4: 82, WifiBand.GHZ_5: 41}
        assert network.signal == 82

    def test_an_ssid_heard_on_only_one_band_reports_only_that_band(self) -> None:
        fake = FakeNmcli()
        fake.set_response(
            SCAN, stdout=f"{_scan_line('Neighbour-5G', signal=55, freq=5745)}\n"
        )
        [network] = _adapter(fake).scan_networks()
        assert network.band_signals == {WifiBand.GHZ_5: 55}

    def test_the_strongest_bss_on_each_band_wins(self) -> None:
        """Several access points serving one SSID on the same band (a mesh,
        repeaters): the band reports the strongest of them."""
        fake = FakeNmcli()
        fake.set_response(
            SCAN,
            stdout=(
                f"{_scan_line('Home-Mesh', signal=35, freq=2412)}\n"
                f"{_scan_line('Home-Mesh', signal=22, freq=5180)}\n"
                f"{_scan_line('Home-Mesh', signal=60, freq=2462)}\n"
                f"{_scan_line('Home-Mesh', signal=48, freq=5500)}\n"
                f"{_scan_line('Home-Mesh', signal=30, freq=2437)}\n"
            ),
        )
        [network] = _adapter(fake).scan_networks()
        assert network.band_signals == {WifiBand.GHZ_2_4: 60, WifiBand.GHZ_5: 48}
        assert network.signal == 60

    def test_a_freq_outside_both_bands_is_ignored_for_the_band_signals(
        self,
    ) -> None:
        """A 6 GHz BSS (5955 MHz) belongs to neither band. It still counts
        toward the overall signal, so the setup hotspot's Wi-Fi picker lists
        the network exactly as before."""
        fake = FakeNmcli()
        fake.set_response(
            SCAN,
            stdout=(
                f"{_scan_line('Office', signal=90, freq=5955)}\n"
                f"{_scan_line('Office', signal=40, freq=2412)}\n"
                f"{_scan_line('SixOnly', signal=70, freq=6115)}\n"
            ),
        )
        networks = _adapter(fake).scan_networks()
        assert networks == [
            WifiNetwork(
                ssid="Office",
                signal=90,
                security="WPA2",
                band_signals={WifiBand.GHZ_2_4: 40},
            ),
            WifiNetwork(ssid="SixOnly", signal=70, security="WPA2"),
        ]

    def test_ignores_blank_lines(self) -> None:
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout=f"\n{_scan_line(SAVED_SSID)}\n\n")
        networks = _adapter(fake).scan_networks()
        assert len(networks) == 1

    def test_empty_scan_yields_no_networks(self) -> None:
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout="")
        assert _adapter(fake).scan_networks() == []

    def test_every_result_is_flagged_in_range(self) -> None:
        """issue #21: the console's scan surface reports SSID, signal,
        security, and an in-range flag — true for every entry here, since
        a fresh nmcli scan only ever returns networks it just detected."""
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout=f"{_scan_line(SAVED_SSID)}\n")
        networks = _adapter(fake).scan_networks()
        assert all(network.in_range for network in networks)


# ---------------------------------------------------------------------------
# has_5ghz() — whether the Wi-Fi adapter has 5 GHz (spec #147)
# ---------------------------------------------------------------------------

_DEVICE_LIST = "eth0:ethernet\nwlan0:wifi\np2p-dev-wlan0:wifi-p2p\nlo:loopback\n"


class TestHas5Ghz:
    def test_yes_when_the_wifi_device_reports_5ghz(self) -> None:
        fake = FakeNmcli()
        fake.set_response(DEVICES, stdout=_DEVICE_LIST)
        fake.set_response(WLAN0_5GHZ, stdout="yes\n")
        assert _adapter(fake).has_5ghz() is True
        assert fake.calls == [DEVICES, WLAN0_5GHZ]

    def test_no_when_the_wifi_device_lacks_5ghz(self) -> None:
        """A Pi Zero W or Pi 3B: 2.4 GHz-only hardware."""
        fake = FakeNmcli()
        fake.set_response(DEVICES, stdout=_DEVICE_LIST)
        fake.set_response(WLAN0_5GHZ, stdout="no\n")
        assert _adapter(fake).has_5ghz() is False

    def test_unknown_when_nmcli_reports_unknown(self) -> None:
        fake = FakeNmcli()
        fake.set_response(DEVICES, stdout=_DEVICE_LIST)
        fake.set_response(WLAN0_5GHZ, stdout="unknown\n")
        assert _adapter(fake).has_5ghz() is None

    def test_unknown_without_a_wifi_device(self) -> None:
        fake = FakeNmcli()
        fake.set_response(DEVICES, stdout="eth0:ethernet\nlo:loopback\n")
        assert _adapter(fake).has_5ghz() is None
        assert fake.calls == [DEVICES]

    def test_unknown_when_the_device_query_fails(self) -> None:
        fake = FakeNmcli()
        fake.set_response(DEVICES, stdout=_DEVICE_LIST)
        fake.set_response(WLAN0_5GHZ, stderr="Error: boom", returncode=10)
        assert _adapter(fake).has_5ghz() is None


# ---------------------------------------------------------------------------
# latest_scan() — cache read by the portal (issue #10)
# ---------------------------------------------------------------------------


class TestLatestScan:
    def test_empty_before_any_ap_session_or_rescan(self) -> None:
        fake = FakeNmcli()
        assert _adapter(fake).latest_scan() == []

    def test_returns_a_copy_not_the_internal_list(self) -> None:
        """Callers (the portal) must not be able to corrupt adapter state
        by mutating what they got back."""
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout=f"{_scan_line(SAVED_SSID)}\n")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.START_AP)
        adapter.latest_scan().clear()
        assert adapter.latest_scan() != []


# ---------------------------------------------------------------------------
# connect_to_network() — installer-submitted SSID+password (issue #10)
# ---------------------------------------------------------------------------


class TestConnectToNetwork:
    def test_issues_nmcli_device_wifi_connect(self) -> None:
        fake = FakeNmcli()
        _adapter(fake).connect_to_network("SiteWiFi", "hunter22")
        assert fake.calls == [
            ["nmcli", "device", "wifi", "connect", "SiteWiFi", "password", "hunter22"]
        ]

    def test_raises_wifi_connect_error_on_wrong_password(self) -> None:
        fake = FakeNmcli()
        connect_cmd = [
            "nmcli",
            "device",
            "wifi",
            "connect",
            "SiteWiFi",
            "password",
            "wrong",
        ]
        fake.set_response(
            connect_cmd,
            stderr="Error: Connection activation failed: Secrets were required.",
            returncode=4,
        )
        adapter = _adapter(fake)
        with pytest.raises(WifiConnectError) as exc_info:
            adapter.connect_to_network("SiteWiFi", "wrong")
        assert exc_info.value.ssid == "SiteWiFi"

    def test_succeeds_silently_on_a_correct_password(self) -> None:
        fake = FakeNmcli()
        _adapter(fake).connect_to_network("SiteWiFi", "hunter22")  # no raise

    def test_hidden_network_adds_hidden_yes_flag(self) -> None:
        """issue #21: the console lets an operator type a hidden SSID that
        never shows up in a scan — nmcli needs an explicit `hidden yes` to
        even attempt associating with it."""
        fake = FakeNmcli()
        _adapter(fake).connect_to_network("HiddenNet", "hunter22", hidden=True)
        assert fake.calls == [
            [
                "nmcli",
                "device",
                "wifi",
                "connect",
                "HiddenNet",
                "password",
                "hunter22",
                "hidden",
                "yes",
            ]
        ]

    def test_visible_network_omits_hidden_flag(self) -> None:
        """Default behavior (issue #10's portal call site) is unchanged."""
        fake = FakeNmcli()
        _adapter(fake).connect_to_network("SiteWiFi", "hunter22", hidden=False)
        assert fake.calls == [
            ["nmcli", "device", "wifi", "connect", "SiteWiFi", "password", "hunter22"]
        ]


JOIN_UUID = "7f8d1c2e-0000-4000-8000-000000000001"


def _join_add(
    ssid: str,
    band: str,
    *,
    password: str | None = "hunter22",
    hidden: bool = False,
    key_mgmt: str = "wpa-psk",
) -> list[str]:
    """The `connection add` argv a join with a Wi-Fi band issues."""
    args = [
        "nmcli",
        "connection",
        "add",
        "type",
        "wifi",
        "con-name",
        ssid,
        "ssid",
        ssid,
        "802-11-wireless.band",
        band,
    ]
    if hidden:
        args += ["802-11-wireless.hidden", "yes"]
    if password:
        args += ["wifi-sec.key-mgmt", key_mgmt, "wifi-sec.psk", password]
    return args


def _added(fake: FakeNmcli, add_args: list[str], ssid: str) -> None:
    fake.set_response(
        add_args,
        stdout=f"Connection '{ssid}' ({JOIN_UUID}) successfully added.\n",
    )


JOIN_UP = ["nmcli", "connection", "up", "uuid", JOIN_UUID]
JOIN_DELETE = ["nmcli", "connection", "delete", "uuid", JOIN_UUID]


class TestConnectToNetworkWithBand:
    """issue #153: a network joined from the console's join form is
    locked to its Wi-Fi band from its very first join. ``nmcli device
    wifi connect`` has no band option, so the profile is created with
    the band set and then brought up."""

    def test_auto_leaves_the_join_argv_unchanged(self) -> None:
        fake = FakeNmcli()
        _adapter(fake).connect_to_network("SiteWiFi", "hunter22", band=WifiBand.AUTO)
        assert fake.calls == [
            ["nmcli", "device", "wifi", "connect", "SiteWiFi", "password", "hunter22"]
        ]

    @pytest.mark.parametrize(
        ("band", "nm_value"), [(WifiBand.GHZ_2_4, "bg"), (WifiBand.GHZ_5, "a")]
    )
    def test_a_band_creates_the_profile_with_it_then_brings_it_up(
        self, band: WifiBand, nm_value: str
    ) -> None:
        fake = FakeNmcli()
        add = _join_add("SiteWiFi", nm_value)
        _added(fake, add, "SiteWiFi")
        _adapter(fake).connect_to_network("SiteWiFi", "hunter22", band=band)
        assert fake.calls == [SCAN, ALL_CONNECTIONS, add, JOIN_UP]

    def test_an_open_network_gets_no_security_settings(self) -> None:
        fake = FakeNmcli()
        add = _join_add("OpenNet", "bg", password="")
        _added(fake, add, "OpenNet")
        _adapter(fake).connect_to_network("OpenNet", "", band=WifiBand.GHZ_2_4)
        assert fake.calls == [SCAN, ALL_CONNECTIONS, add, JOIN_UP]

    def test_a_hidden_network_is_marked_hidden(self) -> None:
        fake = FakeNmcli()
        add = _join_add("HiddenNet", "a", hidden=True)
        _added(fake, add, "HiddenNet")
        _adapter(fake).connect_to_network(
            "HiddenNet", "hunter22", hidden=True, band=WifiBand.GHZ_5
        )
        # A hidden network isn't in a scan by name, so none is taken.
        assert fake.calls == [ALL_CONNECTIONS, add, JOIN_UP]

    def test_a_failed_join_deletes_the_new_profile_and_raises(self) -> None:
        """A join that fails because of the band is an ordinary join
        failure: nothing is reverted to Auto, and no half-made profile is
        left behind for the decision loop to treat as the saved network,
        the same as ``nmcli device wifi connect`` does."""
        fake = FakeNmcli()
        add = _join_add("SiteWiFi", "a")
        _added(fake, add, "SiteWiFi")
        fake.set_response(
            JOIN_UP,
            stderr="Error: Connection activation failed: No network with SSID.",
            returncode=4,
        )
        with pytest.raises(WifiConnectError) as exc_info:
            _adapter(fake).connect_to_network(
                "SiteWiFi", "hunter22", band=WifiBand.GHZ_5
            )
        assert exc_info.value.ssid == "SiteWiFi"
        assert "No network with SSID" in str(exc_info.value)
        assert fake.calls == [SCAN, ALL_CONNECTIONS, add, JOIN_UP, JOIN_DELETE]

    def test_a_failed_profile_add_raises_without_bringing_anything_up(
        self,
    ) -> None:
        fake = FakeNmcli()
        add = _join_add("SiteWiFi", "bg")
        fake.set_response(add, stderr="Error: invalid property.", returncode=2)
        with pytest.raises(WifiConnectError) as exc_info:
            _adapter(fake).connect_to_network(
                "SiteWiFi", "hunter22", band=WifiBand.GHZ_2_4
            )
        assert exc_info.value.ssid == "SiteWiFi"
        assert fake.calls == [SCAN, ALL_CONNECTIONS, add]

    def test_an_unreadable_add_output_falls_back_to_the_profile_name(
        self,
    ) -> None:
        fake = FakeNmcli()
        add = _join_add("SiteWiFi", "bg")
        _adapter(fake).connect_to_network("SiteWiFi", "hunter22", band=WifiBand.GHZ_2_4)
        assert fake.calls == [
            SCAN,
            ALL_CONNECTIONS,
            add,
            ["nmcli", "connection", "up", "id", "SiteWiFi"],
        ]

    @pytest.mark.parametrize(
        ("security", "key_mgmt"),
        [
            ("WPA2", "wpa-psk"),
            ("WPA1 WPA2", "wpa-psk"),
            ("WPA2 WPA3", "wpa-psk"),
            ("WPA3", "sae"),
        ],
    )
    def test_the_security_comes_from_the_scan(
        self, security: str, key_mgmt: str
    ) -> None:
        """A WPA3-only network takes SAE; anything that also offers WPA2
        (or WPA1) joins with WPA-PSK."""
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout=_scan_line("SiteWiFi", 70, security, 5180))
        add = _join_add("SiteWiFi", "a", key_mgmt=key_mgmt)
        _added(fake, add, "SiteWiFi")
        _adapter(fake).connect_to_network("SiteWiFi", "hunter22", band=WifiBand.GHZ_5)
        assert fake.calls == [SCAN, ALL_CONNECTIONS, add, JOIN_UP]

    def test_an_open_network_in_the_scan_ignores_a_typed_password(self) -> None:
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout="OpenNet:60:--:2412 MHz\n")
        add = _join_add("OpenNet", "bg", password="")
        _added(fake, add, "OpenNet")
        _adapter(fake).connect_to_network("OpenNet", "oops", band=WifiBand.GHZ_2_4)
        assert fake.calls == [SCAN, ALL_CONNECTIONS, add, JOIN_UP]

    @pytest.mark.parametrize("security", ["WEP", "WPA2 802.1X", "WPA3 802.1X", "OWE"])
    def test_security_a_band_join_cant_build_is_refused_untouched(
        self, security: str
    ) -> None:
        """No profile is built with the wrong security: the join fails
        with a reason, and nothing is added."""
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout=_scan_line("Corp", 70, security, 5180))
        with pytest.raises(WifiConnectError) as exc_info:
            _adapter(fake).connect_to_network("Corp", "hunter22", band=WifiBand.GHZ_5)
        assert exc_info.value.ssid == "Corp"
        assert "Auto" in str(exc_info.value)
        assert fake.calls == [SCAN]

    def test_an_existing_profile_is_modified_not_duplicated(self) -> None:
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout=_scan_line("SiteWiFi", 70, "WPA2", 5180))
        fake.set_response(ALL_CONNECTIONS, stdout="802-11-wireless:SiteWiFi\n")
        _adapter(fake).connect_to_network("SiteWiFi", "hunter22", band=WifiBand.GHZ_5)
        assert fake.calls == [
            SCAN,
            ALL_CONNECTIONS,
            [
                *_set_band("SiteWiFi", "a"),
                "wifi-sec.key-mgmt",
                "wpa-psk",
                "wifi-sec.psk",
                "hunter22",
            ],
            ["nmcli", "connection", "up", "id", "SiteWiFi"],
        ]

    def test_an_existing_profile_without_a_password_keeps_its_secret(
        self,
    ) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout="802-11-wireless:SiteWiFi\n")
        _adapter(fake).connect_to_network("SiteWiFi", "", band=WifiBand.GHZ_2_4)
        assert fake.calls == [
            SCAN,
            ALL_CONNECTIONS,
            _set_band("SiteWiFi", "bg"),
            ["nmcli", "connection", "up", "id", "SiteWiFi"],
        ]

    def test_an_existing_profile_that_fails_to_come_up_is_kept(self) -> None:
        """It was the operator's saved network before this join; a
        failed join mustn't delete it."""
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout="802-11-wireless:SiteWiFi\n")
        fake.set_response(
            ["nmcli", "connection", "up", "id", "SiteWiFi"],
            stderr="Error: Connection activation failed.",
            returncode=4,
        )
        with pytest.raises(WifiConnectError):
            _adapter(fake).connect_to_network("SiteWiFi", "", band=WifiBand.GHZ_2_4)
        assert not any("delete" in call for call in fake.calls)

    def test_a_failed_modify_of_an_existing_profile_raises(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout="802-11-wireless:SiteWiFi\n")
        fake.set_response(
            _set_band("SiteWiFi", "bg"), stderr="Error: bad.", returncode=2
        )
        with pytest.raises(WifiConnectError):
            _adapter(fake).connect_to_network("SiteWiFi", "", band=WifiBand.GHZ_2_4)
        assert fake.calls == [SCAN, ALL_CONNECTIONS, _set_band("SiteWiFi", "bg")]


# ---------------------------------------------------------------------------
# list_saved_connections() — console saved-profile listing (issue #21)
# ---------------------------------------------------------------------------


class TestListSavedConnections:
    def test_lists_wireless_profiles_excluding_the_ap(self) -> None:
        fake = FakeNmcli()
        fake.set_response(
            ALL_CONNECTIONS,
            stdout=(
                f"802-11-wireless:{AP_SSID}\n"
                f"802-11-wireless:{SAVED_SSID}\n"
                "802-3-ethernet:Wired connection 1\n"
            ),
        )
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{SAVED_SSID}\n")
        connections = _adapter(fake).list_saved_connections()
        assert connections == [SavedWifiConnection(name=SAVED_SSID, active=True)]

    def test_marks_inactive_profile_as_not_active(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{AP_SSID}\n")
        connections = _adapter(fake).list_saved_connections()
        assert connections == [SavedWifiConnection(name=SAVED_SSID, active=False)]

    def test_no_saved_wireless_profiles_yields_empty_list(self) -> None:
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{AP_SSID}\n")
        fake.set_response(ACTIVE_CONNECTIONS, stdout="")
        assert _adapter(fake).list_saved_connections() == []

    def test_each_saved_network_carries_the_band_read_from_its_profile(
        self,
    ) -> None:
        fake = FakeNmcli()
        fake.set_response(
            ALL_CONNECTIONS,
            stdout=(
                "802-11-wireless:FarmShop\n"
                "802-11-wireless:Barn-AP\n"
                "802-11-wireless:Office-5G\n"
            ),
        )
        fake.set_response(ACTIVE_CONNECTIONS, stdout="FarmShop\n")
        fake.set_response(_band_of("FarmShop"), stdout="\n")
        fake.set_response(_band_of("Barn-AP"), stdout="bg\n")
        fake.set_response(_band_of("Office-5G"), stdout="a\n")
        connections = _adapter(fake).list_saved_connections()
        assert connections == [
            SavedWifiConnection(name="FarmShop", active=True, band=WifiBand.AUTO),
            SavedWifiConnection(name="Barn-AP", active=False, band=WifiBand.GHZ_2_4),
            SavedWifiConnection(name="Office-5G", active=False, band=WifiBand.GHZ_5),
        ]
        assert fake.calls == [
            ALL_CONNECTIONS,
            ACTIVE_CONNECTIONS,
            _band_of("FarmShop"),
            _band_of("Barn-AP"),
            _band_of("Office-5G"),
        ]

    @pytest.mark.parametrize(
        ("stdout", "returncode"),
        [("", 10), ("abg\n", 0)],
        ids=["read-fails", "unknown-value"],
    )
    def test_a_band_that_cant_be_read_lists_as_auto(
        self, stdout: str, returncode: int
    ) -> None:
        """One unreadable profile mustn't hide the whole saved list; Auto
        is what NetworkManager does with a profile that has no lock."""
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        fake.set_response(
            _band_of(SAVED_SSID), stdout=stdout, stderr="Error", returncode=returncode
        )
        assert _adapter(fake).list_saved_connections() == [
            SavedWifiConnection(name=SAVED_SSID, active=False, band=WifiBand.AUTO)
        ]


# ---------------------------------------------------------------------------
# change_band() — spec #147
# ---------------------------------------------------------------------------


class TestChangeBand:
    @pytest.mark.parametrize(
        ("band", "nm_value"),
        [(WifiBand.GHZ_2_4, "bg"), (WifiBand.GHZ_5, "a"), (WifiBand.AUTO, "")],
    )
    def test_a_network_that_isnt_active_is_saved_straight_away(
        self, band: WifiBand, nm_value: str
    ) -> None:
        """No reconnect and no scan: the profile is only written."""
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout="FarmShop\n")
        outcome = _adapter(fake).change_band("Barn-AP", band)
        assert outcome == BandChangeSaved(name="Barn-AP", band=band)
        assert fake.calls == [ACTIVE_CONNECTIONS, _set_band("Barn-AP", nm_value)]

    def test_a_failed_write_raises(self) -> None:
        fake = FakeNmcli()
        fake.set_response(
            _set_band("Barn-AP", "bg"),
            stderr="Error: unknown connection 'Barn-AP'.",
            returncode=10,
        )
        with pytest.raises(NmcliError, match="unknown connection"):
            _adapter(fake).change_band("Barn-AP", WifiBand.GHZ_2_4)

    def test_the_active_network_is_refused_a_band_the_scan_doesnt_hear(
        self,
    ) -> None:
        """A fresh scan hears it only on 2.4 GHz: a 5 GHz lock would strand
        the base, so nothing is written and the link isn't touched."""
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{SAVED_SSID}\n")
        fake.set_response(SCAN, stdout=_scan_line(SAVED_SSID, 70, freq=2437))
        outcome = _adapter(fake).change_band(SAVED_SSID, WifiBand.GHZ_5)
        assert outcome == BandChangeRefused(
            name=SAVED_SSID,
            band=WifiBand.GHZ_5,
            reason=(
                "'SiteWiFi' isn't being heard on 5 GHz right now, so the base "
                "couldn't join it there. The band was not changed."
            ),
        )
        assert fake.calls == [ACTIVE_CONNECTIONS, SCAN]

    def test_the_active_network_heard_on_the_band_is_left_to_apply(
        self,
    ) -> None:
        """The check passes, but nothing is written yet: the reconnect is
        :meth:`apply_band_change`'s, so a caller can run it in the
        background."""
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{SAVED_SSID}\n")
        fake.set_response(SCAN, stdout=_scan_line(SAVED_SSID, 40, freq=5180))
        outcome = _adapter(fake).change_band(SAVED_SSID, WifiBand.GHZ_5)
        assert outcome == BandChangePending(name=SAVED_SSID, band=WifiBand.GHZ_5)
        assert fake.calls == [ACTIVE_CONNECTIONS, SCAN]

    def test_setting_the_active_network_back_to_auto_needs_no_scan(self) -> None:
        """Auto allows either band, so it can't rule the network out."""
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{SAVED_SSID}\n")
        outcome = _adapter(fake).change_band(SAVED_SSID, WifiBand.AUTO)
        assert outcome == BandChangePending(name=SAVED_SSID, band=WifiBand.AUTO)
        assert fake.calls == [ACTIVE_CONNECTIONS]

    def test_refuses_the_setup_ap_profile(self) -> None:
        """The setup hotspot is a separate radio role, out of scope."""
        fake = FakeNmcli()
        with pytest.raises(NmcliError):
            _adapter(fake).change_band(AP_SSID, WifiBand.GHZ_2_4)
        assert fake.calls == []


class TestApplyBandChange:
    """Applying a band to the active network: write it, then reconnect,
    since ``device reapply`` rejects band changes (#144)."""

    def test_a_reconnect_that_comes_up_is_applied(self) -> None:
        fake = FakeNmcli()
        fake.set_response(_band_of(SAVED_SSID), stdout="\n")
        outcome = _adapter(fake).apply_band_change(SAVED_SSID, WifiBand.GHZ_2_4)
        assert outcome == BandChangeApplied(name=SAVED_SSID, band=WifiBand.GHZ_2_4)
        assert fake.calls == [
            _band_of(SAVED_SSID),
            _set_band(SAVED_SSID, "bg"),
            SAVED_UP,
        ]

    def test_a_reconnect_that_fails_puts_the_old_band_back(self) -> None:
        """`con up` can't find it on 5 GHz: the old band (Auto here) is
        written back and the network brought up on it again."""
        fake = FakeNmcli()
        fake.set_response(_band_of(SAVED_SSID), stdout="\n")
        fake.queue_responses(
            SAVED_UP,
            FakeCompletedProcess(
                stderr="Error: The Wi-Fi network could not be found.", returncode=4
            ),
            FakeCompletedProcess(),
        )
        outcome = _adapter(fake).apply_band_change(SAVED_SSID, WifiBand.GHZ_5)
        assert outcome == BandChangeReverted(
            name=SAVED_SSID, band=WifiBand.AUTO, tried=WifiBand.GHZ_5
        )
        assert fake.calls == [
            _band_of(SAVED_SSID),
            _set_band(SAVED_SSID, "a"),
            SAVED_UP,
            _set_band(SAVED_SSID, ""),
            SAVED_UP,
        ]

    def test_a_revert_whose_reconnect_also_fails_is_still_reverted(self) -> None:
        """Nothing more is tried: the decision loop recovers the base."""
        fake = FakeNmcli()
        fake.set_response(_band_of(SAVED_SSID), stdout="bg\n")
        fake.set_response(SAVED_UP, stderr="Error: timed out.", returncode=4)
        outcome = _adapter(fake).apply_band_change(SAVED_SSID, WifiBand.GHZ_5)
        assert outcome == BandChangeReverted(
            name=SAVED_SSID, band=WifiBand.GHZ_2_4, tried=WifiBand.GHZ_5
        )
        assert fake.calls == [
            _band_of(SAVED_SSID),
            _set_band(SAVED_SSID, "a"),
            SAVED_UP,
            _set_band(SAVED_SSID, "bg"),
            SAVED_UP,
        ]

    def test_a_failed_write_raises_before_any_reconnect(self) -> None:
        fake = FakeNmcli()
        fake.set_response(
            _set_band(SAVED_SSID, "a"), stderr="Error: failed.", returncode=1
        )
        with pytest.raises(NmcliError):
            _adapter(fake).apply_band_change(SAVED_SSID, WifiBand.GHZ_5)
        assert SAVED_UP not in fake.calls


# ---------------------------------------------------------------------------
# activate_connection() / forget_connection() — issue #21
# ---------------------------------------------------------------------------


class TestActivateConnection:
    def test_issues_connection_up(self) -> None:
        fake = FakeNmcli()
        _adapter(fake).activate_connection(SAVED_SSID)
        assert fake.calls == [SAVED_UP]

    def test_raises_wifi_connect_error_on_failure(self) -> None:
        fake = FakeNmcli()
        fake.set_response(SAVED_UP, stderr="Error: no such connection.", returncode=10)
        with pytest.raises(WifiConnectError) as exc_info:
            _adapter(fake).activate_connection(SAVED_SSID)
        assert exc_info.value.ssid == SAVED_SSID


class TestForgetConnection:
    def test_issues_connection_delete(self) -> None:
        fake = FakeNmcli()
        _adapter(fake).forget_connection(SAVED_SSID)
        assert fake.calls == [["nmcli", "connection", "delete", "id", SAVED_SSID]]

    def test_raises_nmcli_error_on_failure(self) -> None:
        fake = FakeNmcli()
        delete_cmd = ["nmcli", "connection", "delete", "id", SAVED_SSID]
        fake.set_response(
            delete_cmd, stderr="Error: unknown connection.", returncode=10
        )
        with pytest.raises(NmcliError):
            _adapter(fake).forget_connection(SAVED_SSID)

    def test_forgets_the_currently_active_connection(self) -> None:
        """issue #21's AC explicitly covers this case: NetworkManager
        deactivates a connection before deleting it, so forgetting the
        active profile needs no special-casing here — it's the same
        `connection delete` call as any other saved profile."""
        fake = FakeNmcli()
        fake.set_response(ACTIVE_CONNECTIONS, stdout=f"{SAVED_SSID}\n")
        _adapter(fake).forget_connection(SAVED_SSID)
        assert fake.calls == [["nmcli", "connection", "delete", "id", SAVED_SSID]]

    def test_refuses_to_forget_the_setup_ap_profile(self) -> None:
        """Deleting the provisioning hotspot's own profile from the console
        would strand the fallback path issue #6 depends on."""
        fake = FakeNmcli()
        with pytest.raises(NmcliError):
            _adapter(fake).forget_connection(AP_SSID)
        assert fake.calls == []


# ---------------------------------------------------------------------------
# The default runner's timeouts
# ---------------------------------------------------------------------------


class TestDefaultRunnerTimeouts:
    """A failed `con up` takes about 25 s on a Pi before nmcli gives up
    (#144), so the default runner must wait out NetworkManager's own
    activation timeout (90 s) for it, or a band change could never see the
    failure it reverts on."""

    @staticmethod
    def _timeouts(monkeypatch: pytest.MonkeyPatch) -> list[float]:
        seen: list[float] = []

        def fake_run(args: list[str], **kwargs: Any) -> FakeCompletedProcess:
            seen.append(kwargs["timeout"])
            return FakeCompletedProcess()

        monkeypatch.setattr(
            "sp_rtk_base.services.net_provision.nmcli_adapter.subprocess.run",
            fake_run,
        )
        return seen

    def test_connection_up_waits_out_networkmanager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._timeouts(monkeypatch)
        NmcliAdapter(_config()).activate_connection(SAVED_SSID)
        assert len(seen) == 1
        assert seen[0] > 90

    def test_band_join_connection_up_by_uuid_waits_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #153's band-locked join brings the new profile up by uuid.
        seen = self._timeouts(monkeypatch)
        _run_nmcli(JOIN_UP)
        assert len(seen) == 1
        assert seen[0] > 90

    def test_other_commands_keep_the_short_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._timeouts(monkeypatch)
        NmcliAdapter(_config()).forget_connection(SAVED_SSID)
        assert seen == [10.0]
