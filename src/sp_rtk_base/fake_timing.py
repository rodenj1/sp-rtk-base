"""A timing the e2e tests may shorten: honoured only on a fake host.

The e2e server runs the app against fakes (the fake GPS, the fake Network
link host) and shortens a few timings so its tests wait less.  Each override
is read once, at startup, and only while the fake it belongs to is enabled,
so a production base station always keeps its defaults.  Not a user setting.
"""

from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger(__name__)


def fake_timing(
    env: str,
    default: float,
    *,
    gate_env: str,
    gate_value: str | None = None,
    allow_zero: bool = False,
) -> float:
    """*env*'s seconds while *gate_env* enables the fake, else *default*.

    The gate is on when *gate_env* equals *gate_value*, or, with no
    *gate_value*, when it is set at all.  An override that isn't a positive
    number of seconds is logged and ignored: a zero read or poll interval
    would spin its loop.  *allow_zero* lets 0 stand for a timing where it
    means "none" (no settling time) rather than a loop with no pause.
    """
    gate = os.environ.get(gate_env)
    gated_on = gate == gate_value if gate_value is not None else bool(gate)
    value = os.environ.get(env)
    if not gated_on or not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        seconds = math.nan
    if not (seconds >= 0 if allow_zero else seconds > 0):
        logger.warning("Ignoring %s=%r: not a usable number of seconds", env, value)
        return default
    return seconds
