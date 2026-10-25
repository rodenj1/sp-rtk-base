"""The nmcli adapter — state in, commands out (issue #8).

Reads the nmcli-observable facts :func:`~.decision.decide` needs
(uplink connectivity, whether the setup AP is active, presence and
visibility of a saved WiFi profile) and executes each
:class:`ProvisionAction` it returns. Contains no decision logic —
every branch here answers "what does nmcli currently say" or "which
nmcli command does this action need", never "what should happen
next".

The operator console (issue #21) is a second consumer of this same
class, deliberately: :meth:`NmcliAdapter.list_saved_connections`,
:meth:`NmcliAdapter.activate_connection` and
:meth:`NmcliAdapter.forget_connection` don't serve the decision
loop at all, but living here keeps there being exactly one nmcli
boundary in the codebase rather than a second, console-only one.

Two things this adapter deliberately does **not** own:

* ``seconds_since_boot`` / ``seconds_disconnected`` / ``seconds_in_ap``
  are durable wall-clock bookkeeping that must survive a restart of
  the provisioning service (issue #9) — a point-in-time nmcli query
  has no way to reconstruct that history, so callers supply these to
  :meth:`NmcliAdapter.read_state`.
* Consecutive-failure counting for a visible-but-unjoinable saved
  network (issue #25) — this adapter only raises
  :class:`WifiConnectError` when a connect attempt fails; deciding
  when to stop retrying is decision-core logic.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

from sp_rtk_base.models.net_provision_models import (
    BandChangeOutcome,
    BandChangeSaved,
    Connectivity,
    NetProvisionConfig,
    NetworkState,
    ProvisionAction,
    SavedWifiConnection,
    WifiBand,
    WifiNetwork,
)

NmcliRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

_NMCLI_TIMEOUT_SECONDS = 10.0
_WIRELESS_TYPE = "802-11-wireless"

# NetworkManager's loopback connection ("lo") is active on every host,
# always, independent of any real uplink — it is not a stable, fixed
# name the way _WIRELESS_TYPE is a fixed nmcli vocabulary term, but in
# practice NM always names it exactly this. Excluded from
# _active_connection_names() so "only the AP (and loopback) are active"
# is correctly recognized as "no other uplink" (issue #8's Rule 1)
# instead of loopback's permanent presence defeating that check.
_LOOPBACK_CONNECTION_NAME = "lo"


def _run_nmcli(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Default runner: invoke the real ``nmcli`` binary."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=_NMCLI_TIMEOUT_SECONDS,
        check=False,
    )


def _band_of_freq(freq: str) -> WifiBand | None:
    """The Wi-Fi band of one BSS from nmcli's FREQ column (``5180 MHz``).

    ``None`` for a frequency outside both bands (6 GHz, say) or one that
    doesn't parse; such a BSS still counts toward the overall signal.
    """
    try:
        mhz = int(freq.split()[0])
    except (IndexError, ValueError):
        return None
    return WifiBand.from_frequency(mhz)


def _added_connection_uuid(stdout: str) -> str | None:
    """The UUID in ``connection add``'s ``Connection 'X' (<uuid>) successfully
    added.``, or ``None`` if the output doesn't read that way."""
    match = re.search(r"\(([0-9a-fA-F-]{36})\) successfully added", stdout)
    return match.group(1) if match else None


class NmcliError(RuntimeError):
    """An nmcli invocation failed."""


class WifiConnectError(NmcliError):
    """Connecting to a WiFi network failed (e.g. wrong password).

    Raised for the connect step of ``STOP_AP_AND_CONNECT`` — the
    "surfaces connect failures distinctly from success" signal issue
    #25 counts consecutive occurrences of — and, for the console (issue
    #21), by :meth:`NmcliAdapter.connect_to_network` and
    :meth:`NmcliAdapter.activate_connection`.
    """

    def __init__(self, ssid: str, reason: str) -> None:
        self.ssid = ssid
        super().__init__(f"failed to connect to {ssid!r}: {reason}")


