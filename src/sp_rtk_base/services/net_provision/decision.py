"""The provisioning decision core — a pure function of observable state.

No I/O, no nmcli, no clock.  Everything :func:`decide` needs arrives in
its arguments and everything it produces is a
:class:`~sp_rtk_base.models.net_provision_models.ProvisionAction` for the
adapter to execute.  That is what makes the whole AP-versus-client
behavior testable without a Raspberry Pi (issue #7).
"""

from __future__ import annotations

from sp_rtk_base.models.net_provision_models import (
    NetProvisionConfig,
    NetworkState,
    ProvisionAction,
)


def decide(state: NetworkState, config: NetProvisionConfig) -> ProvisionAction:
    """Choose the next provisioning action.

    Args:
        state: Observable network state, as read by the nmcli adapter.
        config: Threshold knobs.

    Returns:
        The action the adapter should execute.
    """
    if state.ap_active:
        # Connectivity while the AP is up means another interface came
        # good — an installer running a cable mid-setup — so stop
        # hosting a setup hotspot nobody needs.
        if state.has_network:
            return ProvisionAction.STOP_AP_AND_CONNECT
        # The last rescan saw the site network, so stop guessing and go
        # back to being a client.
        if state.saved_wifi_known and state.saved_wifi_visible:
            return ProvisionAction.STOP_AP_AND_CONNECT
        # Single radio: the AP has to be let go of before the saved
        # network can even be looked for.
        if state.seconds_in_ap >= config.rescan_interval_seconds:
            return ProvisionAction.RESCAN
        return ProvisionAction.IDLE

    if state.has_network:
        return ProvisionAction.IDLE

    # Boot-wait applies unconditionally: never open the AP while a
    # slow-DHCP Ethernet link might still be coming up.
    if state.seconds_since_boot < config.boot_wait_seconds:
        return ProvisionAction.IDLE

    # A saved profile means this unit worked here once, so the outage is
    # more likely transient than a genuine network change: let
    # NetworkManager keep retrying for the longer fallback window before
    # taking the radio away from it.  An unprovisioned unit has nothing
    # to retry, so boot-wait alone governs and the installer sees the
    # setup AP within a minute of power-on.
    if state.saved_wifi_known and (
        state.seconds_disconnected < config.fallback_window_seconds
    ):
        return ProvisionAction.IDLE

    return ProvisionAction.START_AP
