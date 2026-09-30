"""Signal Quality models — vendor-neutral.

A **Signal Snapshot** is the C/N0 of every signal the receiver would put
in its corrections at one epoch (signals from satellites above its
elevation mask), each tagged with constellation, satellite and band
group.  It is the only input a **Signal Quality** verdict is judged
from, whichever receiver or stream supplied it.  See CONTEXT.md.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Band(str, enum.Enum):
    """Band group a signal belongs to.  Only L1 and L2 are judged."""

    L1 = "L1"  # L1, E1, B1
    L2 = "L2"  # L2, E5b, B2
    OTHER = "other"  # L5 / E5a / B2a, E6, B3, …


class Signal(BaseModel):
    """One tracked signal in a Signal Snapshot."""

    model_config = ConfigDict(frozen=True)

    constellation: str
    satellite: int
    band: Band
    cn0_dbhz: float


class SignalSnapshot(BaseModel):
    """Every signal the receiver would put in its corrections, at one epoch."""

    model_config = ConfigDict(frozen=True)

    captured_at: datetime
    signals: tuple[Signal, ...]


SignalLevel = Literal["Good", "Marginal", "Poor"]


class SignalQualityVerdict(BaseModel):
    """A judged Signal Quality: the verdict, its cause and the three measures."""

    model_config = ConfigDict(frozen=True)

    level: SignalLevel
    l1_level: SignalLevel
    l2_level: SignalLevel
    satellites_level: SignalLevel
    cause: str  # empty when Good, e.g. "L2 weak, few satellites"
    l1_strength_dbhz: float | None  # None when the band has no signals
    l2_strength_dbhz: float | None
    usable_satellites: int


class SignalQualityNoData(BaseModel):
    """No Signal Quality to show, and why, in words for the operator."""

    model_config = ConfigDict(frozen=True)

    reason: str


SignalQualityReading = SignalQualityVerdict | SignalQualityNoData
