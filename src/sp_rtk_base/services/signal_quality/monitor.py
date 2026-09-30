"""SignalQualityMonitor: judges Signal Snapshots into a Signal Quality verdict.

Pure: no I/O, and the caller supplies the clock.  The whole Signal
Quality rule lives here, so it is the one place to review when the
provisional thresholds are revisited.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass

from sp_rtk_base.models.signal_quality_models import (
    Band,
    SignalLevel,
    SignalQualityVerdict,
    SignalSnapshot,
)

# Provisional thresholds (rodenj1/rtk_development#6).  L1 lines are
# u-blox's; L2 is L1 − 3 (test-base measured L2 ≈ L1 − 1); the satellite
# lines leave margin over RTK's ~8–10 minimum.  Revisit once obstructed
# ("Marginal") field data exists.
L1_GOOD_DBHZ, L1_MARGINAL_DBHZ = 44.0, 40.0
L2_GOOD_DBHZ, L2_MARGINAL_DBHZ = 41.0, 37.0
SATELLITES_GOOD, SATELLITES_MARGINAL = 15, 10

# Measures are smoothed over this many seconds of Snapshots, and a
# verdict with no Snapshot this recent is stale.
WINDOW_SECONDS = 10.0
STALE_AFTER_SECONDS = 10.0

# To leave its current level a measure must cross the threshold by this
# much; entering a level uses the plain threshold.  Stops flicker when a
# value sits on a line.
DEADBAND_DBHZ = 1.0
DEADBAND_SATELLITES = 1.0

# A satellite is usable with at least one signal this strong.
USABLE_SIGNAL_DBHZ = 35.0
# Band strength is the mean of this many strongest signals in the band.
BAND_STRENGTH_SIGNALS = 4

_RANK: dict[SignalLevel, int] = {"Good": 0, "Marginal": 1, "Poor": 2}


def _level(value: float | None, good: float, marginal: float) -> SignalLevel:
    if value is None:
        return "Poor"
    if value >= good:
        return "Good"
    return "Marginal" if value >= marginal else "Poor"


def _held_level(
    previous: SignalLevel | None,
    value: float | None,
    good: float,
    marginal: float,
    deadband: float,
) -> SignalLevel:
    """The level for *value*, staying at *previous* until clearly past it.

    Leaving downwards needs a value below the previous level's lower line
    minus *deadband*; leaving upwards needs one at or above its upper line
    plus *deadband*.  Where it lands is then the plain level.
    """
    plain = _level(value, good, marginal)
    if previous is None or value is None or plain == previous:
        return plain
    lower = {"Good": good, "Marginal": marginal, "Poor": -math.inf}[previous]
    upper = {"Good": math.inf, "Marginal": good, "Poor": marginal}[previous]
    if value < lower - deadband or value >= upper + deadband:
        return plain
    return previous


def _band_strength(snapshot: SignalSnapshot, band: Band) -> float | None:
    strongest = sorted(
        (s.cn0_dbhz for s in snapshot.signals if s.band is band and s.cn0_dbhz > 0),
        reverse=True,
    )[:BAND_STRENGTH_SIGNALS]
    return sum(strongest) / len(strongest) if strongest else None


def _usable_satellites(snapshot: SignalSnapshot) -> int:
    return len(
        {
            (s.constellation, s.satellite)
            for s in snapshot.signals
            if s.cn0_dbhz >= USABLE_SIGNAL_DBHZ
        }
    )


@dataclass(frozen=True)
class _Measures:
    """One Snapshot's measures (or a window's smoothed ones)."""

    l1: float | None
    l2: float | None
    satellites: int


def _measure(snapshot: SignalSnapshot) -> _Measures:
    return _Measures(
        _band_strength(snapshot, Band.L1),
        _band_strength(snapshot, Band.L2),
        _usable_satellites(snapshot),
    )


def _mean(values: list[float | None]) -> float | None:
    """Mean of the values present; ``None`` only if every one is missing."""
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _smooth(window: list[_Measures]) -> _Measures:
    return _Measures(
        _mean([m.l1 for m in window]),
        _mean([m.l2 for m in window]),
        math.floor(statistics.median(m.satellites for m in window)),
    )


def judge(snapshot: SignalSnapshot) -> SignalQualityVerdict:
    """Judge one Signal Snapshot on its own: the worst of the three measures wins."""
    return _verdict(_measure(snapshot), None)


def _verdict(
    measures: _Measures, previous: SignalQualityVerdict | None
) -> SignalQualityVerdict:
    """Judge *measures*, holding each level of the *previous* verdict (deadband)."""
    l1, l2, satellites = measures.l1, measures.l2, measures.satellites

    l1_level = _held_level(
        previous and previous.l1_level,
        l1,
        L1_GOOD_DBHZ,
        L1_MARGINAL_DBHZ,
        DEADBAND_DBHZ,
    )
    l2_level = _held_level(
        previous and previous.l2_level,
        l2,
        L2_GOOD_DBHZ,
        L2_MARGINAL_DBHZ,
        DEADBAND_DBHZ,
    )
    satellites_level = _held_level(
        previous and previous.satellites_level,
        float(satellites),
        SATELLITES_GOOD,
        SATELLITES_MARGINAL,
        DEADBAND_SATELLITES,
    )
    worst = max((l1_level, l2_level, satellites_level), key=lambda v: _RANK[v])

    causes: list[str] = []
    if worst != "Good":
        if l1_level == worst:
            causes.append("no L1 signal" if l1 is None else "L1 weak")
        if l2_level == worst:
            causes.append("no L2 signal" if l2 is None else "L2 weak")
        if satellites_level == worst:
            causes.append("few satellites")

    return SignalQualityVerdict(
        level=worst,
        l1_level=l1_level,
        l2_level=l2_level,
        satellites_level=satellites_level,
        cause=", ".join(causes),
        l1_strength_dbhz=l1,
        l2_strength_dbhz=l2,
        usable_satellites=satellites,
    )


class SignalQualityMonitor:
    """Turns a stream of Signal Snapshots into the current verdict.

    Measures are smoothed over the last :data:`WINDOW_SECONDS` of
    Snapshots: Band strengths are the mean, Usable satellites the median
    rounded down.
    """

    def __init__(self) -> None:
        self._window: deque[tuple[float, _Measures]] = deque()
        self._verdict: SignalQualityVerdict | None = None

    def observe(self, snapshot: SignalSnapshot, now: float) -> None:
        """Take in a Snapshot received at monotonic time *now*."""
        self._window.append((now, _measure(snapshot)))
        while self._window and self._window[0][0] <= now - WINDOW_SECONDS:
            self._window.popleft()
        self._verdict = _verdict(_smooth([m for _, m in self._window]), self._verdict)

    def reset(self) -> None:
        """Forget everything seen so far (e.g. the receiver went away)."""
        self._window.clear()
        self._verdict = None

    def current(self, now: float) -> SignalQualityVerdict | None:
        """The verdict at monotonic time *now*, or ``None`` with no fresh data.

        A verdict whose newest Snapshot is :data:`STALE_AFTER_SECONDS` old
        is never returned: stale is no data, not an old verdict.
        """
        if not self._window or now - self._window[-1][0] >= STALE_AFTER_SECONDS:
            return None
        return self._verdict
