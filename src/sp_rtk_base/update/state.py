"""The request file and ``status.json``: how the app and the updater talk.

The app starts an Update by writing a request into the update directory
(``/var/lib/sp-rtk-base/update/``), which ``sp-rtk-base-update.path``
watches. The updater takes (reads and deletes) the request, then reports
each phase in ``status.json`` beside it, which the app reads. See ADR 0005.

The request is an expectation, never input: it names the SP-Base and
Relay the operator read Release notes for, and the updater only compares
them with the target it resolves itself.

**Format compatibility.** The old version's updater installs the new
version, so the new app reads a ``status.json`` the old updater wrote.
Both formats therefore stay readable across one release:

- add fields, never rename or remove one, and give new fields defaults;
- readers ignore fields they don't know;
- ``format`` is bumped when the meaning of an existing field changes.

Importable without NiceGUI, FastAPI or the app's services, like the rest
of :mod:`sp_rtk_base.update`.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

UPDATE_DIR_ENV = "SP_RTK_BASE_UPDATE_DIR"
"""Overrides :data:`DEFAULT_UPDATE_DIR` (tests, e2e)."""
DEFAULT_UPDATE_DIR = Path("/var/lib/sp-rtk-base/update")
REQUEST_FILENAME = "request.json"
"""The name ``sp-rtk-base-update.path`` watches for."""
STATUS_FILENAME = "status.json"
ROLLBACK_MARKER_FILENAME = "rollback"
"""Written by the updater after it restored the snapshot; the update unit's
root ``ExecStopPost`` line restarts the app only while it exists."""
PROGRESS_FILENAME = "update-progress.json"
"""The updater's own record of the phase, beside the venv and the snapshot
(``/opt/sp-rtk-base``), where the app can't write; see :class:`ProgressRecord`."""
ACKNOWLEDGED_FILENAME = "acknowledged.json"
"""The app's own note of the last outcome the operator dismissed."""
FORMAT = 1
"""The format version both files are written in."""

Phase = Literal[
    "requested",
    "resolving",
    "installing",
    "restarting",
    "verifying",
    "rolling_back",
    "done",
    "failed",
]
"""Where an Update is. ``done`` and ``failed`` end it. ``rolling_back``:
the new version failed its health check (or the unit stopped part-way),
the snapshot is restored and the old version is restarting."""
FINISHED_PHASES: frozenset[str] = frozenset({"done", "failed"})

# Why an Update failed, so the page can say whether anything changed.
REASON_BAD_REQUEST = "bad_request"
"""The request file couldn't be read. Nothing changed."""
REASON_CHECK_FAILED = "check_failed"
"""The updater couldn't resolve the target. Nothing changed."""
REASON_NEWER_RELEASE = "newer_release"
"""The target differs from the request. Nothing changed."""
REASON_HOST_SETUP = "host_setup"
"""The target needs newer Host setup than the host has. Nothing changed."""
REASON_HOST_REQUIREMENTS = "host_requirements"
"""The target's Host setup requirement couldn't be read. Nothing changed."""
REASON_INSTALL_FAILED = "install_failed"
"""pip failed."""
REASON_STOPPED = "stopped"
"""The update unit stopped before the Update finished (a timeout, a crash);
see ``rolled_back``."""
REASON_NO_DISK_SPACE = "no_disk_space"
"""Too little disk space for the snapshot. Nothing changed."""
REASON_SNAPSHOT_FAILED = "snapshot_failed"
"""The snapshot couldn't be taken. Nothing changed."""
REASON_FAILED_TO_START = "failed_to_start"
"""The new version failed its health check after the restart; see
``rolled_back``."""

REASON_DIDNT_START = "didnt_start"
"""Written by the app: no phase followed ``requested`` in time, so it took
the request back. Nothing changed."""

NEWER_RELEASE_ERROR = "A newer release appeared; check again."

UPDATING_MESSAGE = "An Update is running. Try again once it has finished."
"""Why Start, Survey-in, Console connect and Check now are refused while
an Update runs."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Versions(BaseModel):
    """An SP-Base and a Relay version, as installed or as a target."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    app: str
    relay: str


class UpdateRequest(BaseModel):
    """The app asking for an Update to the versions the operator read about."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    format: int = FORMAT
    app: str
    """The SP-Base version the operator read Release notes for."""
    relay: str
    """The Relay version the operator read Release notes for."""
    requested_at: datetime = Field(default_factory=_now)


class UpdateStatus(BaseModel):
    """The Update's progress and outcome, as the updater last wrote it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    format: int = FORMAT
    phase: Phase
    from_: Versions | None = Field(
        default=None,
        validation_alias=AliasChoices("from", "from_"),
        serialization_alias="from",
    )
    """What was installed when the Update started (``from`` on disk)."""
    to: Versions | None = None
    """What the Update installs."""
    error: str | None = None
    """Why it failed, in words for the operator."""
    reason: str | None = None
    """Why it failed, as one of the ``REASON_*`` codes."""
    rolled_back: bool = False
    """A failed Update restored the snapshot: the base is on ``from`` again."""
    rollback_error: str | None = None
    """Why the Rollback failed too (a double failure); ``error`` says why
    the Update failed."""
    finished_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("finished", "finished_at"),
        serialization_alias="finished",
    )
    """When the Update ended (``finished`` on disk)."""
    updated_at: datetime = Field(default_factory=_now)

    @property
    def finished(self) -> bool:
        """The Update has ended, one way or the other."""
        return self.phase in FINISHED_PHASES


