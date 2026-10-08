"""Whether a version is healthy after the update unit restarted it (ADR 0005).

Healthy means: within :data:`HEALTH_TIMEOUT_S` of the restart, ``/api/health``
on localhost answers with SP-Base X **and** Relay Y; it still answers
:data:`HEALTH_HOLD_S` later; and systemd's ``NRestarts`` for
``sp-rtk-base.service`` hasn't gone up. Whether the Relay resumes or
corrections flow is not checked: a slow Bluetooth reconnect isn't the
release's fault.

Run by the updater, from the old version's code, for the new version and,
after a Rollback, for the old one. Importable without the web app.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from sp_rtk_base.update.release import Fetch
from sp_rtk_base.update.state import Versions

HEALTH_URL = "http://127.0.0.1:8080/api/health"
HEALTH_TIMEOUT_S = 90.0
"""How long the restarted version has to answer with the right versions."""
HEALTH_HOLD_S = 30.0
"""How long after that it must still answer."""
APP_UNIT = "sp-rtk-base.service"
_REQUEST_TIMEOUT_S = 5.0


def local_fetch(url: str) -> bytes:
    """GET ``url`` directly, never through a proxy (it is localhost)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=_REQUEST_TIMEOUT_S) as response:
        body: bytes = response.read()
    return body


def systemd_restarts(systemctl: str = "/usr/bin/systemctl") -> Callable[[], int | None]:
    """Reads ``NRestarts`` of the app's unit, or ``None`` when it can't."""

    def read() -> int | None:
        try:
            shown = subprocess.run(
                [systemctl, "show", "--property=NRestarts", "--value", APP_UNIT],
                capture_output=True,
                text=True,
                check=True,
                timeout=_REQUEST_TIMEOUT_S,
            )
            return int(shown.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    return read


_SYSTEMD_RESTARTS = systemd_restarts()


@dataclass(frozen=True)
class HealthCheck:
    """Probes ``url`` for the expected versions; see the module docstring."""

    url: str = HEALTH_URL
    timeout_s: float = HEALTH_TIMEOUT_S
    hold_s: float = HEALTH_HOLD_S
    fetch: Fetch = local_fetch
    restarts: Callable[[], int | None] = _SYSTEMD_RESTARTS
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def __call__(self, expected: Versions) -> str | None:
        """``None`` when ``expected`` is healthy, else why not, in words."""
        name = f"SP-Base {expected.app}"
        poll_s = min(1.0, self.timeout_s / 10)
        before = self.restarts()
        deadline = self.clock() + self.timeout_s
        while (problem := self._probe(expected)) is not None:
            if self.clock() >= deadline:
                return f"{name} didn't start within {self.timeout_s:g} s: {problem}"
            self.sleep(poll_s)
        self.sleep(self.hold_s)
        problem = self._probe(expected)
        if problem is not None:
            return f"{name} started, then stopped answering: {problem}"
        after = self.restarts()
        if before is not None and after is not None and after > before:
            return f"{name} started, but systemd restarted it {after - before} time(s)."
        return None

    def _probe(self, expected: Versions) -> str | None:
        try:
            body = json.loads(self.fetch(self.url))
        except (OSError, ValueError) as exc:
            return f"no answer from {self.url} ({exc})"
        if not isinstance(body, dict):
            return f"{self.url} answered {body!r}"
        app = body.get("version")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        relay = body.get("relay_version")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        if (app, relay) != (expected.app, expected.relay):
            return f"it reports SP-Base {app}, Relay {relay}"
        return None
