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

from sp_rtk_base.uplink import Uplink

LinkKind = Literal["ethernet", "wifi"]
LinkState = Literal["Connected", "Disconnected", "No reading"]

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


class HostReading(_Frozen):
    """One sample of every source.  ``None`` is a source whose read failed."""

    interfaces: tuple[InterfaceReading, ...] | None = None
    addresses: tuple[AddressReading, ...] | None = None
    egress: EgressReading | None = None


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
    # Wi-Fi details (#136) go in a ``wifi: WifiDetails | None`` here.
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
