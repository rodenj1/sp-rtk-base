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

from pydantic import BaseModel, ConfigDict

from sp_rtk_base.models.network_link_models import Supported

NotSupported = Literal["Not supported"]
NOT_SUPPORTED: Final[NotSupported] = "Not supported"

T = TypeVar("T")

# One raw reading: its value, None (the read failed) or Not supported.
Raw = T | NotSupported | None


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class HostResourcesReading(_Frozen):
    """One sample of every Host resources reading.  The default, every
    field ``None``, is a sample in which every read failed."""

    # The ``cpu-thermal`` thermal zone's ``temp``, in millidegrees Celsius.
    cpu_temperature_millicelsius: Raw[int] = None
    # The ``rpi_volt`` hwmon device's ``in0_lcrit_alarm``: 1 if the
    # firmware saw undervoltage in the kernel's last ~2 s poll window.
    undervoltage_alarm: Raw[int] = None


class HostResourcesSnapshot(_Frozen):
    """The Host resources now.

    A reading's field is ``None`` when it is Not supported (or not yet
    known to be supported), and a :class:`Supported` whose ``value`` is
    ``None`` while it has no current reading.
    """

    cpu_temperature_celsius: Supported[float] | None = None
    undervoltage: Supported[bool] | None = None
    # Rises (0 -> 1) of the undervoltage alarm the sampler has seen: a
    # counter, so it holds through No reading.  None when Not supported.
    undervoltage_events: int | None = None
    # Seconds since the last sample in which every reading read; None
    # before the first.
    last_sample_age_seconds: float | None = None
