"""Host resources models: the raw host reading and the snapshot.

**Host resources** are the base computer's own CPU, memory, temperature,
power supply and storage (see CONTEXT.md).  Their missing values use the
Network link vocabulary: **Not supported** and **No reading**.

Two halves:

* :class:`HostResourcesReading` is what the host sampler reads each
  sample, one field per reading.  Each field is the value, ``None`` when
  its read failed (No reading), or :data:`NOT_SUPPORTED` when the host
  doesn't have it at all.
* :class:`HostResourcesSnapshot` is what the Host resources monitor makes
  of the readings so far: what ``/metrics`` exports.

To add a reading, add a field to both and a source to the monitor.
"""

from __future__ import annotations

from typing import Final, Literal, TypeVar

from typing_extensions import TypeAliasType

from sp_rtk_base.models.frozen import FrozenModel
from sp_rtk_base.models.network_link_models import Supported

NotSupported = Literal["Not supported"]
NOT_SUPPORTED: Final[NotSupported] = "Not supported"

T = TypeVar("T")

# One raw reading: its value, None (the read failed) or Not supported.
# TypeAliasType is PEP 695's ``type Raw[T] = ...`` for Python 3.10: a
# generic alias pydantic, mypy and pyright all accept.
Raw = TypeAliasType("Raw", T | NotSupported | None, type_params=(T,))


class HostResourcesReading(FrozenModel):
    """One sample of every Host resources reading.  The default, every
    field ``None``, is a sample in which every read failed."""

    # Seconds of CPU time since boot by mode, summed over every core, from
    # the aggregate ``cpu`` line of ``/proc/stat``.
    cpu_seconds: Raw[dict[str, float]] = None
    # ``/proc/meminfo`` ``MemTotal`` and ``MemAvailable`` (the kernel's
    # estimate, not ``MemFree``), in bytes.
    memory_total_bytes: Raw[int] = None
    memory_available_bytes: Raw[int] = None
    # The ``cpu-thermal`` thermal zone's ``temp``, in millidegrees Celsius.
    cpu_temperature_millicelsius: Raw[int] = None
    # The ``rpi_volt`` hwmon device's ``in0_lcrit_alarm``: 1 if the
    # firmware saw undervoltage in the kernel's last ~2 s poll window.
    undervoltage_alarm: Raw[int] = None
    # The root filesystem's size and the space a non-root user can use, in
    # bytes, from ``statvfs("/")``.
    root_filesystem_size_bytes: Raw[int] = None
    root_filesystem_available_bytes: Raw[int] = None


class HostResourcesSnapshot(FrozenModel):
    """The Host resources now.

    A :class:`Supported` field is ``None`` when its reading is Not
    supported (or not yet known to be supported), and a
    :class:`Supported` whose ``value`` is ``None`` while it has no current
    reading.  A plain ``float`` field is for a reading every host has:
    ``None`` is No reading.
    """

    # A counter, by mode: each mode's total never goes backwards and holds
    # through No reading.  None before the first reading.
    cpu_seconds: dict[str, float] | None = None
    # Every host has these: ``None`` is No reading (NaN), never Not supported.
    memory_total_bytes: float | None = None
    memory_available_bytes: float | None = None
    cpu_temperature_celsius: Supported[float] | None = None
    undervoltage: Supported[bool] | None = None
    # Rises (0 -> 1) of the undervoltage alarm the sampler has seen: a
    # counter, so it holds through No reading.  None when Not supported.
    undervoltage_events: int | None = None
    root_filesystem_size_bytes: float | None = None
    root_filesystem_available_bytes: float | None = None
    # Seconds since the last sample in which every reading read; None
    # before the first.
    last_sample_age_seconds: float | None = None
