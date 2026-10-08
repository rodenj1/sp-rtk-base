"""Host setup: does this host have what a release needs to be installed by Update?

Update never rewrites the host's unit files (ADR 0005). A release that
needs different ones raises the **plumbing version**, the integer in
``deploy/plumbing-version``, and the operator re-runs ``install.sh`` once.

- **What the host has:** ``install.sh`` writes the number into
  ``sp-rtk-base-update.service`` as ``Environment=SP_RTK_BASE_PLUMBING=N``.
  The app reads it with ``systemctl show`` (:func:`read_host_setup`); the
  updater, which that unit runs, reads its own environment
  (:func:`host_plumbing_from_env`). A host without the unit has 0.
- **What a release needs:** ``deploy/plumbing-version`` at the release's
  tag (:func:`required_plumbing`).
- **What the running app needs:** :data:`PLUMBING_VERSION`.

Like ``release``, this module stays importable without NiceGUI, FastAPI or
the app's services: the updater runs it on its own.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict

from sp_rtk_base.update.release import APP_PACKAGE, Fetch, github_raw_url

PLUMBING_VERSION = 1
"""The Host setup this version of SP-Base needs; ``deploy/plumbing-version``
(a unit test keeps the two equal)."""

PLUMBING_ENV = "SP_RTK_BASE_PLUMBING"
"""Set in the update unit by ``install.sh``: the host's plumbing version."""

PLUMBING_PATH = "deploy/plumbing-version"

UPDATE_SERVICE = "sp-rtk-base-update.service"
UPDATE_PATH_UNIT = "sp-rtk-base-update.path"

INSTALL_COMMAND = (
    "curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/"
    "deploy/install.sh | sudo bash"
)
"""The one-time step that brings a host's Host setup up to date: a bare
``install.sh`` re-run, which keeps the mode (and a turned-off Update off)."""

HOST_REQUIREMENTS_ERROR = "Couldn't check this release's host requirements."

FAKE_HOST_SETUP_ENV = "SP_RTK_BASE_FAKE_HOST_SETUP"
"""Names a JSON file the app reads its Host setup from instead of systemd (e2e)."""

_ENABLED_STATES = frozenset({"enabled", "enabled-runtime"})

Systemctl = Callable[[Sequence[str]], str]
"""Run ``systemctl <args>`` and return its stdout; raise if it fails."""


class HostSetup(BaseModel):
    """The Update units on this host, as systemd reports them."""

    model_config = ConfigDict(frozen=True)

    installed: bool
    """Both Update units are there."""
    enabled: bool
    """The path unit is enabled: Update isn't turned off on this host."""
    plumbing: int
    """The host's plumbing version; 0 without the units."""


class HostRequirementError(Exception):
    """A release's Host setup requirement couldn't be read."""


NO_HOST_SETUP = HostSetup(installed=False, enabled=False, plumbing=0)


def run_systemctl(args: Sequence[str]) -> str:
    """The real :data:`Systemctl`."""
    done = subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, check=True, timeout=10
    )
    return done.stdout


def read_host_setup(systemctl: Systemctl = run_systemctl) -> HostSetup:
    """The Update units' state, through ``systemctl show`` (read-only).

    systemctl failing, or missing, counts as no Host setup.
    """
    try:
        service = _show(systemctl, UPDATE_SERVICE)
        path = _show(systemctl, UPDATE_PATH_UNIT)
    except (OSError, subprocess.SubprocessError):
        return NO_HOST_SETUP
    if "not-found" in (service.get("LoadState"), path.get("LoadState")):
        return NO_HOST_SETUP
    return HostSetup(
        installed=True,
        enabled=path.get("UnitFileState") in _ENABLED_STATES,
        plumbing=_plumbing_in(service.get("Environment", "")),
    )


def host_plumbing_from_env(environ: Mapping[str, str] | None = None) -> int:
    """The updater's view: the plumbing version its unit sets, else 0."""
    env = os.environ if environ is None else environ
    return _as_plumbing(env.get(PLUMBING_ENV, ""))


def required_plumbing(fetch: Fetch, app_version: str) -> int:
    """The Host setup SP-Base ``app_version`` needs: its tag's
    ``deploy/plumbing-version``.

    Raises:
        HostRequirementError: it couldn't be fetched, or isn't a number.
    """
    url = github_raw_url(APP_PACKAGE, app_version, PLUMBING_PATH)
    try:
        body = fetch(url)
    except Exception as exc:
        raise HostRequirementError(f"Couldn't fetch {url}: {exc}") from exc
    try:
        value = int(body.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise HostRequirementError(f"{url} isn't a plumbing version") from exc
    if value < 0:
        raise HostRequirementError(f"{url} isn't a plumbing version")
    return value


def host_setup_error(required: int, host: int) -> str:
    """Why the updater refused a target needing newer Host setup."""
    return (
        f"This release needs a one-time host setup step (Host setup {required}; "
        f"this host has {host}). Run this on the base: {INSTALL_COMMAND}"
    )


def host_setup_reader_from_env() -> Callable[[], HostSetup]:
    """How the app reads its Host setup: systemd, or the fake e2e host when
    ``SP_RTK_BASE_FAKE_HOST_SETUP`` names a file."""
    fake = os.environ.get(FAKE_HOST_SETUP_ENV)
    if fake:
        return _file_host_setup(Path(fake))
    return read_host_setup


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _show(systemctl: Systemctl, unit: str) -> dict[str, str]:
    out = systemctl(["show", unit, "-p", "LoadState,UnitFileState,Environment"])
    found: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            found[key] = value
    return found


def unit_environment(environment: str) -> dict[str, str]:
    """The variables in a unit's ``Environment`` property, as ``systemctl
    show`` prints it (space-separated, shell-quoted); none if unparseable."""
    try:
        words = shlex.split(environment)
    except ValueError:
        return {}
    found: dict[str, str] = {}
    for word in words:
        key, sep, value = word.partition("=")
        if sep:
            found[key] = value
    return found


def _plumbing_in(environment: str) -> int:
    return _as_plumbing(unit_environment(environment).get(PLUMBING_ENV, "0"))


def _as_plumbing(value: str) -> int:
    try:
        number = int(value.strip())
    except ValueError:
        return 0
    return max(number, 0)


def _file_host_setup(path: Path) -> Callable[[], HostSetup]:
    """The fake host: ``{"installed", "enabled", "plumbing"}`` read from
    ``path`` on every call; no file is a host set up for this version."""

    set_up: dict[str, object] = {
        "installed": True,
        "enabled": True,
        "plumbing": PLUMBING_VERSION,
    }

    def read() -> HostSetup:
        try:
            data: object = json.loads(path.read_text())
        except FileNotFoundError:
            data = {}
        return HostSetup.model_validate({**set_up, **cast("dict[str, object]", data)})

    return read
