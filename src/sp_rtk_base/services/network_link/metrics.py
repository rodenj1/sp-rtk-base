"""The Network link snapshot as Prometheus metrics.

:class:`NetworkLinkCollector` holds the latest snapshot the ``/metrics``
endpoint handed it and builds every family from that on each scrape, so
it never samples, and a vanished interface's series simply aren't built.

To add a metric, add a row to :data:`LINK_METRICS` (one series per link)
or :data:`GAUGES` (one unlabelled series), and to the metric reference in
``docs/monitoring/README.md``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Literal, TypeVar

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.metrics_core import Metric

from sp_rtk_base.models.level_models import LEVEL_VALUE
from sp_rtk_base.models.network_link_models import (
    NetworkLink,
    NetworkLinkSnapshot,
    Supported,
    WifiDetails,
)

NAN = math.nan

T = TypeVar("T")


@dataclass(frozen=True)
class LinkMetric:
    """One series per Network link.

    ``value`` gives the link's value: NaN with no current reading,
    ``None`` for no series at all (Not supported, or a metric for the
    other kind).  A counter's value is never NaN: counter totals are kept
    through No reading.  ``name`` has no ``_total``: a counter's exposition
    adds it.  Labels are ``interface`` and ``kind``, or only ``interface``
    for a kind's own metrics (``ethernet_*``, ``wifi_*``).
    """

    name: str
    documentation: str
    value: Callable[[NetworkLink, NetworkLinkSnapshot], float | None]
    type: Literal["gauge", "counter"] = "gauge"
    kind_label: bool = True


@dataclass(frozen=True)
class Gauge:
    """A gauge with no labels, one value for the whole snapshot.
    ``value`` gets the snapshot and the wall-clock time of the scrape."""

    name: str
    documentation: str
    value: Callable[[NetworkLinkSnapshot, float], float]


def _flag(flag: bool) -> float:
    return 1.0 if flag else 0.0


def _up(link: NetworkLink, _: NetworkLinkSnapshot) -> float:
    if link.state == "No reading":
        return NAN
    return _flag(link.state == "Connected")


def _uplink(link: NetworkLink, snapshot: NetworkLinkSnapshot) -> float:
    return NAN if snapshot.uplink_no_reading else _flag(link.uplink)


def _link_changes(link: NetworkLink, _: NetworkLinkSnapshot) -> float:
    return link.link_changes


def _kernel_counter(direction: str, what: str) -> LinkMetric:
    """``network_{receive,transmit}_{what}``, from the ``rx_``/``tx_`` kernel
    counter of that name."""
    field = f"{'rx' if direction == 'receive' else 'tx'}_{what}"

    def value(link: NetworkLink, _: NetworkLinkSnapshot) -> float:
        total: int = getattr(link.counters, field)
        return total

    return LinkMetric(
        f"network_{direction}_{what}",
        f"Network link {what} {'received' if direction == 'receive' else 'sent'}, "
        "as the kernel counts them (resets when the interface reappears)",
        value,
        type="counter",
    )


def _supported_value(
    field: Supported[T] | None, to_float: Callable[[T], float]
) -> float | None:
    """A Supported reading's value: ``None`` (no series) when Not
    supported, NaN while it has no current value."""
    if field is None:
        return None
    return NAN if field.value is None else to_float(field.value)


def _ethernet_speed(link: NetworkLink, _: NetworkLinkSnapshot) -> float | None:
    if link.ethernet is None:
        return None
    return _supported_value(link.ethernet.speed_mbps, lambda mbps: mbps * 1_000_000.0)


def _ethernet_full_duplex(link: NetworkLink, _: NetworkLinkSnapshot) -> float | None:
    if link.ethernet is None:
        return None
    return _supported_value(link.ethernet.full_duplex, _flag)


def _wifi_metric(
    name: str,
    documentation: str,
    field: Callable[[WifiDetails], Supported[int] | None],
    scale: float = 1.0,
    type: Literal["gauge", "counter"] = "gauge",
) -> LinkMetric:
    """A ``wifi_*`` metric from one Wi-Fi details field, times *scale*."""

    def value(link: NetworkLink, _: NetworkLinkSnapshot) -> float | None:
        if link.wifi is None:
            return None
        return _supported_value(field(link.wifi), lambda v: v * scale)

    return LinkMetric(name, documentation, value, type=type, kind_label=False)


def _wifi_link_strength(link: NetworkLink, _: NetworkLinkSnapshot) -> float | None:
    if link.wifi is None:
        return None
    return _supported_value(link.wifi.link_strength, lambda level: LEVEL_VALUE[level])


def _uplink_other(snapshot: NetworkLinkSnapshot, _: float) -> float:
    if snapshot.uplink_no_reading:
        return NAN
    uplink = snapshot.uplink
    return _flag(uplink is not None and not uplink.network_link)


def _last_sample_timestamp(snapshot: NetworkLinkSnapshot, now: float) -> float:
    age = snapshot.last_sample_age_seconds
    return NAN if age is None else now - age


LINK_METRICS: tuple[LinkMetric, ...] = (
    LinkMetric(
        "network_link_up",
        "Whether the Network link is connected: 1 or 0 (NaN: No reading)",
        _up,
    ),
    LinkMetric(
        "network_link_uplink",
        "Whether the Network link is the Uplink: 1 or 0 (NaN: No reading)",
        _uplink,
    ),
    LinkMetric(
        "network_link_changes",
        "Network link up/down transitions seen since the app started",
        _link_changes,
        type="counter",
    ),
    *(
        _kernel_counter(direction, what)
        for direction in ("receive", "transmit")
        for what in ("bytes", "packets", "errors", "drops")
    ),
    LinkMetric(
        "network_ethernet_speed_bits_per_second",
        "Ethernet link speed in bits per second (NaN: Disconnected or No reading)",
        _ethernet_speed,
        kind_label=False,
    ),
    LinkMetric(
        "network_ethernet_full_duplex",
        "1 for full duplex, 0 for half (NaN: Disconnected or No reading)",
        _ethernet_full_duplex,
        kind_label=False,
    ),
    LinkMetric(
        "wifi_link_strength",
        "Wi-Fi link strength: 0 Good, 1 Marginal, 2 Poor, judged from the "
        "lower median of the last 10 s of dBm (NaN: no current reading)",
        _wifi_link_strength,
        kind_label=False,
    ),
    _wifi_metric(
        "wifi_link_strength_dbm",
        "The latest Wi-Fi link strength reading, in dBm (NaN: no current reading)",
        lambda wifi: wifi.signal_dbm,
    ),
    _wifi_metric(
        "wifi_frequency_hertz",
        "Wi-Fi channel frequency in hertz while associated "
        "(NaN: Disconnected or No reading)",
        lambda wifi: wifi.frequency_mhz,
        scale=1_000_000.0,
    ),
    _wifi_metric(
        "wifi_tx_bitrate_bits_per_second",
        "Wi-Fi bitrate of the last frames sent, in bits per second "
        "(NaN: Disconnected or No reading)",
        lambda wifi: wifi.tx_bitrate_bps,
    ),
    _wifi_metric(
        "wifi_rx_bitrate_bits_per_second",
        "Wi-Fi bitrate of the last frames received, in bits per second "
        "(NaN: Disconnected or No reading)",
        lambda wifi: wifi.rx_bitrate_bps,
    ),
    _wifi_metric(
        "wifi_tx_failed",
        "Wi-Fi frames the access point never acknowledged, as the driver "
        "counts them (resets with each association)",
        lambda wifi: wifi.tx_failed,
        type="counter",
    ),
)

GAUGES: tuple[Gauge, ...] = (
    Gauge(
        "network_uplink_other",
        "1 when the Uplink is an interface that isn't a Network link, such as "
        "a VPN, else 0 (NaN: No reading)",
        _uplink_other,
    ),
    Gauge(
        "network_last_sample_timestamp_seconds",
        "Unix time of the last Network link sample in which every source "
        "read (NaN before the first)",
        _last_sample_timestamp,
    ),
)


class NetworkLinkCollector:
    """Prometheus collector over the latest Network link snapshot."""

    def __init__(
        self,
        namespace: str = "sp_rtk_base",
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._namespace = namespace
        self._wall_clock = wall_clock
        self._snapshot = NetworkLinkSnapshot()

    def update(self, snapshot: NetworkLinkSnapshot) -> None:
        """Export *snapshot* from the next scrape on."""
        self._snapshot = snapshot

    def describe(self) -> Iterable[Metric]:
        """The families (so registering never reads the clock or snapshot)."""
        return list(self._families(NetworkLinkSnapshot(), NAN))

    def collect(self) -> Iterable[Metric]:
        return list(self._families(self._snapshot, self._wall_clock()))

    def _families(self, snapshot: NetworkLinkSnapshot, now: float) -> Iterator[Metric]:
        ns = self._namespace
        for gauge in GAUGES:
            yield GaugeMetricFamily(
                f"{ns}_{gauge.name}", gauge.documentation, gauge.value(snapshot, now)
            )
        for spec in LINK_METRICS:
            labels = ("interface", "kind") if spec.kind_label else ("interface",)
            family_type = (
                CounterMetricFamily if spec.type == "counter" else GaugeMetricFamily
            )
            family = family_type(f"{ns}_{spec.name}", spec.documentation, labels=labels)
            for link in snapshot.links:
                value = spec.value(link, snapshot)
                if value is not None:
                    label_values = (link.name, link.kind)[: len(labels)]
                    family.add_metric(label_values, value)
            yield family
