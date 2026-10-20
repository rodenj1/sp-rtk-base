"""NetworkLinkMonitor: turns raw host readings into the Network link snapshot.

Pure: no I/O, and the caller supplies the clock.  Every rule about what
the dashboard and metrics show for a Network link lives here: which
state it's in and why, the Uplink, the three reasons for a missing value
(Not supported, Disconnected, No reading), throughput, link changes and
the order the links are listed in.

No reading is judged per source (sysfs, addresses, the Uplink probe,
nl80211, the last per Wi-Fi interface): a
source is No reading after a failed read, or once its last success is
more than :data:`STALE_AFTER_SECONDS` old, so one failing source doesn't
blank the others.  In No reading every value of a link is empty except
its counter totals, which keep their last value.
"""

from __future__ import annotations

import ipaddress
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from sp_rtk_base.models.level_models import Level
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
    WifiDetails,
    WifiReading,
)
from sp_rtk_base.uplink import Uplink, uplink

SAMPLE_INTERVAL_SECONDS = 2.0
# A source whose last success is older than this is No reading.
STALE_AFTER_SECONDS = 3 * SAMPLE_INTERVAL_SECONDS

WAITING_REASON = "Waiting for the first reading."
NO_CABLE_REASON = "No cable."
WIFI_DISCONNECTED_REASON = "Wi-Fi is disconnected."
SETUP_AP_REASON = "The setup access point is on."
WIFI_NO_READING_REASON = "The Wi-Fi adapter gave no reading."

# Wi-Fi link strength thresholds (rodenj1/rtk_development#132), provisional:
# Good at -67 dBm or above, Marginal from -68 to -77, Poor at -78 or
# below, applied to the window's lower median with no deadband, like
# Bluetooth link strength.  -67 is the usual floor for a reliable link;
# the bench-Pi weak-signal readings may retune these.
GOOD_DBM, MARGINAL_DBM = -67, -77

# Wi-Fi link strength is judged from the lower median of this many
# seconds of readings.
STRENGTH_WINDOW_SECONDS = 10.0

# Readings a link may not support, by kind: each is Not supported until
# a successful read gives it while the link is up.
_ETHERNET_FIELDS = ("speed_mbps", "full_duplex")

T = TypeVar("T")


def no_reading_reason(age_seconds: float | None) -> str:
    """Why a source has no reading, given how old its last success is."""
    if age_seconds is None:
        return WAITING_REASON
    return f"No reading for {age_seconds:.0f} s."


def _is_up(interface: InterfaceReading, wifi: WifiReading | None) -> bool:
    # Wi-Fi is up when associated: operstate "up" ("dormant" is not
    # associated) and a station present, never as the setup access
    # point.  Its carrier doesn't follow a disassociation on brcmfmac, so
    # it is never used for Wi-Fi.  Without an nl80211 reading, operstate
    # alone has to do.
    if interface.kind == "wifi":
        if interface.operstate != "up":
            return False
        return wifi is None or (wifi.mode != "ap" and wifi.station is not None)
    return interface.carrier is True


def _wifi_values(wifi: WifiReading | None) -> dict[str, object]:
    """An associated nl80211 reading's Wi-Fi values, by WifiDetails field;
    nothing while unassociated (the frequency then hops as it scans)."""
    if wifi is None or wifi.station is None:
        return {}
    station = wifi.station
    return {
        "ssid": wifi.ssid,
        "bssid": station.bssid,
        "frequency_mhz": wifi.frequency_mhz,
        "tx_bitrate_bps": station.tx_bitrate_bps,
        "rx_bitrate_bps": station.rx_bitrate_bps,
        "signal_dbm": station.signal_dbm,
        "tx_failed": station.tx_failed,
    }


def _level(dbm: int) -> Level:
    if dbm >= GOOD_DBM:
        return "Good"
    if dbm >= MARGINAL_DBM:
        return "Marginal"
    return "Poor"


def _excluded(wifi: tuple[WifiReading, ...]) -> frozenset[str]:
    """Wi-Fi interfaces that aren't Network links: P2P and monitor
    interfaces, and an access point beside a station on the same radio
    (an access point alone on its radio is the setup access point)."""
    station_radios = {w.wiphy for w in wifi if w.mode == "station"}
    return frozenset(
        w.interface
        for w in wifi
        if w.mode in ("p2p", "monitor")
        or (w.mode == "ap" and w.wiphy in station_radios)
    )


