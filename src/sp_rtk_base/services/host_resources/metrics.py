"""The Host resources snapshot as Prometheus metrics.

:class:`HostResourcesCollector` holds the latest snapshot the ``/metrics``
endpoint handed it and builds every family from that on each scrape, so
it never reads the host, and a Not supported reading's series simply
isn't built.

To add a metric, add a row to :data:`GAUGES` and to the Host resources
metric table in ``docs/monitoring/README.md``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from prometheus_client.core import GaugeMetricFamily
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


def _supported_value(field: Supported[float] | None) -> float | None:
    """``None`` (no series) when Not supported, NaN with no current value."""
    if field is None:
        return None
    return NAN if field.value is None else field.value


def _cpu_temperature(snapshot: HostResourcesSnapshot, _: float) -> float | None:
    return _supported_value(snapshot.cpu_temperature_celsius)


def _last_sample_timestamp(snapshot: HostResourcesSnapshot, now: float) -> float:
    age = snapshot.last_sample_age_seconds
    return NAN if age is None else now - age


GAUGES: tuple[Gauge, ...] = (
    Gauge(
        "host_cpu_temperature_celsius",
        "CPU temperature in degrees Celsius, from the cpu-thermal thermal zone "
        "(NaN: No reading; absent: Not supported)",
        _cpu_temperature,
    ),
    Gauge(
        "host_last_sample_timestamp_seconds",
        "Unix time of the last Host resources sample in which every reading "
        "read (NaN before the first)",
        _last_sample_timestamp,
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
