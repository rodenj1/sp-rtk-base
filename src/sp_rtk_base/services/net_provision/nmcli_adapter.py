"""The nmcli adapter — state in, commands out (issue #8).

Reads the nmcli-observable facts :func:`~.decision.decide` needs
(uplink connectivity, whether the setup AP is active, presence and
visibility of a saved WiFi profile) and executes each
:class:`ProvisionAction` it returns. Contains no decision logic —
every branch here answers "what does nmcli currently say" or "which
nmcli command does this action need", never "what should happen
next".

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

import subprocess
from collections.abc import Callable

from sp_rtk_base.models.net_provision_models import (
    Connectivity,
    NetProvisionConfig,
    NetworkState,
    ProvisionAction,
)

NmcliRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

_NMCLI_TIMEOUT_SECONDS = 10.0
_WIRELESS_TYPE = "802-11-wireless"


def _run_nmcli(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Default runner: invoke the real ``nmcli`` binary."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=_NMCLI_TIMEOUT_SECONDS,
        check=False,
    )


class NmcliError(RuntimeError):
    """An nmcli invocation failed."""


class WifiConnectError(NmcliError):
    """Connecting to the saved WiFi network failed (e.g. wrong password).

    Raised only for the connect step of ``STOP_AP_AND_CONNECT`` — this
    is the "surfaces connect failures distinctly from success" signal
    issue #25 will later count consecutive occurrences of.
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
            caller-supplied elapsed times.
        """
        active = self._active_connection_names()
        ap_active = self._config.ap_ssid in active
        return NetworkState(
            uplink_connectivity=self._read_uplink_connectivity(active),
            seconds_since_boot=seconds_since_boot,
            seconds_disconnected=seconds_disconnected,
            ap_active=ap_active,
            seconds_in_ap=seconds_in_ap,
            saved_wifi_known=self._saved_wifi_connection_name() is not None,
            saved_wifi_visible=self._saved_wifi_seen,
        )

    def _active_connection_names(self) -> set[str]:
        result = self._run(
            ["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"]
        )
        return {line for line in result.stdout.splitlines() if line}

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

    def _scan_ssids(self) -> set[str]:
        result = self._run(
            ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "yes"]
        )
        return {line for line in result.stdout.splitlines() if line}

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
        if saved_name is not None and saved_name in self._scan_ssids():
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
        result = self._run(["nmcli", "connection", "up", "id", saved_name])
        if result.returncode != 0:
            raise WifiConnectError(
                saved_name, result.stderr.strip() or "nmcli connection up failed"
            )
