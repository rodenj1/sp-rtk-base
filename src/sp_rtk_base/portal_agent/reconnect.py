"""When the agent tries its tunnel again after a drop (ADR 0006, #206).

A Portal restart drops every base's tunnel at once. So that they don't all
come back at once, the agent waits a random 0-60 s before its first retry;
each failed open after that doubles the window the next wait is drawn from
(exponential backoff with full jitter), up to the backoff cap from the agent
config (~10 min). A refusal's ``Retry-After`` (the Portal's 503 when it is
admitting too many opens) is a floor on the next wait. A tunnel that opens
starts it all over.
"""

from __future__ import annotations

import random
from collections.abc import Callable

FIRST_RETRY_S = 60
"""The window the first retry after a drop is drawn from."""
DEFAULT_CAP_S = 600


class Reconnect:
    """The backoff between tries to open the held tunnel."""

    def __init__(
        self,
        cap: Callable[[], float] = lambda: DEFAULT_CAP_S,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._cap = cap
        self._uniform = uniform
        self.failures = 0
        """Tries that failed in a row since the tunnel last opened."""

    def opened(self) -> None:
        self.failures = 0

    def failed(self) -> None:
        self.failures += 1

    def delay(self, retry_after: float | None = None) -> float:
        """Seconds to wait before the next try."""
        window = min(self._cap(), FIRST_RETRY_S * 2 ** min(self.failures, 32))
        return max(self._uniform(0, window), retry_after or 0)
