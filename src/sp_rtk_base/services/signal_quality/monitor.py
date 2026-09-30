"""SignalQualityMonitor: judges Signal Snapshots into a Signal Quality verdict.

Pure: no I/O, and the caller supplies the clock.  The whole Signal
Quality rule lives here, so it is the one place to review when the
provisional thresholds are revisited.
"""

from __future__ import annotations

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


def judge(snapshot: SignalSnapshot) -> SignalQualityVerdict:
    """Judge one Signal Snapshot: the worst of the three measures wins."""
    l1 = _band_strength(snapshot, Band.L1)
    l2 = _band_strength(snapshot, Band.L2)
    satellites = _usable_satellites(snapshot)

    l1_level = _level(l1, L1_GOOD_DBHZ, L1_MARGINAL_DBHZ)
    l2_level = _level(l2, L2_GOOD_DBHZ, L2_MARGINAL_DBHZ)
    satellites_level = _level(float(satellites), SATELLITES_GOOD, SATELLITES_MARGINAL)
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
    """Turns a stream of Signal Snapshots into the current verdict."""

    def __init__(self) -> None:
        self._verdict: SignalQualityVerdict | None = None

    def observe(self, snapshot: SignalSnapshot, now: float) -> None:
        """Take in a Snapshot received at monotonic time *now*."""
        del now  # used once smoothing and staleness arrive
        self._verdict = judge(snapshot)

    def reset(self) -> None:
        """Forget everything seen so far (e.g. the receiver went away)."""
        self._verdict = None

    def current(self, now: float) -> SignalQualityVerdict | None:
        """The verdict at monotonic time *now*, or ``None`` with no data."""
        del now
        return self._verdict
