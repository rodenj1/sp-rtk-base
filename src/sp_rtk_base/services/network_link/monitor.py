"""NetworkLinkMonitor: turns raw host readings into the Network link snapshot.

Pure: no I/O, and the caller supplies the clock.  Every rule about what
the dashboard and metrics show for a Network link lives here: which
state it's in and why, the Uplink, the three reasons for a missing value
(Not supported, Disconnected, No reading), throughput, link changes and
the order the links are listed in.

No reading is judged per source (sysfs, addresses, the Uplink probe): a
source is No reading after a failed read, or once its last success is
more than :data:`STALE_AFTER_SECONDS` old, so one failing source doesn't
blank the others.  In No reading every value of a link is empty except
its counter totals, which keep their last value.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from sp_rtk_base.models.network_link_models import (
    AddressReading,
    Counters,
    EthernetDetails,
    HostReading,
    InterfaceReading,
    NetworkLink,
    NetworkLinkSnapshot,
    Supported,
    Throughput,
)
from sp_rtk_base.uplink import Uplink, uplink

SAMPLE_INTERVAL_SECONDS = 2.0
# A source whose last success is older than this is No reading.
STALE_AFTER_SECONDS = 3 * SAMPLE_INTERVAL_SECONDS

WAITING_REASON = "Waiting for the first reading."
NO_CABLE_REASON = "No cable."
WIFI_DISCONNECTED_REASON = "Wi-Fi is disconnected."

# Readings a link may not support, by kind: each is Not supported until
# a successful read gives it while the link is up.
_ETHERNET_FIELDS = ("speed_mbps", "full_duplex")

T = TypeVar("T")


def no_reading_reason(age_seconds: float | None) -> str:
    """Why a source has no reading, given how old its last success is."""
    if age_seconds is None:
        return WAITING_REASON
    return f"No reading for {age_seconds:.0f} s."


def _is_up(interface: InterfaceReading) -> bool:
    # Wi-Fi is up when associated: operstate "up" ("dormant" is not
    # associated).  Its carrier doesn't follow a disassociation on
    # brcmfmac, so it is never used for Wi-Fi.
    if interface.kind == "wifi":
        return interface.operstate == "up"
    return interface.carrier is True


def _throughput(
    previous: tuple[float, Counters] | None, now: float, counters: Counters
) -> Throughput | None:
    """Bytes per second since *previous*; None without one, or across a
    counter reset."""
    if previous is None:
        return None
    then, before = previous
    elapsed = now - then
    rx = counters.rx_bytes - before.rx_bytes
    tx = counters.tx_bytes - before.tx_bytes
    if elapsed <= 0 or rx < 0 or tx < 0:
        return None
    return Throughput(
        rx_bytes_per_second=rx / elapsed, tx_bytes_per_second=tx / elapsed
    )


@dataclass
class _Source(Generic[T]):
    """One source's last successful read, and whether its latest failed."""

    value: T | None = None
    at: float | None = None
    failed: bool = False

    def take(self, value: T | None, now: float) -> None:
        if value is None:
            self.failed = True
        else:
            self.value, self.at, self.failed = value, now, False

    def age(self, now: float) -> float | None:
        return None if self.at is None else now - self.at

    def fresh(self, now: float) -> bool:
        age = self.age(now)
        return not self.failed and age is not None and age <= STALE_AFTER_SECONDS


@dataclass
class _History:
    """What the monitor remembers about one interface between samples."""

    up: bool
    link_changes: int = 0
    supported: set[str] = field(default_factory=lambda: set[str]())
    previous: tuple[float, Counters] | None = None
    throughput: Throughput | None = None


def _addresses_of(
    name: str, addresses: tuple[AddressReading, ...]
) -> tuple[str | None, str | None]:
    """The interface's IPv4 address and its IPv6 address (global first)."""
    ipv4: str | None = None
    ipv6: list[tuple[bool, str]] = []
    for row in addresses:
        if row.interface != name:
            continue
        ip = ipaddress.ip_address(row.address)
        text = f"{row.address}/{row.prefixlen}"
        if ip.version == 4:
            ipv4 = ipv4 or text
        else:
            ipv6.append((ip.is_link_local, text))
    ipv6.sort(key=lambda pair: pair[0])
    return ipv4, ipv6[0][1] if ipv6 else None


