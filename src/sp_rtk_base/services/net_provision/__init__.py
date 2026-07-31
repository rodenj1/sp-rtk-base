"""Headless field network provisioning (Ethernet-first, WiFi-AP fallback).

NetworkManager does the real networking — Ethernet preference, WiFi
client reconnection, AP creation, DHCP/DNS, connection persistence.
This package holds only the thin sp-rtk-base layer on top: the pure
decision core that chooses *when* to flip between client and AP mode
(:func:`decide`), and — as they land — the nmcli adapter and supervisor
loop that read state and execute the chosen action.
"""

from __future__ import annotations

from sp_rtk_base.services.net_provision.decision import decide

__all__ = ["decide"]
