"""The update check: is there an Available update, and since when do we know?

Checks PyPI at startup, every 24 h, and on Check now, through
:func:`sp_rtk_base.update.release.resolve_release`. Keeps the last good
result and its time, so a failed check never hides what was known: the
page says "Couldn't check" and still shows the last good answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Callable
from datetime import datetime

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, computed_field

from sp_rtk_base import __version__ as app_version
from sp_rtk_base.update.host_setup import HostRequirementError, required_plumbing
from sp_rtk_base.update.release import (
    Fetch,
    PythonVersion,
    ReleaseTarget,
    resolve_release,
    urllib_fetch,
)
from sp_rtk_base.update.release_notes import ReleaseNotes, release_notes

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS: float = 24 * 60 * 60
"""How often the base checks PyPI by itself."""


def running_relay_version() -> str:
    """The installed Relay's version, or "not installed"."""
    try:
        from sp_rtk_base_relay import __version__ as relay_version
    except ImportError:  # pragma: no cover - the Relay is a hard dependency
        return "not installed"
    return relay_version


def _local_now() -> datetime:
    return datetime.now().astimezone()


class UpdateCheck(BaseModel):
    """One good check: what runs here, and what an Update would install."""

    model_config = ConfigDict(frozen=True)

    running_app: str
    running_relay: str
    running_python: str
    """The running Python (``A.B.C``), the one ``target`` was resolved for."""
    target: ReleaseTarget
    checked_at: datetime
    notes: ReleaseNotes | None = None
    """The Release notes up to the target; only for an Available update."""
    host_requirement: int | None = None
    """The Host setup (plumbing version) the target needs; only for an
    Available update, and ``None`` when it couldn't be read."""
    host_requirement_error: str | None = None
    """Why ``host_requirement`` couldn't be read."""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available(self) -> bool:
        """An Available update: the target SP-Base is newer than the running one."""
        try:
            return Version(self.target.app) > Version(self.running_app)
        except InvalidVersion:  # pragma: no cover - both come from packaging
            return False


class UpdateCheckStatus(BaseModel):
    """What the page and the API show about checking for an update."""

    model_config = ConfigDict(frozen=True)

    last_good: UpdateCheck | None = None
    """The last check that worked; kept when a later one fails."""
    checking: bool = False
    last_check_failed: bool = False
    """The most recent check failed; ``last_good`` (if any) is older."""
    error: str | None = None
    """Why the most recent check failed, for logs and the API."""


class UpdateCheckService:
    """Checks for an Available update and holds the last good result."""

    def __init__(
        self,
        fetch: Fetch = urllib_fetch,
        *,
        running_app: str = app_version,
        running_relay: str | None = None,
        python: PythonVersion | None = None,
        clock: Callable[[], datetime] = _local_now,
        interval_s: float = CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._fetch = fetch
        self._running_app = running_app
        self._running_relay = (
            running_relay if running_relay is not None else running_relay_version()
        )
        self._python: PythonVersion = (
            python if python is not None else tuple(sys.version_info[:3])
        )
        self._clock = clock
        self.interval_s = interval_s
        self._status = UpdateCheckStatus()
        self._check: asyncio.Task[UpdateCheckStatus] | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def status(self) -> UpdateCheckStatus:
        """The current status; replaced whole, never mutated."""
        return self._status

    async def check_now(self) -> UpdateCheckStatus:
        """Check PyPI now. A call while a check runs joins that check."""
        if self._check is None or self._check.done():
            self._check = asyncio.create_task(
                self._run_check(), name="sp_rtk_base.update_check.check"
            )
        return await asyncio.shield(self._check)

    async def _run_check(self) -> UpdateCheckStatus:
        self._status = self._status.model_copy(update={"checking": True})
        try:
            check = await asyncio.to_thread(self._check_once)
        except Exception as exc:
            logger.warning("Update check failed: %s", exc)
            self._status = self._status.model_copy(
                update={
                    "checking": False,
                    "last_check_failed": True,
                    "error": str(exc) or type(exc).__name__,
                }
            )
        else:
            self._status = UpdateCheckStatus(last_good=check)
        return self._status

    def _check_once(self) -> UpdateCheck:
        """Resolve the target; for an Available update, load its notes too.

        Only resolution can fail the check: the notes report a GitHub
        failure themselves, and so does the Host setup requirement.
        """
        target = resolve_release(self._fetch, self._python)
        check = UpdateCheck(
            running_app=self._running_app,
            running_relay=self._running_relay,
            running_python=".".join(str(part) for part in self._python),
            target=target,
            checked_at=self._clock(),
        )
        if not check.available:
            return check
        notes = release_notes(
            self._fetch, self._running_app, self._running_relay, target
        )
        try:
            requirement = required_plumbing(self._fetch, target.app)
        except HostRequirementError as exc:
            logger.warning("Host setup requirement unreadable: %s", exc)
            return check.model_copy(
                update={"notes": notes, "host_requirement_error": str(exc)}
            )
        return check.model_copy(
            update={"notes": notes, "host_requirement": requirement}
        )

    # ------------------------------------------------------------------
    # Background schedule
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Check now, then every :attr:`interval_s`. Idempotent."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="sp_rtk_base.update_check"
            )

    async def stop(self) -> None:
        """Cancel the schedule (a check already sent to PyPI is abandoned)."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await self.check_now()
            await asyncio.sleep(self.interval_s)
