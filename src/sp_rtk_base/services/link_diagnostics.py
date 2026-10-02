"""Measuring how corrections travel to the receiver (bench diagnosis, #197).

Small helpers the survey, the correction feed and the u-blox driver use to
report what the link is doing, so a slow or lossy delivery can be measured
rather than guessed at:

- :class:`Sampler` keeps the last few hundred values of one measurement and
  summarises them as a :class:`~sp_rtk_base.models.device_models.Spread`.
- :func:`msm_epoch_age_s` reads a GPS or Galileo MSM Frame's own epoch time
  and says how old its data is now. It reads one header field; it never
  decodes the corrections.
"""

from __future__ import annotations

import statistics
import time
from collections import deque

from sp_rtk_base.models.device_models import Spread

#: How many recent values a Sampler keeps.
SAMPLER_SIZE = 500

# GPS time = UTC + 18 s (leap seconds since 1980, unchanged since 2017).
_GPS_EPOCH_UNIX = 315_964_800
_GPS_LEAP_S = 18
_WEEK_MS = 604_800_000
# GPS (1071-1077) and Galileo (1091-1097) MSM epochs are the time of week in
# ms, on the same clock; GLONASS and BeiDou use other time scales.
_TOW_MSM = frozenset({*range(1071, 1078), *range(1091, 1098)})


class Sampler:
    """The recent values of one measurement, and their spread."""

    def __init__(self, size: int = SAMPLER_SIZE) -> None:
        self._values: deque[float] = deque(maxlen=size)
        self.count = 0  # every value ever added, not just those kept

    def add(self, value: float) -> None:
        self._values.append(value)
        self.count += 1

    def spread(self) -> Spread | None:
        """p50 / p95 / max of the values kept; None before the first."""
        values = sorted(self._values)
        if not values:
            return None
        p95 = values[min(len(values) - 1, round(0.95 * (len(values) - 1)))]
        return Spread(
            count=self.count,
            p50=statistics.median(values),
            p95=p95,
            max=values[-1],
        )


def msm_epoch_age_s(frame: bytes, now_unix: float | None = None) -> float | None:
    """How old a GPS or Galileo MSM Frame's data is, in seconds.

    None for any other Frame. Reads the 30-bit epoch time after the message
    number (12 bits) and station id (12 bits).
    """
    if len(frame) < 10 or frame[0] != 0xD3:
        return None
    payload = int.from_bytes(frame[3:10], "big")  # the first 56 payload bits
    message_number = payload >> 44
    if message_number not in _TOW_MSM:
        return None
    epoch_ms = (payload >> 2) & 0x3FFF_FFFF
    now = time.time() if now_unix is None else now_unix
    now_ms = round((now - _GPS_EPOCH_UNIX + _GPS_LEAP_S) * 1000) % _WEEK_MS
    return ((now_ms - epoch_ms) % _WEEK_MS) / 1000.0
