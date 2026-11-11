"""What the background samplers (Network link, Host resources) share.

Each sampler reads its sources one by one, so one that fails never stops
the others (:func:`attempt`), and its collector builds Prometheus
families from the latest snapshot on each scrape (:class:`Gauge`,
:func:`supported_value`, :func:`sample_timestamp`).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from prometheus_client.core import GaugeMetricFamily

from sp_rtk_base.models.network_link_models import Supported

NAN = math.nan

S = TypeVar("S")
T = TypeVar("T")


def attempt(log: logging.Logger, what: str, read: Callable[[], T]) -> T | None:
    """*read*'s result, or ``None`` (No reading) if it raised, which is
    logged at debug as "*what* failed"."""
    try:
        return read()
    except Exception:
        log.debug("%s failed", what, exc_info=True)
        return None


@dataclass(frozen=True)
class Gauge(Generic[S]):
    """A gauge with no labels, one value for the whole snapshot *S*.

    ``value`` gets the snapshot and the wall-clock time of the scrape, and
    gives NaN with no current reading or ``None`` for no series at all
    (Not supported).
    """

    name: str
    documentation: str
    value: Callable[[S, float], float | None]

    def family(
        self, namespace: str, snapshot: S, now: float, describe: bool = False
    ) -> GaugeMetricFamily:
        """The gauge's family for *snapshot* at *now*: no sample when
        *describe* (registering) or Not supported."""
        family = GaugeMetricFamily(f"{namespace}_{self.name}", self.documentation)
        value = None if describe else self.value(snapshot, now)
        if value is not None:
            family.add_metric((), value)
        return family


def supported_value(
    field: Supported[T] | None, to_float: Callable[[T], float]
) -> float | None:
    """A Supported reading's value: ``None`` (no series) when Not
    supported, NaN while it has no current value."""
    if field is None:
        return None
    return NAN if field.value is None else to_float(field.value)


def sample_timestamp(age: float | None, now: float) -> float:
    """The Unix time of the last full sample, *age* seconds before *now*
    (NaN before the first)."""
    return NAN if age is None else now - age
