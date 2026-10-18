"""BluetoothLinkMonitor: judges Input statuses into a Bluetooth link strength verdict.

Pure: no I/O, and the caller supplies the clock.  The whole Bluetooth
link strength rule lives here, so it is the one place to review when the
provisional thresholds are revisited.

The reading is the Relay's ``InputStatus.link_rssi``: the adapter's HCI
``Read RSSI`` on a classic Bluetooth link, which is not dBm but a signed
dB offset from a comfortable range the controller chooses (0 = inside
it, negative = that many dB below it).
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from typing import Protocol

from sp_rtk_base.models.bluetooth_link_models import (
    BluetoothLinkNoReading,
    BluetoothLinkReading,
    BluetoothLinkVerdict,
)
from sp_rtk_base.models.signal_quality_models import SignalLevel

# Provisional thresholds (rodenj1/rtk_development#107): Good at 0 or
# above, Marginal from -1 to -5, Poor at -6 or below.  The controller puts
# the bottom of its comfortable range between -56 dBm and 6 dB above its
# own sensitivity, so -6 is the edge of reception on the strictest
# controller and an early warning on a generous one.  Revisit once a
# measured distance curve exists.
GOOD_DB, MARGINAL_DB = 0.0, -5.0

# To leave a level, the median must cross that level's line by this much;
# entering a level uses the plain line.
DEADBAND_DB = 1.0

# The verdict is judged from the median of this many seconds of readings,
# and a verdict with no reading this recent is stale.
WINDOW_SECONDS = 10.0
STALE_AFTER_SECONDS = 10.0

BLUETOOTH_SOURCE_TYPE = "bluetooth"

RELAY_STOPPED_REASON = "The Relay is stopped."
DISCONNECTED_REASON = "The Bluetooth link is disconnected."
NO_READING_REASON = "The adapter gave no reading."

# Each level's line: a median at or above it is in that level or better.
_LINE: dict[SignalLevel, float] = {
    "Good": GOOD_DB,
    "Marginal": MARGINAL_DB,
    "Poor": -math.inf,
}


class InputStatusLike(Protocol):
    """The fields of the Relay's ``InputStatus`` the monitor reads.

    ``link_rssi`` (sp-rtk-base-relay 4.2+) is read when present: an older
    Relay without it reads as no reading.
    """

    @property
    def connected(self) -> bool: ...

    @property
    def source_type(self) -> str: ...


def is_bluetooth_input(input_status: InputStatusLike) -> bool:
    """Whether *input_status* is a Bluetooth Input's."""
    return input_status.source_type.lower() == BLUETOOTH_SOURCE_TYPE


def _level(median: float) -> SignalLevel:
    if median >= GOOD_DB:
        return "Good"
    return "Marginal" if median >= MARGINAL_DB else "Poor"


def _held_level(previous: SignalLevel | None, median: float) -> SignalLevel:
    """The level for *median*, staying at *previous* until clearly below its line.

    Only falling out of a level is held.  Climbing into a better one uses
    the plain line: the reading tops out at 0 on a comfortable link, so a
    deadband above Good's line would keep a recovered link Marginal.
    """
    if previous is not None:
        line = _LINE[previous]
        if line - DEADBAND_DB <= median < line:
            return previous
    return _level(median)


def _link_rssi(input_status: InputStatusLike) -> int | None:
    rssi: object = getattr(input_status, "link_rssi", None)
    if isinstance(rssi, int) and not isinstance(rssi, bool):
        return rssi
    return None


class BluetoothLinkMonitor:
    """Turns a sequence of Input statuses into the current verdict.

    Fed the Relay's Input status at each poll (``None`` while the Relay is
    stopped).  A connected Bluetooth Input with a reading adds it to a
    :data:`WINDOW_SECONDS` window judged by its median.  Anything else is
    no reading (or, for a non-Bluetooth Input, no verdict at all), and
    clears the window: a verdict never carries across a gap.
    """

    def __init__(self) -> None:
        self._window: deque[tuple[float, int]] = deque()
        self._reading: BluetoothLinkReading | None = None

    def observe(self, input_status: InputStatusLike | None, now: float) -> None:
        """Take in the Relay's Input status polled at monotonic time *now*."""
        if input_status is None:
            self._no_reading(RELAY_STOPPED_REASON)
        elif not is_bluetooth_input(input_status):
            self._window.clear()
            self._reading = None
        elif not input_status.connected:
            self._no_reading(DISCONNECTED_REASON)
        elif (rssi := _link_rssi(input_status)) is None:
            self._no_reading(NO_READING_REASON)
        else:
            self._judge(rssi, now)

    def current(self, now: float) -> BluetoothLinkReading | None:
        """The reading at monotonic time *now*; ``None`` without a Bluetooth Input.

        A verdict whose newest reading is :data:`STALE_AFTER_SECONDS` old is
        never returned: stale is no reading, not an old verdict.
        """
        if (
            isinstance(self._reading, BluetoothLinkVerdict)
            and now - self._window[-1][0] >= STALE_AFTER_SECONDS
        ):
            return BluetoothLinkNoReading(reason=NO_READING_REASON)
        return self._reading

    def _no_reading(self, reason: str) -> None:
        self._window.clear()
        self._reading = BluetoothLinkNoReading(reason=reason)

    def _judge(self, rssi: int, now: float) -> None:
        previous = self.current(now)
        held = previous.level if isinstance(previous, BluetoothLinkVerdict) else None
        self._window.append((now, rssi))
        while self._window[0][0] <= now - WINDOW_SECONDS:
            self._window.popleft()
        median = float(statistics.median(r for _, r in self._window))
        self._reading = BluetoothLinkVerdict(
            level=_held_level(held, median), median_db=median
        )
