"""``sp-rtk-base-apply-update``: install the Update the app asked for.

Run by ``sp-rtk-base-update.service`` (``deploy/``) as the service user,
outside the app's sandbox, when ``sp-rtk-base-update.path`` sees a request
file. See ADR 0005. The unit runs it three ways:

- with no option, first: take the request, resolve the target, refuse
  unless it is the request's SP-Base and Relay, then install exactly
  ``sp-rtk-base==X sp-rtk-base-relay==Y``, once the target's Host setup
  (``deploy/plumbing-version`` at its tag) is no newer than this host's
  (``SP_RTK_BASE_PLUMBING`` in the unit). Exits non-zero on any refusal
  or failure, so the unit's root restart lines never run after one;
- ``--finish``, after the unit has restarted the app: reports ``done``;
- ``--stopped``, as ``ExecStopPost``, whatever happened: an Update left
  half-way (a timeout, a failed restart, a crash) is reported ``failed``.

Every phase is written to ``status.json``. Nothing from the request file
ever reaches pip: pip only sees the versions resolved here.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from sp_rtk_base.update.fake_release_source import fetch_from_env
from sp_rtk_base.update.host_setup import (
    HOST_REQUIREMENTS_ERROR,
    HostRequirementError,
    host_plumbing_from_env,
    host_setup_error,
    required_plumbing,
)
from sp_rtk_base.update.release import (
    APP_PACKAGE,
    RELAY_PACKAGE,
    Fetch,
    PythonVersion,
    ReleaseCheckError,
    resolve_release,
)
from sp_rtk_base.update.state import (
    NEWER_RELEASE_ERROR,
    REASON_BAD_REQUEST,
    REASON_CHECK_FAILED,
    REASON_HOST_REQUIREMENTS,
    REASON_HOST_SETUP,
    REASON_INSTALL_FAILED,
    REASON_NEWER_RELEASE,
    REASON_STOPPED,
    UpdateFiles,
    UpdateStatus,
    Versions,
)

logger = logging.getLogger(__name__)

VENV_ENV = "SP_RTK_BASE_UPDATE_VENV"
"""Overrides the venv pip installs into (tests); defaults to this one."""

_PIP_ERROR_LINES = 5
"""How much of pip's stderr goes into ``error``."""

# Exit codes. Any non-zero one stops the unit before its restart lines.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOTHING_TO_DO = 2


def installed_versions() -> Versions:
    """The SP-Base and Relay installed in this venv."""
    return Versions(
        app=importlib.metadata.version(APP_PACKAGE),
        relay=importlib.metadata.version(RELAY_PACKAGE),
    )


class Updater:
    """One run of the updater against an update directory and a venv."""

    def __init__(
        self,
        files: UpdateFiles,
        *,
        fetch: Fetch,
        python: PythonVersion,
        venv: Path,
        installed: Callable[[], Versions] = installed_versions,
        host_plumbing: int | None = None,
    ) -> None:
        self._files = files
        self._fetch = fetch
        self._python = python
        self._venv = venv
        self._installed = installed
        self._host_plumbing = (
            host_plumbing if host_plumbing is not None else host_plumbing_from_env()
        )

    def apply(self) -> int:
        """Take the request and install the target it names, or refuse."""
        try:
            request = self._files.take_request()
        except ValueError as exc:
            self._fail(REASON_BAD_REQUEST, str(exc))
            return EXIT_FAILED
        if request is None:
            logger.info("No update request; nothing to do")
            return EXIT_NOTHING_TO_DO

        from_ = self._installed()
        self._files.write_status(UpdateStatus(phase="resolving", from_=from_))
        try:
            target = resolve_release(self._fetch, self._python)
        except ReleaseCheckError as exc:
            self._fail(REASON_CHECK_FAILED, str(exc), from_=from_)
            return EXIT_FAILED
        to = Versions(app=target.app, relay=target.relay)
        if (request.app, request.relay) != (to.app, to.relay):
            logger.warning(
                "Requested %s/%s but the target is %s/%s",
                request.app,
                request.relay,
                to.app,
                to.relay,
            )
            self._fail(REASON_NEWER_RELEASE, NEWER_RELEASE_ERROR, from_=from_, to=to)
            return EXIT_FAILED
        host_refusal = self._host_refusal(to)
        if host_refusal is not None:
            self._fail(*host_refusal, from_=from_, to=to)
            return EXIT_FAILED

        self._files.write_status(UpdateStatus(phase="installing", from_=from_, to=to))
        pip = subprocess.run(
            [
                str(self._venv / "bin" / "pip"),
                "install",
                "--quiet",
                "--disable-pip-version-check",
                f"{APP_PACKAGE}=={to.app}",
                f"{RELAY_PACKAGE}=={to.relay}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if pip.returncode != 0:
            tail = "\n".join(pip.stderr.strip().splitlines()[-_PIP_ERROR_LINES:])
            self._fail(
                REASON_INSTALL_FAILED,
                f"pip failed (exit {pip.returncode}): {tail}",
                from_=from_,
                to=to,
            )
            return EXIT_FAILED

        self._files.write_status(UpdateStatus(phase="restarting", from_=from_, to=to))
        return EXIT_OK

    def _host_refusal(self, to: Versions) -> tuple[str, str] | None:
        """``(reason, error)`` when this host's Host setup can't take ``to``."""
        try:
            required = required_plumbing(self._fetch, to.app)
        except HostRequirementError as exc:
            return REASON_HOST_REQUIREMENTS, f"{HOST_REQUIREMENTS_ERROR} {exc}"
        if required > self._host_plumbing:
            return REASON_HOST_SETUP, host_setup_error(required, self._host_plumbing)
        return None

    def finish(self) -> int:
        """After the restart: the Update is done."""
        status = self._files.read_status()
        if status is None or status.phase != "restarting":
            logger.warning("No restarted Update to finish")
            return EXIT_FAILED
        self._files.write_status(
            UpdateStatus(phase="done", from_=status.from_, to=status.to)
        )
        return EXIT_OK

    def stopped(self, service_result: str) -> int:
        """The unit has stopped: an Update it left half-way has failed."""
        status = self._files.read_status()
        if status is None or status.phase not in (
            "resolving",
            "installing",
            "restarting",
        ):
            return EXIT_OK
        self._fail(
            REASON_STOPPED,
            f"The update stopped while {status.phase} ({service_result}).",
            from_=status.from_,
            to=status.to,
        )
        return EXIT_OK

    def _fail(
        self,
        reason: str,
        error: str,
        *,
        from_: Versions | None = None,
        to: Versions | None = None,
    ) -> None:
        logger.error("Update failed: %s", error)
        self._files.write_status(
            UpdateStatus(phase="failed", from_=from_, to=to, error=error, reason=reason)
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one step of the updater; return the exit code."""
    parser = argparse.ArgumentParser(prog="sp-rtk-base-apply-update")
    step = parser.add_mutually_exclusive_group()
    step.add_argument("--finish", action="store_true", help="report the Update done")
    step.add_argument(
        "--stopped", action="store_true", help="fail an Update the unit left half-way"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    venv = os.environ.get(VENV_ENV)
    updater = Updater(
        UpdateFiles(),
        fetch=fetch_from_env(),
        python=tuple(sys.version_info[:3]),
        venv=Path(venv) if venv else Path(sys.prefix),
    )
    if args.finish:
        return updater.finish()
    if args.stopped:
        return updater.stopped(os.environ.get("SERVICE_RESULT", "unknown"))
    return updater.apply()


def run() -> None:  # pragma: no cover - the console script's two lines
    """The ``sp-rtk-base-apply-update`` console script."""
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
