"""Starting an Update, and following it through ``status.json`` (ADR 0005).

The app's side of the request file: after its guards pass (no Update
already running, Host setup that fits the release, no Survey-in running,
no Console link connected), it writes
the ``requested`` status, then the request file, into the update
directory. The updater, in its own unit, answers in ``status.json``.

While an Update runs (any phase before ``done`` or ``failed``), the app is
in the Updating state: :meth:`UpdateService.updating` is what Start,
Survey-in and Console connect ask before they run.

If no phase follows ``requested`` within :data:`START_TIMEOUT_S`, the app
takes the request back and records that the Update didn't start, so a
path unit enabled later never starts a stale request.

An Update a power cut (or a reboot) cut off is never finished by the
updater; at startup the app fails it (:meth:`UpdateService.recover_interrupted`)
rather than stay Updating.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from sp_rtk_base import __version__ as app_version
from sp_rtk_base.services.update_check import running_relay_version
from sp_rtk_base.update.host_setup import (
    INSTALL_COMMAND,
    HostSetup,
    UpdateUnitState,
    read_host_setup,
    read_update_unit_state,
    update_unit_idle,
)
from sp_rtk_base.update.snapshot import DEFAULT_CONFIG_DIR, Snapshot
from sp_rtk_base.update.state import (
    REASON_DIDNT_START,
    REASON_INTERRUPTED,
    UpdateFiles,
    UpdateRequest,
    UpdateStatus,
    Versions,
    recovery_text,
)

logger = logging.getLogger(__name__)

START_TIMEOUT_S: float = 30.0
"""How long the host has to answer ``requested`` before the Update counts
as not started."""

DIDNT_START_ERROR = "The host didn't pick up the request within 30 s."

UpdateRefusal = Literal[
    "survey_running",
    "console_connected",
    "updating",
    "host_setup_missing",
    "update_turned_off",
    "host_setup_outdated",
    "host_requirements_unknown",
]

REFUSAL_MESSAGES: dict[UpdateRefusal, str] = {
    "survey_running": "A Survey-in is running. Update once it has finished.",
    "console_connected": "A Console link is connected. Disconnect it to update.",
    "updating": "An Update is already running.",
    # Host setup (sp-rtk-base#242), in this order of precedence.
    "host_setup_missing": (
        "Update needs a one-time setup on this host. Run this on the base, "
        "then come back:"
    ),
    "update_turned_off": "Update is turned off on this host.",
    "host_setup_outdated": (
        "This release needs a one-time host setup step. Run this on the base, "
        "then come back:"
    ),
    "host_requirements_unknown": (
        "Couldn't check this release's host requirements. Check again."
    ),
}

REFUSAL_COMMANDS: dict[UpdateRefusal, str] = {
    "host_setup_missing": INSTALL_COMMAND,
    "host_setup_outdated": INSTALL_COMMAND,
}
"""The command a refusal asks the operator to run on the base, if any."""


class UpdateRefusedError(Exception):
    """Update was refused before anything was written."""

    def __init__(self, code: UpdateRefusal) -> None:
        self.code: UpdateRefusal = code
        self.message = REFUSAL_MESSAGES[code]
        self.command: str | None = REFUSAL_COMMANDS.get(code)
        super().__init__(self.message)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _no_survey() -> bool:
    return False


def _snapshot_kept() -> bool:
    """Whether the updater's snapshot is still beside this venv."""
    return Snapshot(Path(sys.prefix), DEFAULT_CONFIG_DIR).exists


