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
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Literal

from sp_rtk_base import __version__ as app_version
from sp_rtk_base.services.update_check import running_relay_version
from sp_rtk_base.update.host_setup import INSTALL_COMMAND, HostSetup, read_host_setup
from sp_rtk_base.update.state import (
    REASON_DIDNT_START,
    UpdateFiles,
    UpdateRequest,
    UpdateStatus,
    Versions,
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
    ) -> None:
        self.files = files if files is not None else UpdateFiles()
        self._running = running
        self._clock = clock
        self._start_timeout = timedelta(seconds=start_timeout_s)
        self._survey_running: Callable[[], Awaitable[bool]] = _no_survey
        self._console_connected: Callable[[], bool] = lambda: False
        self._host_setup = host_setup
        self._host_requirement: Callable[[], int | None] = lambda: 0
        self._requested: Versions | None = None

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def set_survey_check(self, check: Callable[[], Awaitable[bool]]) -> None:
        """Set what says whether a Survey-in is running."""
        self._survey_running = check

    def set_console_check(self, check: Callable[[], bool]) -> None:
        """Set what says whether a Console link is connected."""
        self._console_connected = check

    def set_host_requirement(self, requirement: Callable[[], int | None]) -> None:
        """Set what says which Host setup the offered release needs
        (``None``: it couldn't be read)."""
        self._host_requirement = requirement

    async def host_setup(self) -> HostSetup:
        """This host's Host setup, as systemd reports it."""
        return await asyncio.to_thread(self._host_setup)

    async def refusal(self, host: HostSetup | None = None) -> UpdateRefusedError | None:
        """Why Update would be refused now, or ``None``. Writes nothing.

        ``host``: the Host setup, when the caller has just read it.
        """
        if self.updating():
            return UpdateRefusedError("updating")
        host_refusal = self._host_refusal(host or await self.host_setup())
        if host_refusal is not None:
            return UpdateRefusedError(host_refusal)
        try:
            survey = await self._survey_running()
        except Exception as exc:  # the receiver can't be read: no survey here
            logger.debug("Survey-in state unreadable for the update guard: %s", exc)
            survey = False
        if survey:
            return UpdateRefusedError("survey_running")
        if self._console_connected():
            return UpdateRefusedError("console_connected")
        return None

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
        refused = await self.refusal()
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
        status = self.files.read_status()
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

    # ------------------------------------------------------------------
    # The outcome banner
    # ------------------------------------------------------------------

    def acknowledge(self, status: UpdateStatus) -> None:
        """The operator dismissed ``status``'s outcome banner."""
        self.files.acknowledge(status)

    def acknowledged(self, status: UpdateStatus) -> bool:
        """Whether the operator has dismissed ``status``'s outcome banner."""
        return self.files.acknowledged(status)
