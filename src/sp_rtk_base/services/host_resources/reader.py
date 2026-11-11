"""The host sampler's reads: one :class:`HostResourcesReading` per sample.

Read-only and standard library only: sysfs, procfs and one ``statvfs``
call, with no subprocesses.  Every
read is blocking but quick, so the sampler runs it off the event loop.
Each reading is taken on its own: one whose read raises is ``None`` in
the reading (No reading) and never stops the others.  One the host
doesn't have is :data:`NOT_SUPPORTED`.

To add a reading, add a ``read_*`` function and call it from
:meth:`HostResourcesReader.read`.
"""

from __future__ import annotations

import logging
import os
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

PROC = Path("/proc")
# The aggregate ``cpu`` line's columns, in order.  ``guest`` and
# ``guest_nice`` are left out: the kernel already counts them in ``user``
# and ``nice``.  Older kernels have fewer columns.
CPU_MODES = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")

ReadFile = Callable[[Path], str]
T = TypeVar("T")


class FilesystemSpace(Protocol):
    """The part of ``os.statvfs``'s result the reader uses."""

    @property
    def f_frsize(self) -> int: ...
    @property
    def f_blocks(self) -> int: ...
    @property
    def f_bavail(self) -> int: ...


Statvfs = Callable[[str], FilesystemSpace]


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


def read_cpu_seconds(
    proc: Path = PROC, clock_ticks: int = 100, read: ReadFile = _read_text
) -> dict[str, float]:
    """Seconds of CPU time since boot by mode, summed over every core, from
    the aggregate ``cpu`` line of ``/proc/stat`` (in *clock_ticks* per
    second).

    Raises:
        OSError, ValueError: ``/proc/stat`` couldn't be read or parsed.
    """
    for line in read(proc / "stat").splitlines():
        name, *columns = line.split() or [""]
        if name == "cpu":
            if len(columns) < 4:  # user nice system idle: every kernel has these
                raise ValueError("/proc/stat's cpu line is too short")
            return {
                mode: int(ticks) / clock_ticks
                for mode, ticks in zip(CPU_MODES, columns, strict=False)
            }
    raise ValueError("/proc/stat has no aggregate cpu line")


def read_memory(proc: Path = PROC, read: ReadFile = _read_text) -> tuple[int, int]:
    """``MemTotal`` and ``MemAvailable`` from ``/proc/meminfo``, in bytes.

    Raises:
        OSError, ValueError, KeyError: ``/proc/meminfo`` couldn't be read,
            or lacks either figure.
    """
    kib: dict[str, str] = {}
    for line in read(proc / "meminfo").splitlines():
        name, _, rest = line.partition(":")
        kib[name] = rest.split()[0] if rest.split() else ""
    return int(kib["MemTotal"]) * 1024, int(kib["MemAvailable"]) * 1024


def read_root_filesystem(statvfs: Statvfs = os.statvfs) -> tuple[int, int]:
    """The root filesystem's size and the space a non-root user can use
    (``f_bavail``: not the root-reserved blocks), in bytes.  Its read-only
    flag is ignored: the service unit's hardening always sets it.

    Raises:
        OSError: ``statvfs`` failed.
    """
    space = statvfs("/")
    return space.f_blocks * space.f_frsize, space.f_bavail * space.f_frsize


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
        self,
        thermal_root: Path = SYSFS_THERMAL,
        proc_root: Path = PROC,
        clock_ticks: int | None = None,
        statvfs: Statvfs = os.statvfs,
        read: ReadFile = _read_text,
    ) -> None:
        self._thermal_root = thermal_root
        self._proc_root = proc_root
        self._clock_ticks = clock_ticks or os.sysconf("SC_CLK_TCK")
        self._statvfs = statvfs
        self._read = read

    def read(self) -> HostResourcesReading:
        """One sample of every reading.  Blocking: call it off the event loop."""
        memory = _attempt(
            "the memory", lambda: read_memory(self._proc_root, self._read)
        )
        filesystem = _attempt(
            "the root filesystem", lambda: read_root_filesystem(self._statvfs)
        )
        return HostResourcesReading(
            cpu_seconds=_attempt(
                "the CPU time",
                lambda: read_cpu_seconds(
                    self._proc_root, self._clock_ticks, self._read
                ),
            ),
            memory_total_bytes=_part(memory, 0),
            memory_available_bytes=_part(memory, 1),
            cpu_temperature_millicelsius=_attempt(
                "the CPU temperature",
                lambda: read_cpu_temperature(self._thermal_root, self._read),
            ),
            root_filesystem_size_bytes=_part(filesystem, 0),
            root_filesystem_available_bytes=_part(filesystem, 1),
        )


def _part(raw: Raw[tuple[int, int]], index: int) -> Raw[int]:
    """One figure of a read that gives two (both fail together)."""
    return raw[index] if isinstance(raw, tuple) else raw