def _fully_successful(reading: HostReading) -> bool:
    """Whether every source read, nl80211 for every Wi-Fi interface too."""
    if (
        reading.interfaces is None
        or reading.addresses is None
        or reading.egress is None
        or reading.wifi is None
    ):
        return False
    answered = {wifi.interface for wifi in reading.wifi}
    return all(i.name in answered for i in reading.interfaces if i.kind == "wifi")


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
    # Wi-Fi: the latest sample's nl80211 reading (None: it gave none), the
    # BSSID while associated, and the last failed-sends total.
    wifi: WifiReading | None = None
    bssid: str | None = None
    tx_failed: int | None = None
    # Wi-Fi link strength: (time, dBm) readings of the last 10 s while
    # associated in station mode, cleared on any gap.
    strength: deque[tuple[float, int]] = field(
        default_factory=lambda: deque[tuple[float, int]]()
    )

    def judge(self, dbm: int | None, now: float) -> None:
        """Add one dBm reading to the window, or clear it on a gap."""
        if dbm is None:
            self.strength.clear()
            return
        self.strength.append((now, dbm))
        while self.strength[0][0] <= now - STRENGTH_WINDOW_SECONDS:
            self.strength.popleft()

    def level(self) -> Level | None:
        """The window's verdict: its lower median, so ties lean weaker."""
        if not self.strength:
            return None
        return _level(statistics.median_low(dbm for _, dbm in self.strength))


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
        self._excluded: frozenset[str] = frozenset()
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
            # Throughput and Wi-Fi link strength never span a gap in the
            # readings.
            for history in self._history.values():
                history.previous = history.throughput = None
                history.judge(None, now)
        else:
            self._track(reading.interfaces, reading.wifi, now)
        if _fully_successful(reading):
            self._last_full_sample = now

    def snapshot(self, now: float) -> NetworkLinkSnapshot:
        """The Network links at monotonic time *now*."""
        interfaces = tuple(
            i for i in self._interfaces.value or () if i.name not in self._excluded
        )
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

    def _track(
        self,
        interfaces: tuple[InterfaceReading, ...],
        wifi: tuple[WifiReading, ...] | None,
        now: float,
    ) -> None:
        if wifi is not None:
            self._excluded = _excluded(wifi)
        nl80211 = {reading.interface: reading for reading in wifi or ()}
        histories: dict[str, _History] = {}
        for interface in interfaces:
            if interface.name in self._excluded:
                continue
            wifi_reading = nl80211.get(interface.name)
            up = _is_up(interface, wifi_reading)
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
            if interface.kind == "wifi":
                self._track_wifi(history, wifi_reading, now, up=up)
            history.throughput = _throughput(history.previous, now, interface.counters)
            history.previous = (now, interface.counters)
            histories[interface.name] = history
        self._history = histories

    @staticmethod
    def _track_wifi(
        history: _History, wifi: WifiReading | None, now: float, *, up: bool
    ) -> None:
        history.wifi = wifi
        values = _wifi_values(wifi) if up else {}
        history.supported.update(n for n, v in values.items() if v is not None)
        station = wifi.station if up and wifi is not None else None
        # Judged in station mode only: the setup access point is never up.
        history.judge(None if station is None else station.signal_dbm, now)
        if station is not None and station.tx_failed is not None:
            history.tx_failed = station.tx_failed
        if not up:
            history.bssid = None
        elif station is not None:
            bssid = station.bssid
            # Roaming: a BSSID change while connected is a link change.
            if history.bssid is not None and history.bssid != bssid:
                history.link_changes += 1
            history.bssid = bssid

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
                wifi=self._wifi(interface, history, fresh=False),
                counters=interface.counters,
                link_changes=history.link_changes,
            )
        ipv4 = ipv6 = None
        if self._addresses.fresh(now):
            ipv4, ipv6 = _addresses_of(interface.name, self._addresses.value or ())
        reason = None
        if not history.up:
            if interface.kind == "ethernet":
                reason = NO_CABLE_REASON
            elif history.wifi is not None and history.wifi.mode == "ap":
                reason = SETUP_AP_REASON
            else:
                reason = WIFI_DISCONNECTED_REASON
        return NetworkLink(
            name=interface.name,
            kind=interface.kind,
            state="Connected" if history.up else "Disconnected",
            reason=reason,
            uplink=is_uplink,
            ethernet=self._ethernet(interface, history, current=history.up),
            wifi=self._wifi(interface, history, fresh=True),
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

    @staticmethod
    def _wifi(
        interface: InterfaceReading, history: _History, *, fresh: bool
    ) -> WifiDetails | None:
        """Wi-Fi details: a Supported value for each reading the link has
        given while associated, empty unless the link is *fresh*, up and
        nl80211 read.  The failed-sends total keeps its last value, like
        the counters."""
        if interface.kind != "wifi":
            return None
        wifi = history.wifi if fresh and history.up else None
        station = None if wifi is None else wifi.station

        def supported(name: str, value: T | None) -> Supported[T] | None:
            if name not in history.supported:
                return None
            return Supported[T](value=value)

        return WifiDetails(
            no_reading_reason=(
                WIFI_NO_READING_REASON if fresh and history.wifi is None else None
            ),
            ssid=supported(
                "ssid", None if station is None or wifi is None else wifi.ssid
            ),
            bssid=supported("bssid", None if station is None else station.bssid),
            frequency_mhz=supported(
                "frequency_mhz",
                None if station is None or wifi is None else wifi.frequency_mhz,
            ),
            tx_bitrate_bps=supported(
                "tx_bitrate_bps", None if station is None else station.tx_bitrate_bps
            ),
            rx_bitrate_bps=supported(
                "rx_bitrate_bps", None if station is None else station.rx_bitrate_bps
            ),
            signal_dbm=supported(
                "signal_dbm", None if station is None else station.signal_dbm
            ),
            # Supported with the dBm reading it is judged from.
            link_strength=(
                None
                if "signal_dbm" not in history.supported
                else Supported[Level](
                    value=None if station is None else history.level()
                )
            ),
            tx_failed=supported("tx_failed", history.tx_failed),
        )
