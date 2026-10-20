"""Network link health models: the raw host reading and the snapshot.

A **Network link** is one of the base station's own physical Ethernet or
Wi-Fi interfaces and its local connection; the **Uplink** is where its
outbound IPv4 traffic actually leaves.  See CONTEXT.md, which also names
the three reasons a reading has no current value: **Not supported**,
**Disconnected** and **No reading**.

Two halves:

* :class:`HostReading` is what the Network link sampler reads from the
  host each sample, one field per source.  A source whose read failed is
  ``None``.  It is also the JSON the e2e fake reader serves.
* :class:`NetworkLinkSnapshot` is what the Network link monitor makes of
  the readings so far: what the dashboard (and metrics) show.
"""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

from sp_rtk_base.models.level_models import Level
from sp_rtk_base.uplink import Uplink

LinkKind = Literal["ethernet", "wifi"]
LinkState = Literal["Connected", "Disconnected", "No reading"]
# A Wi-Fi interface's nl80211 interface type, as far as Network links care.
WifiMode = Literal["station", "ap", "p2p", "monitor", "other"]

T = TypeVar("T")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Raw readings (the sampler's sources)
# ---------------------------------------------------------------------------


class Counters(_Frozen):
    """An interface's kernel counter totals (sysfs ``statistics/``)."""

    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_drops: int = 0
    tx_drops: int = 0


class InterfaceReading(_Frozen):
    """One Network link interface as sysfs shows it.

    A field sysfs didn't give (the file raised ``EINVAL``, or held a
    "don't know" such as ``speed`` -1) is ``None``.  ``carrier`` is only
    read for Ethernet: on Wi-Fi it doesn't follow the association.
    """

    name: str
    kind: LinkKind
    operstate: str | None = None
    carrier: bool | None = None
    speed_mbps: int | None = None
    full_duplex: bool | None = None
    counters: Counters = Counters()


class AddressReading(_Frozen):
    """One IPv4 or IPv6 address on an interface."""

    interface: str
    address: str
    prefixlen: int


class EgressReading(_Frozen):
    """The Uplink probe's result: the source address the kernel picked for
    traffic to a public IPv4 address, ``None`` when it's unreachable."""

    source: str | None


class StationReading(_Frozen):
    """The access point a Wi-Fi interface is associated with, as nl80211
    reports it.  A value the driver didn't give is ``None``."""

    bssid: str
    signal_dbm: int | None = None
    tx_bitrate_bps: int | None = None
    rx_bitrate_bps: int | None = None
    # Resets with each association.
    tx_failed: int | None = None


class WifiReading(_Frozen):
    """One Wi-Fi interface as nl80211 shows it.

    ``frequency_mhz`` hops while the interface scans, so only an
    associated reading's is meaningful.
    """

    interface: str
    mode: WifiMode = "station"
    # The radio (nl80211 wiphy) the interface is on.
    wiphy: int | None = None
    frequency_mhz: int | None = None
    ssid: str | None = None
    # None: not associated.
    station: StationReading | None = None


class HostReading(_Frozen):
    """One sample of every source.  ``None`` is a source whose read failed.

    ``wifi`` has a reading for each Wi-Fi interface nl80211 answered for:
    one it failed for is missing (No reading for that interface only).
    """

    interfaces: tuple[InterfaceReading, ...] | None = None
    addresses: tuple[AddressReading, ...] | None = None
    egress: EgressReading | None = None
    wifi: tuple[WifiReading, ...] | None = None


# ---------------------------------------------------------------------------
# The Network link snapshot
# ---------------------------------------------------------------------------


class Supported(_Frozen, Generic[T]):
    """A reading this link supports.  ``value`` is ``None`` while it has no
    current value (Disconnected or No reading).  A Not supported reading
    isn't a ``Supported`` at all: its field is ``None``."""

    value: T | None = None


class Throughput(_Frozen):
    """Bytes per second over the last two samples."""

    rx_bytes_per_second: float
    tx_bytes_per_second: float


class EthernetDetails(_Frozen):
    """Ethernet-only readings.  ``None`` fields are Not supported."""

    speed_mbps: Supported[int] | None = None
    full_duplex: Supported[bool] | None = None


class WifiDetails(_Frozen):
    """Wi-Fi-only readings, from nl80211.  ``None`` fields are Not supported.

    Everything but ``tx_failed`` has a value only while associated and
    nl80211 reads.  ``tx_failed`` is a counter total, so like the other
    counters it keeps its last value through Disconnected and No reading.
    """

    # Why the Wi-Fi details have no value while the rest of the link
    # reads (nl80211 failed); None otherwise.
    no_reading_reason: str | None = None
    ssid: Supported[str] | None = None
    bssid: Supported[str] | None = None
    frequency_mhz: Supported[int] | None = None
    tx_bitrate_bps: Supported[int] | None = None
    rx_bitrate_bps: Supported[int] | None = None
    # The latest reading, in dBm: what Wi-Fi link strength is judged from.
    signal_dbm: Supported[int] | None = None
    # Wi-Fi link strength: the verdict on the lower median of the last
    # 10 s of dBm readings, in station mode only.  Supported with the dBm
    # reading; it has a value only while ``signal_dbm`` does.
    link_strength: Supported[Level] | None = None
    tx_failed: Supported[int] | None = None


class NetworkLink(_Frozen):
    """One Network link as the snapshot shows it."""

    name: str
    kind: LinkKind
    state: LinkState
    # Why it's Disconnected or No reading, for the operator; None when
    # Connected.
    reason: str | None = None
    uplink: bool = False
    # Ethernet only.
    ethernet: EthernetDetails | None = None
    # Wi-Fi only.
    wifi: WifiDetails | None = None
    ipv4: str | None = None
    ipv6: str | None = None
    # None until there are two samples, and during No reading.
    throughput: Throughput | None = None
    # Kept through No reading: totals never go blank.
    counters: Counters = Counters()
    # Up/down transitions the monitor has seen since the app started.
    link_changes: int = 0


class NetworkLinkSnapshot(_Frozen):
    """The Network links now: what the dashboard and metrics read."""

    # Uplink first, then Ethernet, then Wi-Fi, each by name.
    links: tuple[NetworkLink, ...] = ()
    # The Uplink; None is No Uplink, unless ``uplink_no_reading``.
    uplink: Uplink | None = None
    uplink_no_reading: bool = True
    # Seconds since the last sample in which every source read; None
    # before the first.
    last_sample_age_seconds: float | None = None
    # Whether that sample is too old to trust (No reading).
    stale: bool = True
