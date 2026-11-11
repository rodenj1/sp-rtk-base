"""The host sampler's reads: one :class:`HostResourcesReading` per sample.

Read-only and standard library only: sysfs, with no subprocesses.  Every
read is blocking but quick, so the sampler runs it off the event loop.
Each reading is taken on its own: one whose read raises is ``None`` in
the reading (No reading) and never stops the others.  One the host
doesn't have is :data:`NOT_SUPPORTED`.

To add a reading, add a ``read_*`` function and call it from
:meth:`HostResourcesReader.read`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TypeVar

from sp_rtk_base.models.host_resources_models import (
    NOT_SUPPORTED,
    HostResourcesReading,
    Raw,
)

logger = logging.getLogger(__name__)

SYSFS_THERMAL = Path("/sys/class/thermal")
# The SoC's thermal zone on the Pi 3B+, 4 and 5: found by type, because
# zone numbers aren't stable (the Pi 5's RP1 and overlays add zones).
CPU_THERMAL_TYPE = "cpu-thermal"

ReadFile = Callable[[Path], str]
T = TypeVar("T")


def _read_text(path: Path) -> str:
    return path.read_text()


class _UnknownError(Exception):
    """A zone's type couldn't be read, so the zone may be the one wanted."""


def _cpu_thermal_zone(root: Path, read: ReadFile) -> Path | None:
    """The ``cpu-thermal`` zone under *root*, or ``None`` if there is none.

    Raises:
        _UnknownError: there is no match, but a zone's type couldn't be read.
    """
    if not root.is_dir():
        return None
    unknown = False
    for zone in sorted(root.glob("thermal_zone*")):
        try:
            zone_type = read(zone / "type").strip()
        except OSError:
            unknown = True
            continue
        if zone_type == CPU_THERMAL_TYPE:
            return zone
    if unknown:
        raise _UnknownError
    return None


def read_cpu_temperature(
    root: Path = SYSFS_THERMAL, read: ReadFile = _read_text
) -> int | None:
    """The ``cpu-thermal`` zone's temperature in millidegrees Celsius, or
    ``None`` when there is no such zone (Not supported).

    Raises:
        OSError, ValueError: the zone's temperature couldn't be read.
    """
    try:
        zone = _cpu_thermal_zone(root, read)
    except _UnknownError as exc:
        raise OSError("a thermal zone's type couldn't be read") from exc
    if zone is None:
        return None
    return int(read(zone / "temp").strip())


def _attempt(name: str, read: Callable[[], T | None]) -> Raw[T]:
    """*read*'s result: ``None`` (No reading) if it raised, Not supported
    if it gave ``None``."""
    try:
        value = read()
    except Exception:
        logger.debug("Host resources read of %s failed", name, exc_info=True)
        return None
    return NOT_SUPPORTED if value is None else value


class HostResourcesReaderLike(Protocol):
    """What the host sampler reads the host with."""

    def read(self) -> HostResourcesReading: ...


class HostResourcesReader:
    """Reads one :class:`HostResourcesReading`, each reading failing on its own."""

    def __init__(
        self, thermal_root: Path = SYSFS_THERMAL, read: ReadFile = _read_text
    ) -> None:
        self._thermal_root = thermal_root
        self._read = read

    def read(self) -> HostResourcesReading:
        """One sample of every reading.  Blocking: call it off the event loop."""
        return HostResourcesReading(
            cpu_temperature_millicelsius=_attempt(
                "the CPU temperature",
                lambda: read_cpu_temperature(self._thermal_root, self._read),
            ),
        )
