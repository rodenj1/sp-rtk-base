"""Headless field network provisioning (Ethernet-first, WiFi-AP fallback).

NetworkManager does the real networking — Ethernet preference, WiFi
client reconnection, AP creation, DHCP/DNS, connection persistence.
This package holds only the thin sp-rtk-base layer on top: the pure
decision core that chooses *when* to flip between client and AP mode
(:func:`decide`), the nmcli adapter that reads state and executes the
chosen action (:class:`NmcliAdapter`), and — as it lands — the
supervisor loop that ties them together on a timer.
"""

from __future__ import annotations

from sp_rtk_base.services.net_provision.decision import decide
from sp_rtk_base.services.net_provision.nmcli_adapter import (
    NmcliAdapter,
    NmcliError,
    WifiConnectError,
)

__all__ = ["NmcliAdapter", "NmcliError", "WifiConnectError", "decide"]
