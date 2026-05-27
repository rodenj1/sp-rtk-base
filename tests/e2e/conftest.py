"""Shared fixtures for the e2e (Playwright) test suite.

The fixtures here launch the **real** SP-Base server in a subprocess on
a random free port and an isolated temp ``$HOME`` directory so that
each test session gets:

- A clean ``~/.config/sp-rtk-base/config.yaml``
- No collision with any developer's running instance on port 8080
- A fully real FastAPI + NiceGUI process (subprocess) — not an
  in-process import, because NiceGUI keeps module-level singleton
  state and re-importing it inside the test process is unsafe.

The ``base_url`` fixture is consumed by ``pytest-playwright`` (its
``page`` fixture auto-navigates relative URLs against ``base_url``)
and by the ``api_base_url`` fixture below for direct REST calls.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest


def _find_free_port() -> int:
    """Return an OS-assigned free TCP port on localhost.

    Uses ``bind(("", 0))`` which lets the kernel pick an unused port.
    There's a small race between releasing the socket and the server
    grabbing the port, but for sequential e2e runs that's acceptable.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_http(url: str, timeout: float = 30.0) -> None:
    """Poll ``url`` until it returns any HTTP response or timeout elapses.

    Args:
        url: Health-check URL to GET.
        timeout: Maximum wait time in seconds.

    Raises:
        TimeoutError: if the server doesn't respond within ``timeout``.
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=2.0)
            if resp.status_code < 500:
                return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.25)
    raise TimeoutError(
        f"Server did not respond at {url} within {timeout}s (last error: {last_exc!r})"
    )


@pytest.fixture(scope="session")
def sp_rtk_base_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Launch the SP-Base server in a subprocess for the test session.

    Yields:
        The base URL (``http://127.0.0.1:<port>``) of the running server.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Isolated $HOME so the test doesn't pollute the developer's
    # ~/.config/sp-rtk-base/config.yaml.
    fake_home = tmp_path_factory.mktemp("e2e-home")
    config_dir = fake_home / ".config" / "sp-rtk-base"
    config_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["SP_RTK_BASE_HOST"] = "127.0.0.1"
    env["SP_RTK_BASE_PORT"] = str(port)
    # Force a known config path even if the user's shell exported one.
    env["SP_RTK_BASE_CONFIG"] = str(config_dir / "config.yaml")
    # Register the in-memory FakeGpsDriver so tests can drive UI paths
    # gated on a connected GPS device (Survey-In, GPS Config, Dashboard
    # GPS card, …) without real hardware.  See
    # ``services/drivers/fake.py`` for the driver and
    # ``docs/e2e-testing.md`` for the e2e usage pattern.
    env["SP_RTK_BASE_FAKE_GPS"] = "1"
    # NiceGUI's ui.run() flips into "screen test" mode when it detects
    # any of these pytest env vars (see nicegui.helpers.is_pytest and
    # nicegui.ui_run.run).  We're running the server as a real
    # subprocess, NOT as a pytest fixture, so strip them before
    # spawning so NiceGUI binds to our configured port instead of
    # raising ``KeyError: 'NICEGUI_SCREEN_TEST_PORT'``.
    for var in (
        "PYTEST_CURRENT_TEST",
        "PYTEST_VERSION",
        "PYTEST_XDIST_WORKER",
        "NICEGUI_USER_SIMULATION",
    ):
        env.pop(var, None)

    cmd = [sys.executable, "-m", "sp_rtk_base.main"]

    # Stream the server's combined stdout+stderr to a log file in the
    # session tmp dir.  Using ``subprocess.PIPE`` without an active
    # reader risks the OS pipe buffer filling up and stalling NiceGUI's
    # uvicorn worker.  Pointing at a real file is both robust and gives
    # us a debuggable artifact when a test fails in CI.
    log_path = fake_home / "sp-rtk-base.log"
    log_fh = log_path.open("wb")
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )

    try:
        try:
            _wait_for_http(f"{base_url}/api/health", timeout=45.0)
        except TimeoutError:
            # Surface the server log so CI failures aren't blind.
            log_fh.flush()
            try:
                tail = log_path.read_text(errors="replace")[-4000:]
            except Exception:
                tail = "(could not read server log)"
            raise TimeoutError(
                f"sp-rtk-base did not become healthy on {base_url}.\n"
                f"--- server log (tail) ---\n{tail}"
            ) from None
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
        log_fh.close()


@pytest.fixture(scope="session")
def base_url(sp_rtk_base_server: str) -> str:
    """``pytest-playwright`` base URL for relative page navigations."""
    return sp_rtk_base_server


@pytest.fixture(scope="session")
def api_base_url(sp_rtk_base_server: str) -> str:
    """Base URL for direct REST API calls inside e2e tests."""
    return sp_rtk_base_server


@pytest.fixture(scope="session")
def browser_context_args(
    browser_context_args: dict[str, object],
) -> dict[str, object]:
    """Override ``pytest-playwright``'s default browser context args.

    A 1280×800 viewport is large enough that the NiceGUI left drawer
    auto-expands on desktop (breakpoint=1024) so navigation links are
    immediately visible without toggling the hamburger menu.
    """
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 800},
        "ignore_https_errors": True,
    }


@pytest.fixture()
def clean_config(api_base_url: str) -> Iterator[None]:
    """Delete all destinations and saved positions before & after a test.

    Use this when a test needs to assert on "empty state" or wants to
    avoid cross-test pollution.  Goes through the REST API rather than
    poking the YAML file directly so the running server's in-memory
    cache stays consistent.
    """

    def _extract_names(payload: object) -> list[str]:
        """Extract destination names from a JSON response defensively."""
        raw: object = payload
        if isinstance(payload, dict):
            raw = cast(dict[str, Any], payload).get("destinations", [])
        if not isinstance(raw, list):
            return []
        names: list[str] = []
        for item in cast(list[Any], raw):  # type: ignore[redundant-cast]
            if isinstance(item, dict):
                value = cast(dict[str, Any], item).get("name")
                if isinstance(value, str) and value:
                    names.append(value)
        return names

    def _wipe() -> None:
        try:
            resp = httpx.get(f"{api_base_url}/api/destinations", timeout=5.0)
            for name in _extract_names(resp.json()):
                httpx.delete(
                    f"{api_base_url}/api/destinations/{name}",
                    timeout=5.0,
                )
        except Exception:
            # Endpoint may not exist on all branches; the e2e suite is
            # tolerant — failing tests will surface real issues.
            pass

    _wipe()
    yield
    _wipe()


@pytest.fixture()
def temp_config_path(tmp_path: Path) -> Path:
    """Return a per-test temp config file path (useful for API-only tests)."""
    p = tmp_path / "sp-rtk-base" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Fake GPS connection fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def connected_gps(api_base_url: str) -> Iterator[None]:
    """Connect the running server to the in-memory ``FakeGpsDriver``.

    Posts to ``/api/device/connect`` with ``vendor="fake"`` so the
    server's :class:`DeviceService` switches to the fake driver and
    every UI path gated on ``device_service.is_connected`` becomes
    reachable: Survey-In card, GPS Config page, Dashboard GPS card,
    live position, GNSS constellation toggles, base-config read-back,
    etc.

    The fixture cleans up by posting to ``/api/device/disconnect`` on
    teardown so each test starts from a clean disconnected state.
    ``409 Conflict`` on either endpoint is silently ignored — that
    just means the server was already in the desired state and we
    don't want a flaky teardown to mask a real test failure.

    Usage::

        def test_my_thing(page: Page, base_url: str, connected_gps: None) -> None:
            page.goto(f"{base_url}/survey")
            ...
    """
    payload = {"vendor": "fake", "port": "FAKE", "baud_rate": 115200}

    # In case a previous test crashed mid-flight without running
    # teardown, force-disconnect first.  Don't assert.
    try:
        httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
    except Exception:
        pass

    resp = httpx.post(
        f"{api_base_url}/api/device/connect",
        json=payload,
        timeout=10.0,
    )
    # 409 means "already connected" — re-use the connection rather
    # than failing the test.  Anything else is fatal.
    if resp.status_code not in (200, 409):
        raise RuntimeError(
            f"Could not connect FakeGpsDriver: HTTP {resp.status_code} — {resp.text}"
        )

    try:
        yield
    finally:
        try:
            httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
        except Exception:
            # Teardown is best-effort; don't mask the test result.
            pass
