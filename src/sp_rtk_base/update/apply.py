"""``sp-rtk-base-apply-update``: install the Update the app asked for.

Run by ``sp-rtk-base-update.service`` (``deploy/``) as the service user,
outside the app's sandbox, when ``sp-rtk-base-update.path`` sees a request
file. See ADR 0005. The unit runs it three ways:

- with no option, first: take the request, resolve the target, refuse
  unless it is the request's SP-Base and Relay, refuse unless the target's
  Host setup (``deploy/plumbing-version`` at its tag) is no newer than this
  host's (``SP_RTK_BASE_PLUMBING`` in the unit), check there is room for
  the snapshot, take it (the venv to ``venv.prev``, the config dir to
  ``config.prev``), then install exactly ``sp-rtk-base==X
  sp-rtk-base-relay==Y``. A pip failure restores the snapshot. Exits
  non-zero on any refusal or failure, so the unit's root restart lines
  never run after one;
- ``--verify``, after the unit has restarted the app, run from
  ``venv.prev`` (the old version's code, so a release that fails on import
  can't stop its own Rollback): the new version is healthy (``done``), or
  the snapshot is restored, the rollback marker written and the exit is
  non-zero, so the unit's root ``ExecStopPost`` line restarts the old
  version;
- ``--stopped``, as ``ExecStopPost``, whatever happened, also run from
  ``venv.prev`` when there is one (so a broken release can't stop its own
  reporting or cleanup; removing ``venv.prev`` is its last step): after a Rollback
  it gives the old version the same health check; it removes the snapshot
  once the running version is healthy; and an Update left half-way (a
  timeout, a crash) is reported ``failed``. A Rollback gets one attempt:
  if the old version fails too, the snapshot is kept and both errors are
  reported.

Every phase is written to the updater's own record beside the venv
(``update-progress.json``, out of the app's reach), which decides what the
updater does, and reported to the app in ``status.json``, which never
does. Nothing from the request file ever reaches pip: pip only sees the
versions resolved here.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from sp_rtk_base.update.fake_release_source import fetch_from_env
from sp_rtk_base.update.health import (
    HEALTH_HOLD_S,
    HEALTH_TIMEOUT_S,
    HealthCheck,
    systemd_health_url,
    systemd_restarts,
)
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
from sp_rtk_base.update.snapshot import (
    DEFAULT_CONFIG_DIR,
    NotEnoughDiskSpaceError,
    Snapshot,
)
from sp_rtk_base.update.state import (
    NEWER_RELEASE_ERROR,
    PROGRESS_FILENAME,
    REASON_BAD_REQUEST,
    REASON_CHECK_FAILED,
    REASON_FAILED_TO_START,
    REASON_HOST_REQUIREMENTS,
    REASON_HOST_SETUP,
    REASON_INSTALL_FAILED,
    REASON_NEWER_RELEASE,
    REASON_NO_DISK_SPACE,
    REASON_SNAPSHOT_FAILED,
    REASON_STOPPED,
    ProgressRecord,
    UpdateFiles,
    UpdateRequest,
    UpdateStatus,
    Versions,
)

logger = logging.getLogger(__name__)

VENV_ENV = "SP_RTK_BASE_UPDATE_VENV"
"""Overrides the venv pip installs into (tests); defaults to this one."""
CONFIG_DIR_ENV = "SP_RTK_BASE_UPDATE_CONFIG_DIR"
"""Overrides the config dir the snapshot covers (tests)."""
HEALTH_URL_ENV = "SP_RTK_BASE_UPDATE_HEALTH_URL"
HEALTH_TIMEOUT_ENV = "SP_RTK_BASE_UPDATE_HEALTH_TIMEOUT_S"
HEALTH_HOLD_ENV = "SP_RTK_BASE_UPDATE_HEALTH_HOLD_S"
SYSTEMCTL_ENV = "SP_RTK_BASE_UPDATE_SYSTEMCTL"
"""Override the health check's endpoint (else read from the app's unit),
its 90 s and 30 s, and the ``systemctl`` it reads the app's environment
and ``NRestarts`` with (tests)."""

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
        config_dir: Path = DEFAULT_CONFIG_DIR,
        installed: Callable[[], Versions] = installed_versions,
        host_plumbing: int | None = None,
        health: Callable[[Versions], str | None] | None = None,
    ) -> None:
        self._files = files
        self._fetch = fetch
        self._python = python
        self._venv = venv
        self._installed = installed
        self._host_plumbing = (
            host_plumbing if host_plumbing is not None else host_plumbing_from_env()
        )
        self._snapshot = Snapshot(venv, config_dir)
        self._progress = ProgressRecord(venv.with_name(PROGRESS_FILENAME))
        self._health = health if health is not None else HealthCheck()
        self._current: UpdateStatus | None = None
        """What this run last reported."""

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
        try:
            return self._install(request)
        except Exception as exc:
            logger.exception("The updater crashed")
            status = self._current or UpdateStatus(phase="resolving")
            self._stopped_half_way(status, f"crashed: {exc!r}")
            return EXIT_FAILED

    def _install(self, request: UpdateRequest) -> int:
        from_ = self._installed()
        self._report(UpdateStatus(phase="resolving", from_=from_))
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

        try:
            self._snapshot.check_space()
        except NotEnoughDiskSpaceError as exc:
            self._fail(REASON_NO_DISK_SPACE, str(exc), from_=from_, to=to)
            return EXIT_FAILED
        try:
            self._snapshot.take()
        except OSError as exc:
            self._fail(
                REASON_SNAPSHOT_FAILED,
                f"Couldn't save the current version before updating: {exc}",
                from_=from_,
                to=to,
            )
            return EXIT_FAILED

        self._report(UpdateStatus(phase="installing", from_=from_, to=to))
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
            # Nothing restarted: the old app still runs, on restored files.
            self._restore_and_fail(
                REASON_INSTALL_FAILED,
                f"pip failed (exit {pip.returncode}): {tail}",
                from_=from_,
                to=to,
            )
            return EXIT_FAILED

        self._report(UpdateStatus(phase="restarting", from_=from_, to=to))
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

    def verify(self) -> int:
        """After the restart: ``done`` if the new version is healthy, else
        restore the snapshot and leave the marker for the unit's root
        ``ExecStopPost`` line, which restarts the old version."""
        status = self._progress.read()
        if status is None or status.phase != "restarting" or status.to is None:
            logger.warning("No restarted Update to verify")
            return EXIT_FAILED
        from_, to = status.from_, status.to
        self._report(UpdateStatus(phase="verifying", from_=from_, to=to))
        problem = self._check(to)
        if problem is None:
            self._report(
                UpdateStatus(phase="done", from_=from_, to=to, finished_at=_now())
            )
            return EXIT_OK
        logger.error("Rolling back: %s", problem)
        try:
            self._snapshot.restore()
        except OSError as exc:
            self._double_failure(problem, f"Restoring {_app(from_)} failed: {exc}")
            return EXIT_FAILED
        self._report(
            UpdateStatus(
                phase="rolling_back",
                from_=from_,
                to=to,
                error=problem,
                reason=REASON_FAILED_TO_START,
            )
        )
        self._files.mark_rollback()
        return EXIT_FAILED

    def stopped(self, service_result: str) -> int:
        """The unit has stopped. After a Rollback, check the old version;
        once the running version is healthy, remove the snapshot; and fail
        an Update left half-way."""
        marked = self._files.take_rollback_marker()
        status = self._progress.read()
        if status is None:
            return EXIT_OK
        if status.phase == "rolling_back" and marked:
            self._rolled_back(status)
        elif status.phase == "done":
            self._snapshot.remove()  # last: this may run from venv.prev
        elif not status.finished and status.phase != "requested":
            self._stopped_half_way(status, service_result)
        return EXIT_OK

    def _rolled_back(self, status: UpdateStatus) -> None:
        """The old version is back and restarted: it gets the same check."""
        error = status.error or "The new version failed to start."
        if status.from_ is None:  # pragma: no cover - the updater always records it
            self._double_failure(error, "The old version isn't known.", status)
            return
        problem = self._check(status.from_)
        if problem is not None:
            self._double_failure(error, problem, status)
            return
        self._report(
            status.model_copy(
                update={
                    "phase": "failed",
                    "rolled_back": True,
                    "finished_at": _now(),
                    "updated_at": _now(),
                }
            )
        )
        self._snapshot.remove()  # last: this may run from venv.prev

    def _stopped_half_way(self, status: UpdateStatus, service_result: str) -> None:
        error = f"The update stopped while {status.phase} ({service_result})."
        if status.phase == "installing" and self._snapshot.exists:
            self._restore_and_fail(
                REASON_STOPPED, error, from_=status.from_, to=status.to
            )
            return
        self._fail(REASON_STOPPED, error, from_=status.from_, to=status.to)

    def _report(self, status: UpdateStatus) -> None:
        """Record ``status`` in the updater's own record, then report it to
        the app in ``status.json``. The report goes out even when the
        record can't be written (``/opt`` read-only or full)."""
        self._current = status
        try:
            self._progress.write(status)
        except OSError:
            logger.exception("Couldn't record the phase %s", status.phase)
        self._files.write_status(status)

    def _check(self, expected: Versions) -> str | None:
        """The health check; a crash in it counts as unhealthy."""
        try:
            return self._health(expected)
        except Exception as exc:
            logger.exception("The health check crashed")
            return f"The health check failed: {exc!r}"

    def _restore_and_fail(
        self,
        reason: str,
        error: str,
        *,
        from_: Versions | None,
        to: Versions | None,
    ) -> None:
        """Put the snapshot back before the app restarted, then report."""
        try:
            self._snapshot.restore()
        except OSError as exc:
            self._fail(
                reason,
                error,
                from_=from_,
                to=to,
                rollback_error=f"Restoring {_app(from_)} failed: {exc}",
            )
            return
        self._snapshot.remove()
        self._fail(reason, error, from_=from_, to=to, rolled_back=True)

    def _double_failure(
        self, error: str, rollback_error: str, status: UpdateStatus | None = None
    ) -> None:
        """The Rollback failed too: one attempt only; the snapshot stays."""
        if status is None:
            status = self._progress.read()
        self._fail(
            REASON_FAILED_TO_START,
            error,
            from_=status.from_ if status is not None else None,
            to=status.to if status is not None else None,
            rollback_error=rollback_error,
        )

    def _fail(
        self,
        reason: str,
        error: str,
        *,
        from_: Versions | None = None,
        to: Versions | None = None,
        rolled_back: bool = False,
        rollback_error: str | None = None,
    ) -> None:
        logger.error("Update failed: %s", error)
        if rollback_error is not None:
            logger.error("Rollback failed: %s", rollback_error)
        self._report(
            UpdateStatus(
                phase="failed",
                from_=from_,
                to=to,
                error=error,
                reason=reason,
                rolled_back=rolled_back,
                rollback_error=rollback_error,
                finished_at=_now(),
            )
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _app(versions: Versions | None) -> str:
    return f"SP-Base {versions.app}" if versions is not None else "the old version"


def default_venv() -> Path:
    """The venv this runs from; the live one when that is ``venv.prev``
    (the unit's verify line)."""
    prefix = Path(sys.prefix)
    if prefix.name.endswith(".prev"):
        return prefix.with_name(prefix.name[: -len(".prev")])
    return prefix


def _float_env(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


def main(argv: Sequence[str] | None = None) -> int:
    """Run one step of the updater; return the exit code."""
    parser = argparse.ArgumentParser(prog="sp-rtk-base-apply-update")
    step = parser.add_mutually_exclusive_group()
    step.add_argument(
        "--verify",
        action="store_true",
        help="after the restart: check the new version, or roll back",
    )
    step.add_argument(
        "--stopped",
        action="store_true",
        help="after the unit: check a rolled-back version, clean up, fail a "
        "half-way Update",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    venv = os.environ.get(VENV_ENV)
    config_dir = os.environ.get(CONFIG_DIR_ENV)
    systemctl = os.environ.get(SYSTEMCTL_ENV) or "/usr/bin/systemctl"
    health = HealthCheck(
        url=os.environ.get(HEALTH_URL_ENV) or systemd_health_url(systemctl),
        timeout_s=_float_env(HEALTH_TIMEOUT_ENV, HEALTH_TIMEOUT_S),
        hold_s=_float_env(HEALTH_HOLD_ENV, HEALTH_HOLD_S),
        restarts=systemd_restarts(systemctl),
    )
    updater = Updater(
        UpdateFiles(),
        fetch=fetch_from_env(),
        python=tuple(sys.version_info[:3]),
        venv=Path(venv) if venv else default_venv(),
        config_dir=Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR,
        health=health,
    )
    if args.verify:
        return updater.verify()
    if args.stopped:
        return updater.stopped(os.environ.get("SERVICE_RESULT", "unknown"))
    return updater.apply()


def run() -> None:  # pragma: no cover - the console script's two lines
    """The ``sp-rtk-base-apply-update`` console script."""
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
