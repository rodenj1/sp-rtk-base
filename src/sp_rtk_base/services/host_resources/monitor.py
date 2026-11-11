"""HostResourcesMonitor: turns raw host readings into the Host resources snapshot.

Pure: no I/O, and the caller supplies the clock.  Every rule about what
``/metrics`` shows for a Host resource lives here.

Each reading is judged on its own, like a Network link source:

* **Not supported:** the latest read said the host doesn't have it, or
  no read has yet said it does.  The snapshot field is ``None``.
* **No reading:** its latest read failed, or its last success is more
  than :data:`NO_READING_AFTER_SECONDS` old.  The value is ``None``.

Counters hold their value through No reading and are absent (``None``)
when Not supported.

To add a reading, give it a :class:`_Source` and a snapshot field.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
        if (
            self.failed
            or self.value is None
            or self.at is None
            or now - self.at > NO_READING_AFTER_SECONDS
        ):
            return Supported[U]()
        return Supported[U](value=convert(self.value))


@dataclass
class _RiseCounter:
    """Counts each 0 -> 1 rise of a flag across successful reads.

    The flag before the first read counts as 0, so a flag already raised
    at the first sample is one rise.  A failed read neither rises nor
    resets the flag, so a flag held across it counts once.
    """

    count: int = 0
    raised: bool = False

    def take(self, raw: Raw[int]) -> None:
        if raw is None or raw == NOT_SUPPORTED:
            return
        if raw and not self.raised:
            self.count += 1
        self.raised = bool(raw)


def _flag(value: int) -> bool:
    return bool(value)


def _celsius(millicelsius: int) -> float:
    return millicelsius / 1000.0


class HostResourcesMonitor:
    """Turns a sequence of host readings into the Host resources snapshot.

    Fed one :class:`HostResourcesReading` per sample with :meth:`observe`;
    :meth:`snapshot` gives what to export at a given time.
    """

    def __init__(self) -> None:
        self._cpu_temperature = _Source[int]()
        self._undervoltage = _Source[int]()
        self._undervoltage_events = _RiseCounter()
        self._last_full_sample: float | None = None

    def observe(self, reading: HostResourcesReading, now: float) -> None:
        """Take in one sample read at monotonic time *now*."""
        read = [
            self._cpu_temperature.take(reading.cpu_temperature_millicelsius, now),
            self._undervoltage.take(reading.undervoltage_alarm, now),
        ]
        self._undervoltage_events.take(reading.undervoltage_alarm)
        if all(read):
            self._last_full_sample = now

    def snapshot(self, now: float) -> HostResourcesSnapshot:
        """The Host resources at monotonic time *now*."""
        age = None if self._last_full_sample is None else now - self._last_full_sample
        return HostResourcesSnapshot(
            cpu_temperature_celsius=self._cpu_temperature.current(now, _celsius),
            undervoltage=self._undervoltage.current(now, _flag),
            undervoltage_events=(
                self._undervoltage_events.count
                if self._undervoltage.supported
                else None
            ),
            last_sample_age_seconds=age,
        )
