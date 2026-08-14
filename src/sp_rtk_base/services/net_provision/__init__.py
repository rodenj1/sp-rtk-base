"""Headless field network provisioning (Ethernet-first, WiFi-AP fallback).

NetworkManager does the real networking — Ethernet preference, WiFi
client reconnection, AP creation, DHCP/DNS, connection persistence.
This package holds only the thin sp-rtk-base layer on top: the pure
decision core that chooses *when* to flip between client and AP mode
(:func:`decide`), the nmcli adapter that reads state and executes the
chosen action (:class:`NmcliAdapter`), the durable clock store that
survives a service restart (:class:`ProvisioningStateStore`), the
strict config loader (:func:`load_net_provision_config`), the
supervisor loop that ties them all together on a timer
(:func:`run_forever`), and the AP-mode WiFi-picker captive portal
(:class:`Portal`) that lets an installer choose the site network.
"""

from __future__ import annotations

from sp_rtk_base.services.net_provision.config_loader import (
    NetProvisionConfigError,
    load_net_provision_config,
)
from sp_rtk_base.services.net_provision.decision import decide
from sp_rtk_base.services.net_provision.nmcli_adapter import (
    NmcliAdapter,
    NmcliError,
    WifiConnectError,
)
from sp_rtk_base.services.net_provision.portal import Portal
from sp_rtk_base.services.net_provision.state_store import (
    ProvisioningClockState,
    ProvisioningStateStore,
)
from sp_rtk_base.services.net_provision.supervisor import run_forever, tick

__all__ = [
    "NetProvisionConfigError",
    "NmcliAdapter",
    "NmcliError",
    "Portal",
    "ProvisioningClockState",
    "ProvisioningStateStore",
    "WifiConnectError",
    "decide",
    "load_net_provision_config",
    "run_forever",
    "tick",
]