class UpdateService:
    """Requests an Update and reports where it is."""

    def __init__(
        self,
        files: UpdateFiles | None = None,
        *,
        running: Versions | None = None,
        clock: Callable[[], datetime] = _utc_now,
        start_timeout_s: float = START_TIMEOUT_S,
        host_setup: Callable[[], HostSetup] = read_host_setup,
        update_unit_state: UpdateUnitState = read_update_unit_state,
        snapshot_kept: Callable[[], bool] = _snapshot_kept,
    ) -> None:
        self.files = files if files is not None else UpdateFiles()
        self._running = running
        self._clock = clock
        self._start_timeout = timedelta(seconds=start_timeout_s)
        self._survey_running: Callable[[], Awaitable[bool]] = _no_survey
        self._survey_as_last_seen: Callable[[], bool] = lambda: False
        self._console_connected: Callable[[], bool] = lambda: False
        self._host_setup = host_setup
        self._host: HostSetup | None = None
        """The Host setup as last read: at startup and on Check now."""
        self._status_key: tuple[int, int, int] | None = None
        self._status: UpdateStatus | None = None
        """``status.json`` as last parsed, and the file it was parsed from."""
        self._acknowledged: tuple[datetime, bool] | None = None
        """Whether the outcome of that ``updated_at`` was dismissed."""
        self._update_unit_state = update_unit_state
        self._snapshot_kept = snapshot_kept
        self._host_requirement: Callable[[], int | None] = lambda: 0
        self._requested: Versions | None = None

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def set_survey_check(
        self,
        check: Callable[[], Awaitable[bool]],
        as_last_seen: Callable[[], bool],
    ) -> None:
        """Set what says whether a Survey-in is running: ``check`` asks the
        receiver (for Update itself); ``as_last_seen`` doesn't (for the
        page's poll)."""
        self._survey_running = check
        self._survey_as_last_seen = as_last_seen

    def set_console_check(self, check: Callable[[], bool]) -> None:
        """Set what says whether a Console link is connected."""
        self._console_connected = check

    def set_host_requirement(self, requirement: Callable[[], int | None]) -> None:
        """Set what says which Host setup the offered release needs
        (``None``: it couldn't be read)."""
        self._host_requirement = requirement

    async def host_setup(self) -> HostSetup:
        """This host's Host setup as last read (read now if never)."""
        if self._host is None:
            return await self.refresh_host_setup()
        return self._host

    async def refresh_host_setup(self) -> HostSetup:
        """Read this host's Host setup from systemd again: at startup and on
        Check now."""
        self._host = await asyncio.to_thread(self._host_setup)
        return self._host

    async def refusal(
        self, *, live: bool = False, updating: bool | None = None
    ) -> UpdateRefusedError | None:
        """Why Update would be refused now, or ``None``. Writes nothing.

        ``live``: read the Host setup and ask the receiver about a
        Survey-in now (Update itself); otherwise use what was last seen,
        which costs nothing (the page's poll). ``updating``: the Updating
        state, when the caller has just read it.
        """
        if updating is None:
            updating = self.updating()
        if updating:
            return UpdateRefusedError("updating")
        host = await self.refresh_host_setup() if live else await self.host_setup()
        host_refusal = self._host_refusal(host)
        if host_refusal is not None:
            return UpdateRefusedError(host_refusal)
        if await self._survey(live=live):
            return UpdateRefusedError("survey_running")
        if self._console_connected():
            return UpdateRefusedError("console_connected")
        return None

    async def _survey(self, *, live: bool) -> bool:
        if not live:
            return self._survey_as_last_seen()
        try:
            return await self._survey_running()
        except Exception as exc:  # the receiver can't be read: no survey here
            logger.debug("Survey-in state unreadable for the update guard: %s", exc)
            return False

    def _host_refusal(self, host: HostSetup) -> UpdateRefusal | None:
        if not host.installed:
            return "host_setup_missing"
        if not host.enabled:
            return "update_turned_off"
        required = self._host_requirement()
        if required is None:
            return "host_requirements_unknown"
        if required > host.plumbing:
            return "host_setup_outdated"
        return None

    # ------------------------------------------------------------------
    # Request
    # ------------------------------------------------------------------

    async def request(self, target: Versions) -> UpdateStatus:
        """Ask the host to update to ``target``, the versions the operator read.

        Writes the ``requested`` status first, so it never overwrites the
        host's answer to the request.

        Raises:
            UpdateRefusedError: a guard refused; nothing was written.
        """
        refused = await self.refusal(live=True)
        if refused is not None:
            raise refused
        status = UpdateStatus(
            phase="requested", from_=self.running, to=target, updated_at=self._clock()
        )
        self.files.write_status(status)
        self.files.write_request(
            UpdateRequest(
                app=target.app, relay=target.relay, requested_at=self._clock()
            )
        )
        self._requested = target
        logger.info("Update to %s (Relay %s) requested", target.app, target.relay)
        return status

    @property
    def running(self) -> Versions:
        """The SP-Base and Relay running here."""
        if self._running is None:
            self._running = Versions(app=app_version, relay=running_relay_version())
        return self._running

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> UpdateStatus | None:
        """The last Update's status, or ``None`` if there never was one.

        A ``requested`` the host left unanswered for too long becomes a
        ``failed`` with :data:`REASON_DIDNT_START`.
        """
        status = self._read_status()
        if status is None:
            return None
        if (
            status.phase == "requested"
            and self._clock() - status.updated_at > self._start_timeout
        ):
            status = self._didnt_start(status)
        if status.to is None and not status.finished and self._requested is not None:
            # The host names the target once it has resolved it.
            status = status.model_copy(update={"to": self._requested})
        return status

    def _read_status(self) -> UpdateStatus | None:
        """``status.json``, parsed again only when the file changed (every
        write replaces it): pages ask several times a second."""
        try:
            info = self.files.status_path.stat()
        except OSError:
            self._status_key, self._status = None, None
            return None
        key = (info.st_ino, info.st_mtime_ns, info.st_size)
        if key != self._status_key:
            self._status_key, self._status = key, self.files.read_status()
        return self._status

    def updating(self) -> bool:
        """Whether an Update is running: the Updating state."""
        status = self.status()
        return status is not None and not status.finished

    def _didnt_start(self, status: UpdateStatus) -> UpdateStatus:
        if self.files.take_request() is None:
            return status  # the host has just taken it, and will answer
        failed = UpdateStatus(
            phase="failed",
            from_=status.from_,
            to=status.to,
            error=DIDNT_START_ERROR,
            reason=REASON_DIDNT_START,
            finished_at=self._clock(),
            updated_at=self._clock(),
        )
        self.files.write_status(failed)
        logger.warning("Update didn't start: %s", DIDNT_START_ERROR)
        return failed

    async def recover_interrupted(self) -> None:
        """At startup: fail an Update the updater left unfinished and will
        never finish, because its unit isn't running (a power cut, a
        reboot). ``requested`` is left to the path unit and the 30 s rule;
        an unreadable unit state leaves the status alone."""
        status = self._read_status()
        if status is None or status.finished or status.phase == "requested":
            return
        state = await asyncio.to_thread(self._update_unit_state)
        if not update_unit_idle(state):
            return
        error = (
            f"The base restarted while the Update was "
            f"{status.phase.replace('_', ' ')}, before it finished."
        )
        if status.from_ is not None and await asyncio.to_thread(self._snapshot_kept):
            error += f" {recovery_text(status.from_.app)}"
        self.files.write_status(
            UpdateStatus(
                phase="failed",
                from_=status.from_,
                to=status.to,
                error=error,
                reason=REASON_INTERRUPTED,
                finished_at=self._clock(),
                updated_at=self._clock(),
            )
        )
        logger.warning("Update interrupted: %s", error)

    # ------------------------------------------------------------------
    # The outcome banner
    # ------------------------------------------------------------------

    def acknowledge(self, status: UpdateStatus) -> None:
        """The operator dismissed ``status``'s outcome banner."""
        self.files.acknowledge(status)
        self._acknowledged = (status.updated_at, True)

    def acknowledged(self, status: UpdateStatus) -> bool:
        """Whether the operator has dismissed ``status``'s outcome banner
        (read from disk once per outcome)."""
        if self._acknowledged is None or self._acknowledged[0] != status.updated_at:
            self._acknowledged = (status.updated_at, self.files.acknowledged(status))
        return self._acknowledged[1]