class NmcliAdapter:
    """Reads NetworkManager state and executes provisioning actions.

    Holds one piece of state beyond the config: whether a scan taken
    since the AP's current session began has seen the saved network
    (``saved_wifi_visible``'s non-sticky contract). Everything else is
    read fresh from nmcli on every call.
    """

    def __init__(
        self, config: NetProvisionConfig, runner: NmcliRunner = _run_nmcli
    ) -> None:
        self._config = config
        self._run = runner
        self._saved_wifi_seen = False
        self._last_scan: list[WifiNetwork] = []

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def read_state(
        self,
        *,
        seconds_since_boot: float,
        seconds_disconnected: float,
        seconds_in_ap: float,
    ) -> NetworkState:
        """Assemble a :class:`NetworkState` for :func:`decide`.

        Args:
            seconds_since_boot: Passed straight through to the result —
                the caller's durable uptime clock, not something this
                adapter can derive from a single nmcli query.
            seconds_disconnected: Passed straight through; see above.
            seconds_in_ap: Passed straight through; see above.

        Returns:
            The assembled state: nmcli-observed fields plus the
            caller-supplied elapsed times. ``saved_wifi_name`` lets the
            supervisor key its durable connect-failure count (issue
            #25) to *which* network without a second nmcli round trip
            to re-derive the same name this call already resolved.
        """
        active = self._active_connection_names()
        ap_active = self._config.ap_ssid in active
        saved_wifi_name = self._saved_wifi_connection_name()
        return NetworkState(
            uplink_connectivity=self._read_uplink_connectivity(active),
            seconds_since_boot=seconds_since_boot,
            seconds_disconnected=seconds_disconnected,
            ap_active=ap_active,
            seconds_in_ap=seconds_in_ap,
            saved_wifi_known=saved_wifi_name is not None,
            saved_wifi_visible=self._saved_wifi_seen,
            saved_wifi_name=saved_wifi_name,
        )

    def _active_connection_names(self) -> set[str]:
        result = self._run(
            ["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"]
        )
        return {
            line
            for line in result.stdout.splitlines()
            if line and line != _LOOPBACK_CONNECTION_NAME
        }

    def _has_other_uplink(self, active: set[str]) -> bool:
        """Whether any active connection besides the setup AP exists."""
        return bool(active - {self._config.ap_ssid})

    def _read_uplink_connectivity(self, active: set[str]) -> Connectivity:
        # Rule 1: a hotspot-only host has no uplink. NM reports `limited`
        # for a host whose only active connection is its own shared-mode
        # AP; passing that through would make the orchestrator tear down
        # the AP it just raised and re-raise it next tick.
        if not self._has_other_uplink(active):
            return Connectivity.NONE
        result = self._run(["nmcli", "-t", "networking", "connectivity", "check"])
        value = result.stdout.strip().lower()
        # Rule 2: `unknown` (connectivity checking disabled, common on Pi
        # images) means NM can't say — but a non-AP connection is active,
        # so treat it as a usable-but-unverified uplink rather than none.
        if value == "unknown":
            return Connectivity.LIMITED
        try:
            return Connectivity(value)
        except ValueError:
            return Connectivity.NONE

    def _saved_wifi_connection_name(self) -> str | None:
        result = self._run(["nmcli", "-t", "-f", "TYPE,NAME", "connection", "show"])
        for line in result.stdout.splitlines():
            if not line:
                continue
            # TYPE first: it never contains a colon, so splitting on the
            # first one leaves the NAME intact even if a connection name
            # does.
            conn_type, _, name = line.partition(":")
            if conn_type == _WIRELESS_TYPE and name != self._config.ap_ssid:
                return name
        return None

    def list_saved_connections(self) -> list[SavedWifiConnection]:
        """List saved WiFi profiles, for the console's Network page (issue #21).

        Excludes the setup AP's own profile — the console has no business
        listing, switching to, or forgetting the provisioning hotspot.

        Returns:
            One entry per saved WiFi profile, flagged with whether it is
            the currently active connection.
        """
        result = self._run(["nmcli", "-t", "-f", "TYPE,NAME", "connection", "show"])
        active = self._active_connection_names()
        connections: list[SavedWifiConnection] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            conn_type, _, name = line.partition(":")
            if conn_type != _WIRELESS_TYPE or name == self._config.ap_ssid:
                continue
            connections.append(
                SavedWifiConnection(
                    name=name, active=name in active, band=self._band_of(name)
                )
            )
        return connections

    def _band_of(self, name: str) -> WifiBand:
        """The Wi-Fi band saved in profile ``name``.

        Auto when it can't be read, so one odd profile doesn't hide the
        whole saved list: Auto is also what NetworkManager does with a
        profile that carries no lock.
        """
        result = self._run(
            ["nmcli", "-g", "802-11-wireless.band", "connection", "show", "id", name]
        )
        if result.returncode != 0:
            return WifiBand.AUTO
        try:
            return WifiBand.from_nm(result.stdout)
        except ValueError:
            return WifiBand.AUTO

    def scan_networks(self) -> list[WifiNetwork]:
        """Scan for nearby WiFi networks, for the WiFi-picker portal (issue #10).

        A single-radio Pi can only scan while it isn't itself acting as
        the AP, so callers are the AP lifecycle methods below, not the
        portal directly — the portal reads :meth:`latest_scan` instead.

        Returns:
            One entry per SSID, strongest signal first. A network seen
            on multiple BSSIDs (repeaters, band-steering) collapses to
            its single strongest reading.
        """
        result = self._run(
            [
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
        )
        best_by_ssid: dict[str, WifiNetwork] = {}
        for line in result.stdout.splitlines():
            if not line:
                continue
            # Split from the right: SIGNAL/SECURITY/FREQ never contain a
            # colon, but an SSID theoretically could, so anchoring on the
            # last three colons keeps an SSID with colons in it intact.
            parts = line.rsplit(":", 3)
            if len(parts) != 4:
                continue
            ssid, signal_str, security_raw, freq_raw = parts
            if not ssid:
                continue
            try:
                signal = int(signal_str)
            except ValueError:
                continue
            security = "" if security_raw == "--" else security_raw
            band = _band_of_freq(freq_raw)
            existing = best_by_ssid.get(ssid)
            if existing is None:
                existing = WifiNetwork(ssid=ssid, signal=signal, security=security)
                best_by_ssid[ssid] = existing
            elif signal > existing.signal:
                existing.signal = signal
                existing.security = security
            if band is not None and signal > existing.band_signals.get(band, -1):
                existing.band_signals[band] = signal
        return sorted(best_by_ssid.values(), key=lambda n: n.signal, reverse=True)

    def has_5ghz(self) -> bool | None:
        """Whether the Wi-Fi adapter has 5 GHz, from ``WIFI-PROPERTIES.5GHZ``.

        This is the hardware's capability, not whether 5 GHz is usable
        under the current regulatory country: a base with a wrong or
        unset WLAN country can say yes here and still never hear 5 GHz.

        Returns:
            ``True`` or ``False`` as nmcli reports it; ``None`` when it
            can't tell (nmcli says ``unknown``, there's no Wi-Fi device,
            or the query fails).
        """
        devices = self._run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"])
        if devices.returncode != 0:
            return None
        wifi_device = next(
            (
                device
                for device, _, kind in (
                    line.rpartition(":") for line in devices.stdout.splitlines()
                )
                if kind == "wifi" and device
            ),
            None,
        )
        if wifi_device is None:
            return None
        result = self._run(
            ["nmcli", "-g", "WIFI-PROPERTIES.5GHZ", "device", "show", wifi_device]
        )
        if result.returncode != 0:
            return None
        return {"yes": True, "no": False}.get(result.stdout.strip())

    def latest_scan(self) -> list[WifiNetwork]:
        """The scan cached by the most recent AP (re)start or rescan.

        Fed to the WiFi-picker portal (issue #10): the radio can't scan
        again while serving the AP an installer's phone is connected to,
        so this is a cache rather than a fresh read.
        """
        return list(self._last_scan)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def execute(self, action: ProvisionAction) -> None:
        """Run the nmcli invocation(s) for ``action``.

        Args:
            action: The action chosen by :func:`decide`.

        Raises:
            NmcliError: If bringing the AP up or down fails.
            WifiConnectError: If ``STOP_AP_AND_CONNECT``'s connect step
                fails.
        """
        if action is ProvisionAction.IDLE:
            return
        if action is ProvisionAction.START_AP:
            self._start_ap()
        elif action is ProvisionAction.RESCAN:
            self._rescan()
        elif action is ProvisionAction.STOP_AP_AND_CONNECT:
            self._stop_ap_and_connect()

    def _run_checked(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run an nmcli command, raising :class:`NmcliError` if it fails.

        Used for the mutating AP up/down invocations. The connect step
        gets its own :class:`WifiConnectError` instead of this, since
        callers need to identify *that* failure distinctly (issue #25).
        """
        result = self._run(args)
        if result.returncode != 0:
            reason = result.stderr.strip() or f"exit code {result.returncode}"
            raise NmcliError(f"`{' '.join(args)}` failed: {reason}")
        return result

    def _ap_up(self) -> None:
        self._run_checked(["nmcli", "connection", "up", "id", self._config.ap_ssid])

    def _ap_down(self) -> None:
        self._run_checked(["nmcli", "connection", "down", "id", self._config.ap_ssid])

    def _start_ap(self) -> None:
        # A fresh AP session invalidates any earlier scan: report
        # saved_wifi_visible = False until a scan taken during *this*
        # session says otherwise.
        self._saved_wifi_seen = False
        # The radio is free right up until the AP comes up — the one
        # moment a scan is cheap — so the portal (issue #10) has a
        # network list ready the instant an installer's phone associates,
        # rather than an empty list until the first RESCAN cycle.
        self._last_scan = self.scan_networks()
        self._ap_up()

    def _rescan(self) -> None:
        # Single radio: the AP has to be let go of before scanning is
        # even possible, then resumed so decide() sees it active again
        # next tick. This down/up is an implementation detail of taking
        # a scan, not a fresh AP session — it must not reset
        # saved_wifi_seen, since setting that flag from the scan below
        # is the entire point of this method.
        self._ap_down()
        saved_name = self._saved_wifi_connection_name()
        self._last_scan = self.scan_networks()
        if saved_name is not None and any(
            n.ssid == saved_name for n in self._last_scan
        ):
            self._saved_wifi_seen = True
        self._ap_up()

    def _stop_ap_and_connect(self) -> None:
        # decide() returns this action in two distinct situations: (a)
        # another interface (e.g. Ethernet) already has an uplink, in
        # which case there is nothing to connect — just drop the AP; or
        # (b) the saved WiFi was seen on the last scan and must be
        # actively joined. The action alone can't tell them apart, so
        # re-check nmcli directly.
        active = self._active_connection_names()
        had_other_uplink = self._has_other_uplink(active)
        if self._config.ap_ssid in active:
            self._ap_down()
        if had_other_uplink:
            return
        saved_name = self._saved_wifi_connection_name()
        if saved_name is None:
            return
        self.activate_connection(saved_name)

    def activate_connection(self, name: str) -> None:
        """Activate an already-saved connection profile (issue #21).

        Used by the console's "switch to a different saved network" flow.
        Unlike :meth:`connect_to_network`, this never creates a profile —
        it only (re)activates one nmcli already knows about.

        Raises:
            WifiConnectError: The connection failed to come up.
        """
        result = self._run(["nmcli", "connection", "up", "id", name])
        if result.returncode != 0:
            raise WifiConnectError(
                name, result.stderr.strip() or "nmcli connection up failed"
            )

    def forget_connection(self, name: str) -> None:
        """Delete a saved connection profile (issue #21).

        Works whether or not ``name`` is currently active — NetworkManager
        deactivates before deleting. Refuses to delete the setup AP's own
        profile: doing so from the console would strand the AP-fallback
        path issue #6 depends on.

        Raises:
            NmcliError: ``name`` is the setup AP profile, or the delete
                failed.
        """
        if name == self._config.ap_ssid:
            raise NmcliError(f"refusing to forget the setup AP connection {name!r}")
        self._run_checked(["nmcli", "connection", "delete", "id", name])

    def change_band(self, name: str, band: WifiBand) -> BandChangeOutcome:
        """Lock saved network ``name`` to ``band``, or set it back to Auto.

        For a network that isn't active the band is written to its
        profile straight away, with no reconnect and no scan check, and
        the outcome is Saved. Being in the profile, the band survives a
        reboot.

        Raises:
            NmcliError: ``name`` is the setup AP profile, ``name`` is the
                active network (its band can't be changed yet), or the
                write failed.
        """
        if name == self._config.ap_ssid:
            raise NmcliError(
                f"refusing to change the band of the setup AP connection {name!r}"
            )
        if name in self._active_connection_names():
            raise NmcliError(
                f"changing the band of the active network {name!r} isn't supported"
            )
        self._run_checked(
            [
                "nmcli",
                "connection",
                "modify",
                "id",
                name,
                "802-11-wireless.band",
                band.nm_value,
            ]
        )
        return BandChangeSaved(name=name, band=band)

    def connect_to_network(
        self,
        ssid: str,
        password: str,
        *,
        hidden: bool = False,
        band: WifiBand = WifiBand.AUTO,
    ) -> None:
        """Connect to an installer-submitted SSID+password (issue #10).

        Unlike :meth:`_stop_ap_and_connect`, which reconnects an
        already-*saved* profile chosen by :func:`decide`, this creates
        and activates a profile for whatever network the installer typed
        into the WiFi-picker portal. Deliberately does not call
        :meth:`_ap_down` first: NetworkManager tears the AP down itself
        as part of activating the new connection — a single-radio device
        can only run one — so a caller-issued teardown would just race it.

        The password is briefly visible in this process's argv (e.g. via
        `ps`/`/proc`) — an accepted tradeoff, since `nmcli device wifi
        connect` has no other way to pass a PSK on first connect to an
        unsaved network. Contrast :meth:`_stop_ap_and_connect`, which
        never sees a plaintext secret because it only reactivates an
        already-saved profile by name.

        Args:
            hidden: The SSID is not broadcast, so nmcli must be told to
                attempt an association blind rather than rely on a scan
                result (issue #21's console hidden-SSID entry).
            band: The Wi-Fi band to lock the new profile to from its
                very first join (issue #153). Auto keeps the plain
                ``nmcli device wifi connect``; see
                :meth:`_connect_with_band` for the rest.

        Raises:
            WifiConnectError: The connect failed, e.g. a wrong password.
        """
        if band is not WifiBand.AUTO:
            self._connect_with_band(ssid, password, hidden=hidden, band=band)
            return
        args = ["nmcli", "device", "wifi", "connect", ssid, "password", password]
        if hidden:
            args += ["hidden", "yes"]
        result = self._run(args)
        if result.returncode != 0:
            raise WifiConnectError(
                ssid, result.stderr.strip() or "nmcli device wifi connect failed"
            )

    def _connect_with_band(
        self, ssid: str, password: str, *, hidden: bool, band: WifiBand
    ) -> None:
        """Create a profile locked to ``band``, then bring it up.

        ``nmcli device wifi connect`` takes no band, and setting it with
        ``connection modify`` afterwards would make the first join an
        unlocked one, so the profile is built with ``connection add``.
        That means naming the security ourselves: a password means
        WPA-PSK (which also joins WPA2/WPA3 transition networks), none
        means open. A failed join deletes the new profile again, the
        way ``nmcli device wifi connect`` does, so a failed join leaves
        nothing the decision loop would mistake for the saved network.
        The profile is brought up and deleted by the UUID ``connection
        add`` reports, so an older profile of the same name is never
        touched; failing that, by name.
        """
        add = [
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
            band.nm_value,
        ]
        if hidden:
            add += ["802-11-wireless.hidden", "yes"]
        if password:
            add += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        added = self._run(add)
        if added.returncode != 0:
            raise WifiConnectError(
                ssid, added.stderr.strip() or "nmcli connection add failed"
            )
        uuid = _added_connection_uuid(added.stdout)
        selector = ["uuid", uuid] if uuid is not None else ["id", ssid]
        result = self._run(["nmcli", "connection", "up", *selector])
        if result.returncode != 0:
            self._run(["nmcli", "connection", "delete", *selector])
            raise WifiConnectError(
                ssid, result.stderr.strip() or "nmcli connection up failed"
            )