class Acknowledged(BaseModel):
    """The outcome the operator dismissed, named by its ``updated_at``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    updated_at: datetime


class UpdateFiles:
    """The update directory: its request file and ``status.json``.

    ``directory`` defaults to ``$SP_RTK_BASE_UPDATE_DIR``, else
    :data:`DEFAULT_UPDATE_DIR`.
    """

    def __init__(self, directory: Path | None = None) -> None:
        if directory is None:
            env = os.environ.get(UPDATE_DIR_ENV)
            directory = Path(env) if env else DEFAULT_UPDATE_DIR
        self.directory = directory

    @property
    def request_path(self) -> Path:
        return self.directory / REQUEST_FILENAME

    @property
    def status_path(self) -> Path:
        return self.directory / STATUS_FILENAME

    def write_request(self, request: UpdateRequest) -> None:
        """Ask for an Update. The file appears whole, so the path unit
        never fires on half a request."""
        self._write(self.request_path, request.model_dump_json())

    def take_request(self) -> UpdateRequest | None:
        """Read and delete the request, or ``None`` if there is none.

        The file is gone before this returns, even when it can't be read,
        so the path unit can't fire again on it.

        Raises:
            ValueError: the request couldn't be read.
        """
        try:
            text = self.request_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        finally:
            self.request_path.unlink(missing_ok=True)
        try:
            return UpdateRequest.model_validate_json(text)
        except ValidationError as exc:
            raise ValueError(f"The update request couldn't be read: {exc}") from exc

    def write_status(self, status: UpdateStatus) -> None:
        self._write(self.status_path, status.model_dump_json(by_alias=True))

    def read_status(self) -> UpdateStatus | None:
        """The last status written, or ``None`` if there is none or it
        can't be read."""
        try:
            text = self.status_path.read_text(encoding="utf-8")
            return UpdateStatus.model_validate_json(text)
        except (OSError, ValidationError):
            return None

    @property
    def rollback_marker_path(self) -> Path:
        return self.directory / ROLLBACK_MARKER_FILENAME

    def mark_rollback(self) -> None:
        """Ask the unit's root ``ExecStopPost`` line to restart the app."""
        self._write(self.rollback_marker_path, "")

    def take_rollback_marker(self) -> bool:
        """Delete the rollback marker; whether there was one."""
        try:
            self.rollback_marker_path.unlink()
        except FileNotFoundError:
            return False
        return True

    @property
    def acknowledged_path(self) -> Path:
        return self.directory / ACKNOWLEDGED_FILENAME

    def acknowledge(self, status: UpdateStatus) -> None:
        """Note that the operator dismissed ``status``'s outcome."""
        self._write(
            self.acknowledged_path,
            Acknowledged(updated_at=status.updated_at).model_dump_json(),
        )

    def acknowledged(self, status: UpdateStatus) -> bool:
        """Whether the operator dismissed ``status``'s outcome."""
        try:
            text = self.acknowledged_path.read_text(encoding="utf-8")
            seen = Acknowledged.model_validate_json(text)
        except (OSError, ValidationError):
            return False
        return seen.updated_at == status.updated_at

    def _write(self, path: Path, text: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        _write_atomically(path, text)


class ProgressRecord:
    """The updater's own record of where the Update is.

    ``status.json`` is the report to the app, which can write it too, so
    a compromised app could forge a phase. Whether to verify, restore the
    snapshot or roll back is decided from this record instead: it lives
    beside the snapshot (``/opt/sp-rtk-base``), which only the update unit
    can write. It holds the same :class:`UpdateStatus` the updater last
    reported.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, status: UpdateStatus) -> None:
        _write_atomically(self.path, status.model_dump_json(by_alias=True))

    def read(self) -> UpdateStatus | None:
        """What the updater last reported, or ``None`` if it never has (or
        the record can't be read)."""
        try:
            text = self.path.read_text(encoding="utf-8")
            return UpdateStatus.model_validate_json(text)
        except (OSError, ValidationError):
            return None


def _write_atomically(path: Path, text: str) -> None:
    """Write ``path`` atomically: a temp file beside it, then a rename."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)  # a leftover from a crash
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
