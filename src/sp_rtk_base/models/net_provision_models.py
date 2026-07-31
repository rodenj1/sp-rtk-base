"""Models for headless field network provisioning.

Data structures for the Ethernet-first / WiFi-AP-fallback onboarding
described in issue #6: the observable state read from NetworkManager,
the actions the orchestrator can take, and the tunable knobs.

Nothing here touches NetworkManager, a clock, or a subprocess — these
are plain values so the decision logic in
:mod:`sp_rtk_base.services.net_provision.decision` stays a pure
function of observable state.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Observable network state
# ---------------------------------------------------------------------------


class Connectivity(str, enum.Enum):
    """Whether the device currently has a usable network.

    Mirrors the meaningful values of ``nmcli networking connectivity``.
    Anything other than :attr:`NONE` counts as "has a network" — an RTK
    base on a LAN-only site with an on-premise NTRIP caster is fully
    operational without internet, so flipping such a device into AP
    mode would take a working install offline.

    NetworkManager also reports ``unknown`` when its connectivity
    checking is disabled (common on Pi images).  Mapping that to one of
    these members is the nmcli adapter's job (issue #8): a device with
    an active connection should be reported as :attr:`LIMITED`, not
    :attr:`NONE`.
    """

    NONE = "none"
    PORTAL = "portal"
    LIMITED = "limited"
    FULL = "full"


class NetworkState(BaseModel):
    """Observable network state at one point in time.

    Produced by the nmcli adapter, consumed by :func:`decide`.
    """

    connectivity: Connectivity = Field(
        description="Whether a usable network is present",
    )
    seconds_since_boot: float = Field(
        ge=0.0,
        description="Uptime; gates the boot-wait grace period",
    )
    seconds_disconnected: float = Field(
        ge=0.0,
        description=(
            "How long the device has been without a network. 0 while "
            "connected. Read against the fallback window on a device "
            "that already has a saved WiFi profile."
        ),
    )
    ap_active: bool = Field(
        default=False,
        description="True while the setup access point is up",
    )
    seconds_in_ap: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "How long the current AP session has been up. Resets each "
            "time the AP is (re)started, so it also measures time since "
            "the last rescan."
        ),
    )
    saved_wifi_known: bool = Field(
        default=False,
        description=(
            "A saved WiFi profile exists, i.e. the device has been "
            "provisioned at least once."
        ),
    )
    saved_wifi_visible: bool = Field(
        default=False,
        description=(
            "The saved WiFi network was seen in the most recent scan. "
            "Only ever True on the tick after a RESCAN, because a "
            "single-radio Pi cannot scan while serving the AP."
        ),
    )

    @property
    def has_network(self) -> bool:
        """True when a usable network is present."""
        return self.connectivity is not Connectivity.NONE


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class ProvisionAction(str, enum.Enum):
    """What the orchestrator should do next.

    Executed by the nmcli adapter (issue #8); :func:`decide` only
    chooses between them.
    """

    IDLE = "idle"
    START_AP = "start_ap"
    STOP_AP_AND_CONNECT = "stop_ap_and_connect"
    RESCAN = "rescan"


# ---------------------------------------------------------------------------
# Configuration knobs
# ---------------------------------------------------------------------------

DEFAULT_AP_SSID = "sp-rtk-base-setup"


class NetProvisionConfig(BaseModel):
    """Tunable knobs for headless network provisioning.

    Persisted separately from the application's ``config.yaml``: the
    provisioning service runs independently of ``sp-rtk-base.service``
    (issue #6, story 17) and must not read a file the web UI rewrites.
    The installer writes these once, only if absent (issue #11).
    """

    ap_ssid: str = Field(
        default=DEFAULT_AP_SSID,
        min_length=1,
        max_length=32,
        description="Fixed setup-AP SSID, printable on a sticker (802.11 caps at 32)",
    )
    ap_password: str = Field(
        min_length=8,
        max_length=63,
        description=(
            "Setup-AP WPA2 passphrase (WPA2-PSK requires 8-63 chars). "
            "Required — there is deliberately no default, so no unit "
            "ever ships with a hotspot password baked into source."
        ),
    )
    boot_wait_seconds: float = Field(
        default=45.0,
        gt=0.0,
        description=(
            "Grace period after boot before an unprovisioned device "
            "opens the setup AP. Must exceed worst-case DHCP on a wired "
            "site so a slow Ethernet link is not cut off prematurely."
        ),
    )
    fallback_window_seconds: float = Field(
        default=300.0,
        gt=0.0,
        description=(
            "How long a provisioned device tolerates having no network "
            "before re-opening the setup AP. Trades transient-outage "
            "tolerance against reconfiguration responsiveness."
        ),
    )
    rescan_interval_seconds: float = Field(
        default=120.0,
        gt=0.0,
        description=(
            "How long to serve the AP before dropping it to look for "
            "the saved WiFi network."
        ),
    )
    poll_interval_seconds: float = Field(
        default=5.0,
        gt=0.0,
        description="How often the supervisor loop samples state (issue #9)",
    )
