"""The Good / Marginal / Poor level a verdict comes to.

Shared by every feature that judges something into those three levels
(Signal Quality, Bluetooth link strength), so none of them borrows
another's name for it.
"""

from __future__ import annotations

from typing import Literal

Level = Literal["Good", "Marginal", "Poor"]

# Each level as a number, for Prometheus gauges: higher is worse.
LEVEL_VALUE: dict[Level, int] = {"Good": 0, "Marginal": 1, "Poor": 2}
