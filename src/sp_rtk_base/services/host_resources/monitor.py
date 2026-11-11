"""HostResourcesMonitor: turns raw host readings into the Host resources snapshot.

Pure: no I/O, and the caller supplies the clock.  Every rule about what
``/metrics`` shows for a Host resource lives here.

Each reading is judged on its own, like a Network link source:

* **Not supported:** the latest read said the host doesn't have it, or
  no read has yet said it does.  The snapshot field is ``None``.
* **No reading:** its latest read failed, or its last success is more
  than :data:`NO_READING_AFTER_SECONDS` old.  The value is ``None``.

A counter (:class:`_Counter`) instead holds its totals through No
reading, never goes backwards, and is absent (``None``) when Not
supported.

To add a gauge reading, give it a :class:`_Source` and a snapshot field;
to add a counter, give it a :class:`_Counter`.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from sp_rtk_base.models.host_resources_models import (
    NOT_SUPPORTED,
    HostResourcesReading,
    HostResourcesSnapshot,
    Raw,
)
from sp_rtk_base.models.network_link_models import Supported

SAMPLE_INTERVAL_SECONDS = 2.0
# A reading whose last success is older than this is No reading.
NO_READING_AFTER_SECONDS = 3 * SAMPLE_INTERVAL_SECONDS

T = TypeVar("T")
U = TypeVar("U")
K = TypeVar("K", bound=Hashable)


@dataclass
class _Source(Generic[T]):
    """One reading's last successful value, and what its latest read said."""

    value: T | None = None
    at: float | None = None
    failed: bool = False
    supported: bool = False

    def take(self, raw: Raw[T], now: float) -> bool:
        """Take one sample's raw reading; whether it read (Not supported
        counts as read: there was nothing to read)."""
        if raw is None:
            self.failed = True
            return False
        if raw == NOT_SUPPORTED:
            self.value, self.at, self.failed, self.supported = None, None, False, False
            return True
        self.value, self.at, self.failed, self.supported = raw, now, False, True
        return True

    def current(self, now: float, convert: Callable[[T], U]) -> Supported[U] | None:
        """The reading at *now*: ``None`` if Not supported, an empty
        :class:`Supported` on No reading."""
        if not self.supported:
            return None
        return Supported[U](value=self.value_at(now, convert))

    def value_at(self, now: float, convert: Callable[[T], U]) -> U | None:
        """The reading's value at *now*, or ``None`` on No reading (for a
        reading every host has, so never Not supported)."""
        if (
            self.failed
            or self.value is None
            or self.at is None
            or now - self.at > NO_READING_AFTER_SECONDS
        ):
            return None
        return convert(self.value)