class NetworkLinkMonitor:
    """Turns a sequence of host readings into the Network link snapshot.

    Fed one :class:`HostReading` per sample with :meth:`observe`;
    :meth:`snapshot` gives what to show at a given time.  Interfaces are
    taken afresh from every successful sysfs read, so one that vanishes
    disappears (and starts over if it comes back).
    """

    def __init__(self) -> None:
        self._interfaces = _Source[tuple[InterfaceReading, ...]]()
        self._addresses = _Source[tuple[AddressReading, ...]]()
        self._egress = _Source[str | None]()
        self._history: dict[str, _History] = {}
        self._last_full_sample: float | None = None

    def observe(self, reading: HostReading, now: float) -> None:
        """Take in one sample read at monotonic time *now*."""
        self._interfaces.take(reading.interfaces, now)
        self._addresses.take(reading.addresses, now)
        if reading.egress is None:
            self._egress.failed = True
        else:
            self._egress.value, self._egress.at = reading.egress.source, now
            self._egress.failed = False
        if reading.interfaces is None:
            # Throughput never spans a gap in the readings.
            for history in self._history.values():
                history.previous = history.throughput = None
        else:
            self._track(reading.interfaces, now)
        if None not in (reading.interfaces, reading.addresses, reading.egress):
            self._last_full_sample = now

    def snapshot(self, now: float) -> NetworkLinkSnapshot:
        """The Network links at monotonic time *now*."""
        interfaces = self._interfaces.value or ()
        the_uplink, uplink_no_reading = self._uplink(interfaces, now)
        links = [self._link(i, the_uplink, now) for i in interfaces]
        links.sort(
            key=lambda link: (not link.uplink, link.kind != "ethernet", link.name)
        )
        age = None if self._last_full_sample is None else now - self._last_full_sample
        return NetworkLinkSnapshot(
            links=tuple(links),
            uplink=the_uplink,
            uplink_no_reading=uplink_no_reading,
            last_sample_age_seconds=age,
            stale=age is None or age > STALE_AFTER_SECONDS,
        )

    def _track(self, interfaces: tuple[InterfaceReading, ...], now: float) -> None:
        histories: dict[str, _History] = {}
        for interface in interfaces:
            up = _is_up(interface)
            history = self._history.get(interface.name)
            if history is None:
                history = _History(up=up)
            elif history.up != up:
                history.up = up
                history.link_changes += 1
            if up and interface.kind == "ethernet":
                history.supported.update(
                    name
                    for name in _ETHERNET_FIELDS
                    if getattr(interface, name) is not None
                )
            history.throughput = _throughput(history.previous, now, interface.counters)
            history.previous = (now, interface.counters)
            histories[interface.name] = history
        self._history = histories

    def _uplink(
        self, interfaces: tuple[InterfaceReading, ...], now: float
    ) -> tuple[Uplink | None, bool]:
        """The Uplink, and whether it is No reading."""
        if not (self._egress.fresh(now) and self._addresses.fresh(now)):
            return None, True
        rows = (
            (row.interface, row.address)
            for row in self._addresses.value or ()
            if ipaddress.ip_address(row.address).version == 4
        )
        names = {interface.name for interface in interfaces}
        return uplink(self._egress.value, rows, names), False

    def _link(
        self, interface: InterfaceReading, the_uplink: Uplink | None, now: float
    ) -> NetworkLink:
        history = self._history[interface.name]
        is_uplink = the_uplink is not None and the_uplink.interface == interface.name
        if not self._interfaces.fresh(now):
            return NetworkLink(
                name=interface.name,
                kind=interface.kind,
                state="No reading",
                reason=no_reading_reason(self._interfaces.age(now)),
                uplink=is_uplink,
                ethernet=self._ethernet(interface, history, current=False),
                counters=interface.counters,
                link_changes=history.link_changes,
            )
        ipv4 = ipv6 = None
        if self._addresses.fresh(now):
            ipv4, ipv6 = _addresses_of(interface.name, self._addresses.value or ())
        reason = None
        if not history.up:
            reason = (
                NO_CABLE_REASON
                if interface.kind == "ethernet"
                else WIFI_DISCONNECTED_REASON
            )
        return NetworkLink(
            name=interface.name,
            kind=interface.kind,
            state="Connected" if history.up else "Disconnected",
            reason=reason,
            uplink=is_uplink,
            ethernet=self._ethernet(interface, history, current=history.up),
            ipv4=ipv4,
            ipv6=ipv6,
            throughput=history.throughput,
            counters=interface.counters,
            link_changes=history.link_changes,
        )

    @staticmethod
    def _ethernet(
        interface: InterfaceReading, history: _History, *, current: bool
    ) -> EthernetDetails | None:
        """Ethernet details: a Supported value for each reading the link has
        given while up, empty unless *current*."""
        if interface.kind != "ethernet":
            return None

        def supported(name: str, value: T | None) -> Supported[T] | None:
            if name not in history.supported:
                return None
            return Supported[T](value=value if current else None)

        return EthernetDetails(
            speed_mbps=supported("speed_mbps", interface.speed_mbps),
            full_duplex=supported("full_duplex", interface.full_duplex),
        )
