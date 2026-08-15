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
    """State of the device's *uplink* — its route off the box.

    Mirrors the meaningful values of ``nmcli networking connectivity``.
    Anything other than :attr:`NONE` counts as a usable uplink — an RTK
    base on a LAN-only site with an on-premise NTRIP caster is fully
    operational without internet, so flipping such a device into AP
    mode would take a working install offline.

    Two mapping rules bind the nmcli adapter (issue #8), because the
    decision core cannot see interfaces:

    1. **Exclude the setup AP's own connection.**  A NetworkManager
       ``shared``-mode hotspot is itself an active connection, and NM
       will happily report ``limited`` for a host whose only connection
       is that hotspot.  Passing that through as an uplink would make
       the orchestrator tear down the AP it just raised, re-raise it on
       the next tick, and flap the hotspot faster than an installer's
       phone can associate.  A hotspot-only host has **no** uplink:
       report :attr:`NONE`.
    2. **Map NM's ``unknown``.**  NM reports ``unknown`` when its
       connectivity checking is disabled (common on Pi images); report
       :attr:`LIMITED` when some non-AP connection is active, otherwise
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

    uplink_connectivity: Connectivity = Field(
        description=(
            "State of the uplink, excluding the setup AP's own "
            "connection (see Connectivity for the adapter's rules)"
        ),
    )
    seconds_since_boot: float = Field(
        ge=0.0,
        description="Uptime; gates the boot-wait grace period",
    )
    seconds_disconnected: float = Field(
        ge=0.0,
        description=(
            "How long the device has been without an uplink. 0 while "
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
            "A single-radio Pi cannot scan while serving the AP, so the "
            "adapter must report False whenever the AP is (re)started "
            "and only report True for a scan newer than that. A stale "
            "True would retry a failing network every tick and leave no "
            "AP window to reconfigure through."
        ),
    )
    saved_wifi_name: str | None = Field(
        default=None,
        description=(
            "The saved WiFi profile's name, if any — the same name "
            "saved_wifi_known/saved_wifi_visible describe. Bookkeeping "
            "only: decide() never reads this field, it exists so a "
            "caller (issue #25's supervisor) can key its durable "
            "connect-failure count to *which* network without a second "
            "nmcli round trip alongside this one."
        ),
    )
    consecutive_connect_failures: int = Field(
        default=0,
        ge=0,
        description=(
            "Consecutive failed STOP_AP_AND_CONNECT attempts against the "
            "currently-saved WiFi profile (issue #25). Reset to 0 by the "
            "caller the moment a connect succeeds or the saved profile "
            "changes — a visible-but-unjoinable network (wrong/changed "
            "password) would otherwise be retried on every rescan forever."
        ),
    )
    seconds_since_last_connect_failure: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "How long ago the last consecutive connect failure happened. "
            "Only meaningful once consecutive_connect_failures has reached "
            "the configured threshold; read against the suppression "
            "window so backoff expires instead of pinning the device in "
            "AP mode forever over a transient association failure."
        ),
    )
    consecutive_uplink_ticks: int = Field(
        default=0,
        ge=0,
        description=(
            "Consecutive polls in a row that have observed has_uplink "
            "True (issue #33). A single noisy nmcli connectivity read "
            "must not be enough to tear down an AP session someone may "
            "be mid-join on; read against uplink_confirm_ticks so the "
            "signal has to hold steady before decide() acts on it."
        ),
    )

    @property
    def has_uplink(self) -> bool:
        """True when a usable route off the device exists."""
        return self.uplink_connectivity is not Connectivity.NONE


# ---------------------------------------------------------------------------
# WiFi scan results
# ---------------------------------------------------------------------------


class WifiNetwork(BaseModel):
    """One network from an nmcli scan, as rendered by the WiFi-picker portal."""

    ssid: str = Field(description="Network name")
    signal: int = Field(ge=0, le=100, description="Signal strength percent")
    security: str = Field(
        description=(
            "Security type (e.g. 'WPA2'), or empty for an open network — "
            "the adapter normalizes nmcli's '--' column to ''."
        )
    )
    in_range: bool = Field(
        default=True,
        description=(
            "Whether this network was detected by the scan that produced "
            "this entry. Always True for a fresh nmcli scan (issue #21) — "
            "the field exists so console API responses are self-describing "
            "rather than relying on callers to know that invariant."
        ),
    )


# ---------------------------------------------------------------------------
# Console network operations (issue #21)
# ---------------------------------------------------------------------------


class SavedWifiConnection(BaseModel):
    """One saved WiFi profile NetworkManager knows about.

    Excludes the setup AP's own profile — the console has no business
    listing, switching to, or forgetting the provisioning hotspot.
    """

    name: str = Field(
        description=(
            "The nmcli connection id, which also serves as the SSID under "
            "this codebase's existing convention (see "
            "NmcliAdapter._saved_wifi_connection_name)."
        )
    )
    active: bool = Field(description="True if this is the currently active connection")


class LinkType(str, enum.Enum):
    """The kind of link a device's active connection runs over."""

    WIRED = "wired"
    WIFI = "wifi"


class ActiveLink(BaseModel):
    """The device's current non-AP network link, for the console status view."""

    link_type: LinkType = Field(description="Wired or WiFi")
    name: str = Field(description="Connection name (the SSID, for WiFi)")
    ip_address: str | None = Field(
        default=None, description="IPv4 address without the CIDR suffix, if assigned"
    )
    signal: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Signal strength percent; None for a wired link",
    )


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
        default=10.0,
        gt=0.0,
        description=(
            "How often the supervisor loop re-reads state and calls "
            "decide(). Must be finer-grained than the other thresholds "
            "or the loop can sleep straight through them."
        ),
    )
    max_connect_failures: int = Field(
        default=3,
        ge=1,
        description=(
            "Consecutive failed connect attempts against the same saved "
            "WiFi profile before decide() stops retrying it and holds the "
            "AP up instead (issue #25) — protects a wrong/changed "
            "password from being retried, radio-stealing, on every "
            "rescan forever."
        ),
    )
    failure_suppression_seconds: float = Field(
        default=300.0,
        gt=0.0,
        description=(
            "How long to hold off retrying a saved WiFi profile once "
            "max_connect_failures is reached, before trying it again. "
            "Bounds the backoff so a genuinely transient association "
            "failure doesn't pin the device in AP mode forever."
        ),
    )
    uplink_confirm_ticks: int = Field(
        default=2,
        ge=1,
        description=(
            "Consecutive polls has_uplink must read True before decide() "
            "tears down an active AP session over it (issue #33). A "
            "single noisy/stale nmcli connectivity read must not be "
            "enough to kill an AP a phone may be mid-join on."
        ),
    )
    ap_gateway_ip: str = Field(
        default="10.42.0.1",
        min_length=1,
        description=(
            "The setup AP's own address — NetworkManager's `shared`-mode "
            "hotspot default is 10.42.0.1/24. NM's own shared-mode dnsmasq "
            "is configured (issue #34, install-time dnsmasq-shared.d "
            "drop-in) to answer every DNS lookup with this address, and it "
            "is the documented manual-URL fallback if the captive-portal "
            "prompt doesn't auto-pop."
        ),
    )
    portal_http_port: int = Field(
        default=80,
        ge=0,
        le=65535,
        description=(
            "Port the WiFi-picker portal listens on. OS captive-portal "
            "probes use plain http:// with no port, so this must be 80 "
            "in production. 0 (bind an OS-assigned ephemeral port) is "
            "allowed for tests."
        ),
    )