@dataclass
class _Counter(Generic[K]):
    """One counter's totals, by key: a label value, or ``()`` for a
    counter with no labels.

    Fed either raw totals since some fixed start (:meth:`take`, such as
    ``/proc/stat``'s CPU ticks) or a 0/1 flag whose rises it counts
    (:meth:`take_rises`).  Either way its totals hold through No reading,
    never go backwards, and survive the reading going Not supported and
    coming back; :meth:`current` hides them while it is Not supported.
    """

    totals: dict[K, float] = field(default_factory=dict[K, float])
    # :meth:`take`'s last raw total per key, which the next is measured
    # against.
    last_raw_totals: dict[K, float] = field(default_factory=dict[K, float])
    # :meth:`take_rises`'s last flag per key, which a rise is measured
    # against.
    raised: dict[K, bool] = field(default_factory=dict[K, bool])
    supported: bool = False

    def take(self, raw: Raw[Mapping[K, float]]) -> bool:
        """Take one sample's raw totals; whether it read (Not supported
        counts as read).

        A raw total that falls (iowait can) adds nothing, and the counter
        grows again from the new raw value.
        """
        if raw is None or raw == NOT_SUPPORTED:
            return self._unread(raw)
        for key, value in raw.items():
            before = self.last_raw_totals.get(key)
            if before is None:
                self.totals.setdefault(key, value)
            else:
                self.totals[key] += max(0.0, value - before)
            self.last_raw_totals[key] = value
        self.supported = True
        return True

    def take_rises(self, raw: Raw[int], key: K) -> bool:
        """Take one sample's 0/1 flag, counting each 0 -> 1 rise under
        *key*; whether it read.

        The flag before the first read counts as 0, so a flag already
        raised at the first sample is one rise.  A failed read neither
        rises nor resets the flag, so a flag held across it counts once.
        """
        if raw is None or raw == NOT_SUPPORTED:
            return self._unread(raw)
        raised = bool(raw)
        rise = raised and not self.raised.get(key, False)
        self.totals[key] = self.totals.get(key, 0.0) + rise
        self.raised[key] = raised
        self.supported = True
        return True

    def _unread(self, raw: Raw[object]) -> bool:
        """A failed read (``None``) holds; Not supported hides the totals."""
        if raw is None:
            return False
        self.supported = False
        return True

    def current(self) -> dict[K, float] | None:
        """The totals, or ``None`` if Not supported (or nothing read yet)."""
        return dict(self.totals) if self.supported else None


def _event_count(counter: _Counter[tuple[()]]) -> int | None:
    """A label-less event counter's count, or ``None`` if Not supported."""
    totals = counter.current()
    return None if totals is None else int(totals.get((), 0))


def _celsius(millicelsius: int) -> float:
    return millicelsius / 1000.0


class HostResourcesMonitor:
    """Turns a sequence of host readings into the Host resources snapshot.

    Fed one :class:`HostResourcesReading` per sample with :meth:`observe`;
    :meth:`snapshot` gives what to export at a given time.
    """

    def __init__(self) -> None:
        self._cpu_seconds = _Counter[str]()
        self._memory_total = _Source[int]()
        self._memory_available = _Source[int]()
        self._cpu_temperature = _Source[int]()
        self._root_filesystem_size = _Source[int]()
        self._root_filesystem_available = _Source[int]()
        self._undervoltage = _Source[int]()
        self._undervoltage_events = _Counter[tuple[()]]()
        self._last_full_sample: float | None = None

    def observe(self, reading: HostResourcesReading, now: float) -> None:
        """Take in one sample read at monotonic time *now*."""
        read = [
            self._cpu_seconds.take(reading.cpu_seconds),
            self._memory_total.take(reading.memory_total_bytes, now),
            self._memory_available.take(reading.memory_available_bytes, now),
            self._cpu_temperature.take(reading.cpu_temperature_millicelsius, now),
            self._root_filesystem_size.take(reading.root_filesystem_size_bytes, now),
            self._root_filesystem_available.take(
                reading.root_filesystem_available_bytes, now
            ),
            self._undervoltage.take(reading.undervoltage_alarm, now),
        ]
        self._undervoltage_events.take_rises(reading.undervoltage_alarm, ())
        if all(read):
            self._last_full_sample = now

    def snapshot(self, now: float) -> HostResourcesSnapshot:
        """The Host resources at monotonic time *now*."""
        age = None if self._last_full_sample is None else now - self._last_full_sample
        return HostResourcesSnapshot(
            cpu_seconds=self._cpu_seconds.current(),
            memory_total_bytes=self._memory_total.value_at(now, float),
            memory_available_bytes=self._memory_available.value_at(now, float),
            cpu_temperature_celsius=self._cpu_temperature.current(now, _celsius),
            root_filesystem_size_bytes=self._root_filesystem_size.value_at(now, float),
            root_filesystem_available_bytes=self._root_filesystem_available.value_at(
                now, float
            ),
            undervoltage=self._undervoltage.current(now, bool),
            undervoltage_events=_event_count(self._undervoltage_events),
            last_sample_age_seconds=age,
        )
