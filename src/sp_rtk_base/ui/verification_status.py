"""Status copy every kind of Verification shares in the UI."""

from __future__ import annotations

import math
from datetime import datetime, timezone


def countdown_label(expires_at: datetime, now: datetime | None = None) -> str | None:
    """How much of the Green is left, as a short label.

    Args:
        expires_at: When the Green stops standing (absolute, UTC).
        now: Override for the current moment; defaults to now (UTC).

    Returns:
        A label like ``"12s"``, or ``None`` once the Green has expired.
        Rounds **up**, so a Green that is still valid never renders as
        ``"0s"`` — a countdown that reads zero while the button still
        works would be worse than none at all.
    """
    moment = now or datetime.now(timezone.utc)
    remaining = (expires_at - moment).total_seconds()
    if remaining <= 0:
        return None
    return f"{math.ceil(remaining)}s"
