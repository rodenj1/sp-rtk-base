"""Whether a version is healthy after the update unit restarted it (ADR 0005).

Healthy means: within :data:`HEALTH_TIMEOUT_S` of the restart, ``/api/health``
on localhost answers with SP-Base X **and** Relay Y; it still answers
:data:`HEALTH_HOLD_S` later; and systemd's ``NRestarts`` for
``sp-rtk-base.service`` hasn't gone up. Whether the Relay resumes or
corrections flow is not checked: a slow Bluetooth reconnect isn't the
release's fault.

The app listens where ``sp-rtk-base.service`` tells it to
(``SP_RTK_BASE_HOST`` and ``SP_RTK_BASE_PORT``, which a drop-in can set):
:func:`app_health_url` reads them from systemd, as ``main`` does from its
environment.

Run by the updater, from the old version's code, for the new version and,
after a Rollback, for the old one. Importable without the web app.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sp_rtk_base.update.host_setup import unit_environment
from sp_rtk_base.update.release import Fetch
from sp_rtk_base.update.state import Versions

HOST_ENV = "SP_RTK_BASE_HOST"
PORT_ENV = "SP_RTK_BASE_PORT"
DEFAULT_PORT = 8080
"""The app's defaults, as in ``sp_rtk_base.main``: all addresses, 8080."""
HEALTH_URL = f"http://127.0.0.1:{DEFAULT_PORT}/api/health"
_LOOPBACK_FOR = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}
"""Where to reach an app bound to every address."""
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


def app_health_url(environment: Mapping[str, str]) -> str:
    """``/api/health`` where an app started with ``environment`` listens.

    Mirrors ``sp_rtk_base.main``: a missing or unreadable port is 8080;
    an app bound to every address is reached on loopback.
    """
    try:
        port = int(environment.get(PORT_ENV, str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT
    host = environment.get(HOST_ENV, "").strip()
    host = _LOOPBACK_FOR.get(host, host)
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/api/health"


def systemd_health_url(systemctl: str = "/usr/bin/systemctl") -> str:
    """:func:`app_health_url` for ``sp-rtk-base.service``'s environment as
    systemd has it (drop-ins included); the default when it can't be read."""
    try:
        shown = subprocess.run(
            [systemctl, "show", "--property=Environment", "--value", APP_UNIT],
            capture_output=True,
            text=True,
            check=True,
            timeout=_REQUEST_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return HEALTH_URL
    return app_health_url(unit_environment(shown.stdout.strip()))


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
