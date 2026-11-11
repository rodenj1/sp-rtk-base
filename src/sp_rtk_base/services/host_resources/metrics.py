"""The Host resources snapshot as Prometheus metrics.

:class:`HostResourcesCollector` holds the latest snapshot the ``/metrics``
endpoint handed it and builds every family from that on each scrape, so
it never reads the host, and a Not supported reading's series simply
isn't built.

To add a metric, add a row to :data:`GAUGES` (or :data:`COUNTERS`) and
to the Host resources metric table in ``docs/monitoring/README.md``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.metrics_core import Metric

from sp_rtk_base.models.host_resources_models import HostResourcesSnapshot
from sp_rtk_base.models.network_link_models import Supported

NAN = math.nan


@dataclass(frozen=True)
class Gauge:
    """A gauge with no labels.  ``value`` gets the snapshot and the
    wall-clock time of the scrape, and gives NaN with no current reading
    or ``None`` for no series at all (Not supported)."""

    name: str
    documentation: str
    value: Callable[[HostResourcesSnapshot, float], float | None]


@dataclass(frozen=True)
class Counter:
    """A counter, whose totals hold through No reading and are never NaN.

    ``name`` has no ``_total``: the exposition adds it.  ``labels`` are
    the label names, ``()`` for none.  ``totals`` gets the snapshot and
    gives each series' total keyed by its label values (``{(): total}``
    with no labels), or ``None`` for no series at all (Not supported, or
    nothing counted yet).
    """

    name: str
    documentation: str
    labels: tuple[str, ...]
    totals: Callable[[HostResourcesSnapshot], Mapping[tuple[str, ...], float] | None]


def _bytes(value: float | None) -> float:
    """NaN with No reading."""
    return NAN if value is None else value


def _supported_value(field: Supported[float] | None) -> float | None:
    """``None`` (no series) when Not supported, NaN with no current value."""
    if field is None:
        return None
    return NAN if field.value is None else field.value


def _cpu_temperature(snapshot: HostResourcesSnapshot, _: float) -> float | None:
    return _supported_value(snapshot.cpu_temperature_celsius)


def _undervoltage(snapshot: HostResourcesSnapshot, _: float) -> float | None:
    field = snapshot.undervoltage
    if field is None:
        return None
    return NAN if field.value is None else float(field.value)


def _last_sample_timestamp(snapshot: HostResourcesSnapshot, now: float) -> float:
    age = snapshot.last_sample_age_seconds
    return NAN if age is None else now - age


GAUGES: tuple[Gauge, ...] = (
    Gauge(
        "host_memory_total_bytes",
        "Total usable RAM in bytes, from /proc/meminfo MemTotal (NaN: No reading)",
        lambda snapshot, _: _bytes(snapshot.memory_total_bytes),
    ),
    Gauge(
        "host_memory_available_bytes",
        "RAM available to start new applications without swapping in bytes, the "
        "kernel's MemAvailable estimate (NaN: No reading)",
        lambda snapshot, _: _bytes(snapshot.memory_available_bytes),
    ),
    Gauge(
        "host_cpu_temperature_celsius",
        "CPU temperature in degrees Celsius, from the cpu-thermal thermal zone "
        "(NaN: No reading; absent: Not supported)",
        _cpu_temperature,
    ),
    Gauge(
        "host_root_filesystem_size_bytes",
        "Size of the root filesystem in bytes (NaN: No reading)",
        lambda snapshot, _: _bytes(snapshot.root_filesystem_size_bytes),
    ),
    Gauge(
        "host_root_filesystem_available_bytes",
        "Space on the root filesystem a non-root user can use, in bytes "
        "(NaN: No reading)",
        lambda snapshot, _: _bytes(snapshot.root_filesystem_available_bytes),
    ),
    Gauge(
        "host_undervoltage",
        "1 if the Pi firmware saw undervoltage in about the last 2 s, from the "
        "rpi_volt hwmon alarm, else 0 (NaN: No reading; absent: Not supported)",
        _undervoltage,
    ),
    Gauge(
        "host_last_sample_timestamp_seconds",
        "Unix time of the last Host resources sample in which every reading "
        "read (NaN before the first)",
        _last_sample_timestamp,
    ),
)


def _cpu_seconds(
    snapshot: HostResourcesSnapshot,
) -> dict[tuple[str, ...], float] | None:
    if snapshot.cpu_seconds is None:
        return None
    return {(mode,): total for mode, total in snapshot.cpu_seconds.items()}


def _unlabelled(total: float | None) -> dict[tuple[str, ...], float] | None:
    """A counter with no labels: its one series, or none."""
    return None if total is None else {(): total}


COUNTERS: tuple[Counter, ...] = (
    Counter(
        "host_cpu_seconds",
        "CPU time since boot in seconds by mode, summed over every core, from "
        "the aggregate cpu line of /proc/stat (holds through No reading; a "
        "mode that falls, as iowait can, is clamped)",
        ("mode",),
        _cpu_seconds,
    ),
    Counter(
        "host_undervoltage_events",
        "Rises (0 to 1) of the rpi_volt undervoltage alarm seen by the 2 s "
        "host sampler (holds on No reading; absent: Not supported)",
        (),
        lambda snapshot: _unlabelled(snapshot.undervoltage_events),
    ),
)


class HostResourcesCollector:
    """Prometheus collector over the latest Host resources snapshot."""

    def __init__(
        self,
        namespace: str = "sp_rtk_base",
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._namespace = namespace
        self._wall_clock = wall_clock
        self._snapshot = HostResourcesSnapshot()

    def update(self, snapshot: HostResourcesSnapshot) -> None:
        """Export *snapshot* from the next scrape on."""
        self._snapshot = snapshot

    def describe(self) -> Iterable[Metric]:
        """The families (so registering never reads the clock or snapshot)."""
        return list(self._families(HostResourcesSnapshot(), NAN, describe=True))

    def collect(self) -> Iterable[Metric]:
        return list(self._families(self._snapshot, self._wall_clock()))

    def _families(
        self, snapshot: HostResourcesSnapshot, now: float, describe: bool = False
    ) -> Iterator[Metric]:
        for gauge in GAUGES:
            family = GaugeMetricFamily(
                f"{self._namespace}_{gauge.name}", gauge.documentation
            )
            value = gauge.value(snapshot, now)
            if value is not None and not describe:
                family.add_metric((), value)
            yield family
        for counter in COUNTERS:
            counter_family = CounterMetricFamily(
                f"{self._namespace}_{counter.name}",
                counter.documentation,
                labels=counter.labels,
            )
            totals = None if describe else counter.totals(snapshot)
            for label_values, total in (totals or {}).items():
                counter_family.add_metric(label_values, total)
            yield counter_family
