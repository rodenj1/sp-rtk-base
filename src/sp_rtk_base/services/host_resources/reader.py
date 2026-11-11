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
from typing import NamedTuple, Protocol, TypeVar

from sp_rtk_base.models.host_resources_models import (
    NOT_SUPPORTED,
    HostResourcesReading,
    NotSupported,
    Raw,
)
from sp_rtk_base.services.sampling import attempt

logger = logging.getLogger(__name__)

SYSFS_THERMAL = Path("/sys/class/thermal")
# The SoC's thermal zone on the Pi 3B+, 4 and 5: found by type, because
# zone numbers aren't stable (the Pi 5's RP1 and overlays add zones).
CPU_THERMAL_TYPE = "cpu-thermal"

SYSFS_HWMON = Path("/sys/class/hwmon")
# The Pi firmware's undervoltage sensor: found by name, because hwmon
# numbers depend on probe order.
RPI_VOLT_NAME = "rpi_volt"
UNDERVOLTAGE_ALARM = "in0_lcrit_alarm"

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
    """A device's identifying file couldn't be read, so the device may be
    the one wanted."""


def _find_device(
    root: Path, pattern: str, attribute: str, wanted: str, read: ReadFile
) -> Path | None:
    """The device under *root* matching *pattern* whose *attribute* file
    reads *wanted*, or ``None`` if there is none.

    Raises:
        _UnknownError: there is no match, but a device's *attribute*
            couldn't be read.
    """
    if not root.is_dir():
        return None
    unknown = False
    for device in sorted(root.glob(pattern)):
        try:
            value = read(device / attribute).strip()
        except OSError:
            unknown = True
            continue
        if value == wanted:
            return device
    if unknown:
        raise _UnknownError
    return None


def _find_or_raise(
    root: Path, pattern: str, attribute: str, wanted: str, read: ReadFile
) -> Path | None:
    """:func:`_find_device`, with an unknown device as an ``OSError``."""
    try:
        return _find_device(root, pattern, attribute, wanted, read)
    except _UnknownError as exc:
        raise OSError(f"a device's {attribute} couldn't be read") from exc


def read_cpu_temperature(
    root: Path = SYSFS_THERMAL, read: ReadFile = _read_text
) -> int | None:
    """The ``cpu-thermal`` zone's temperature in millidegrees Celsius, or
    ``None`` when there is no such zone (Not supported).

    Raises:
        OSError, ValueError: the zone's temperature couldn't be read.
    """
    zone = _find_or_raise(root, "thermal_zone*", "type", CPU_THERMAL_TYPE, read)
    if zone is None:
        return None
    return int(read(zone / "temp").strip())


def read_undervoltage_alarm(
    root: Path = SYSFS_HWMON, read: ReadFile = _read_text
) -> int | None:
    """The ``rpi_volt`` hwmon device's undervoltage alarm (0 or 1), or
    ``None`` when there is no such device (Not supported).

    The kernel polls the firmware every 2 s, so 1 means undervoltage
    in roughly the last 2 s: it does not latch since boot.

    Raises:
        OSError, ValueError: the alarm couldn't be read, or isn't 0 or 1.
    """
    device = _find_or_raise(root, "hwmon*", "name", RPI_VOLT_NAME, read)
    if device is None:
        return None
    alarm = int(read(device / UNDERVOLTAGE_ALARM).strip())
    if alarm not in (0, 1):
        raise ValueError(f"undervoltage alarm {alarm} is not 0 or 1")
    return alarm


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


class Memory(NamedTuple):
    """``/proc/meminfo``'s two figures, in bytes."""

    total_bytes: int
    available_bytes: int


class RootFilesystem(NamedTuple):
    """The root filesystem's two figures, in bytes."""

    size_bytes: int
    available_bytes: int


def read_memory(proc: Path = PROC, read: ReadFile = _read_text) -> Memory:
    """``MemTotal`` and ``MemAvailable`` from ``/proc/meminfo``, in bytes.

    Raises:
        OSError, ValueError, KeyError: ``/proc/meminfo`` couldn't be read,
            or lacks either figure.
    """
    kib: dict[str, str] = {}
    for line in read(proc / "meminfo").splitlines():
        name, _, rest = line.partition(":")
        kib[name] = rest.split()[0] if rest.split() else ""
    return Memory(
        total_bytes=int(kib["MemTotal"]) * 1024,
        available_bytes=int(kib["MemAvailable"]) * 1024,
    )


def read_root_filesystem(statvfs: Statvfs = os.statvfs) -> RootFilesystem:
    """The root filesystem's size and the space a non-root user can use
    (``f_bavail``: not the root-reserved blocks), in bytes.  Its read-only
    flag is ignored: the service unit's hardening always sets it.

    Raises:
        OSError: ``statvfs`` failed.
    """
    space = statvfs("/")
    return RootFilesystem(
        size_bytes=space.f_blocks * space.f_frsize,
        available_bytes=space.f_bavail * space.f_frsize,
    )


def _attempt(name: str, read: Callable[[], T | None]) -> Raw[T]:
    """*read*'s result: ``None`` (No reading) if it raised, Not supported
    if it gave ``None``."""

    def read_or_not_supported() -> T | NotSupported:
        value = read()
        return NOT_SUPPORTED if value is None else value

    return attempt(logger, f"Host resources read of {name}", read_or_not_supported)


class HostResourcesReaderLike(Protocol):
    """What the host sampler reads the host with."""

    def read(self) -> HostResourcesReading: ...


class HostResourcesReader:
    """Reads one :class:`HostResourcesReading`, each reading failing on its own."""

    def __init__(
        self,
        thermal_root: Path = SYSFS_THERMAL,
        hwmon_root: Path = SYSFS_HWMON,
        proc_root: Path = PROC,
        clock_ticks: int | None = None,
        statvfs: Statvfs = os.statvfs,
        read: ReadFile = _read_text,
    ) -> None:
        self._thermal_root = thermal_root
        self._hwmon_root = hwmon_root
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
            memory_total_bytes=(
                memory.total_bytes if isinstance(memory, Memory) else memory
            ),
            memory_available_bytes=(
                memory.available_bytes if isinstance(memory, Memory) else memory
            ),
            cpu_temperature_millicelsius=_attempt(
                "the CPU temperature",
                lambda: read_cpu_temperature(self._thermal_root, self._read),
            ),
            undervoltage_alarm=_attempt(
                "undervoltage",
                lambda: read_undervoltage_alarm(self._hwmon_root, self._read),
            ),
            # Both figures of one read fail together.
            root_filesystem_size_bytes=(
                filesystem.size_bytes
                if isinstance(filesystem, RootFilesystem)
                else filesystem
            ),
            root_filesystem_available_bytes=(
                filesystem.available_bytes
                if isinstance(filesystem, RootFilesystem)
                else filesystem
            ),
        )
