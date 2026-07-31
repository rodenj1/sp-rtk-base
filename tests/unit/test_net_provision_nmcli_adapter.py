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
    Connectivity,
    NetProvisionConfig,
    NetworkState,
    ProvisionAction,
    WifiNetwork,
)
from sp_rtk_base.services.net_provision import (
    NmcliAdapter,
    NmcliError,
    WifiConnectError,
)

AP_SSID = "sp-rtk-base-setup"
SAVED_SSID = "SiteWiFi"

ACTIVE_CONNECTIONS = ["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"]
CONNECTIVITY_CHECK = ["nmcli", "-t", "networking", "connectivity", "check"]
ALL_CONNECTIONS = ["nmcli", "-t", "-f", "TYPE,NAME", "connection", "show"]
SCAN = [
    "nmcli",
    "-t",
    "-f",
    "SSID,SIGNAL,SECURITY",
    "device",
    "wifi",
    "list",
    "--rescan",
    "yes",
]
AP_UP = ["nmcli", "connection", "up", "id", AP_SSID]
AP_DOWN = ["nmcli", "connection", "down", "id", AP_SSID]
SAVED_UP = ["nmcli", "connection", "up", "id", SAVED_SSID]


def _scan_line(ssid: str, signal: int = 50, security: str = "WPA2") -> str:
    """One `nmcli -t -f SSID,SIGNAL,SECURITY device wifi list` line."""
    return f"{ssid}:{signal}:{security}"


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
        self._responses: dict[tuple[str, ...], FakeCompletedProcess] = {}

    def set_response(
        self,
        args: list[str],
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self._responses[tuple(args)] = FakeCompletedProcess(stdout, stderr, returncode)

    def __call__(self, args: list[str]) -> FakeCompletedProcess:
        self.calls.append(args)
        return self._responses.get(tuple(args), FakeCompletedProcess())


def _config(**overrides: Any) -> NetProvisionConfig:
    values: dict[str, Any] = {"ap_ssid": AP_SSID, "ap_password": "sticker-secret"}
    values.update(overrides)
    return NetProvisionConfig(**values)


def _adapter(fake: FakeNmcli, **config_overrides: Any) -> NmcliAdapter:
    return NmcliAdapter(_config(**config_overrides), runner=fake)


def _read_state(adapter: NmcliAdapter) -> NetworkState:
    return adapter.read_state(
        seconds_since_boot=1000.0,
        seconds_disconnected=0.0,
        seconds_in_ap=0.0,
    )


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
            WifiNetwork(ssid=SAVED_SSID, signal=80, security="WPA2")
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
        active again on the next tick."""
        fake = FakeNmcli()
        fake.set_response(ALL_CONNECTIONS, stdout=f"802-11-wireless:{SAVED_SSID}\n")
        fake.set_response(SCAN, stdout=f"{_scan_line(SAVED_SSID)}\n")
        adapter = _adapter(fake)
        adapter.execute(ProvisionAction.RESCAN)
        assert fake.calls == [AP_DOWN, ALL_CONNECTIONS, SCAN, AP_UP]

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
            WifiNetwork(ssid="OtherNetwork", signal=40, security="WPA2")
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
        assert networks == [WifiNetwork(ssid=SAVED_SSID, signal=64, security="WPA2")]

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
        assert networks == [WifiNetwork(ssid=SAVED_SSID, signal=75, security="WPA2")]

    def test_ignores_blank_lines(self) -> None:
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout=f"\n{_scan_line(SAVED_SSID)}\n\n")
        networks = _adapter(fake).scan_networks()
        assert len(networks) == 1

    def test_empty_scan_yields_no_networks(self) -> None:
        fake = FakeNmcli()
        fake.set_response(SCAN, stdout="")
        assert _adapter(fake).scan_networks() == []


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
