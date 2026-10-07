"""What the Version & Update card and the header badge say (sp-rtk-base#236).

``ui/pages/*`` and ``ui/layout.py`` are excluded from the coverage gate,
so the wording is decided here, in a covered module.

A failed check shows on Settings only, as "Couldn't check (last checked
…)"; the last good result stays shown, and the badge never mentions it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sp_rtk_base.services.update_check import UpdateCheckStatus

UP_TO_DATE_TEXT = "Up to date."


@dataclass(frozen=True)
class CheckLine:
    """The card header's line about the last check."""

    text: str
    checking: bool = False
    failed: bool = False


@dataclass(frozen=True)
class VersionRow:
    """One version row: ``current``, or ``current → target``."""

    label: str
    current: str
    target: str | None


def format_checked_at(when: datetime) -> str:
    """``7 Oct 09:12``, in the base's local time."""
    local = when.astimezone()
    return f"{local.day} {local:%b %H:%M}"


def check_line(status: UpdateCheckStatus) -> CheckLine:
    """Say "Last checked …", "Checking…" or "Couldn't check (last checked …)"."""
    if status.checking:
        return CheckLine("Checking…", checking=True)
    last = status.last_good
    if status.last_check_failed:
        if last is None:
            return CheckLine("Couldn't check", failed=True)
        return CheckLine(
            f"Couldn't check (last checked {format_checked_at(last.checked_at)})",
            failed=True,
        )
    if last is None:
        return CheckLine("Not checked yet")
    return CheckLine(f"Last checked {format_checked_at(last.checked_at)}")


def version_rows(
    status: UpdateCheckStatus, running_app: str, running_relay: str
) -> list[VersionRow]:
    """The SP-Base and Relay rows, with the target when an Update changes it."""
    last = status.last_good
    if last is None or not last.available:
        return [
            VersionRow("SP-Base", running_app, None),
            VersionRow("SP-Base Relay", running_relay, None),
        ]
    relay_target = last.target.relay if last.target.relay != running_relay else None
    return [
        VersionRow("SP-Base", running_app, last.target.app),
        VersionRow("SP-Base Relay", running_relay, relay_target),
    ]


def up_to_date(status: UpdateCheckStatus) -> bool:
    """A check has worked, and it found no Available update."""
    return status.last_good is not None and not status.last_good.available


def badge_text(status: UpdateCheckStatus) -> str | None:
    """The header badge, "Update X", or ``None``. Never a failed check."""
    last = status.last_good
    if last is None or not last.available:
        return None
    return f"Update {last.target.app}"


def python_note(status: UpdateCheckStatus) -> str | None:
    """The note when a newer release needs a newer Python than this base runs."""
    last = status.last_good
    if last is None or last.target.newer_needs_python is None:
        return None
    newer = last.target.newer_needs_python
    needs = f"Python {newer.python}" if newer.python else "a newer Python"
    # "A.B" unless the floor itself names a patch release.
    patch_level = newer.python is not None and newer.python.count(".") >= 2
    runs = last.running_python
    if not patch_level:
        runs = ".".join(runs.split(".")[:2])
    note = f"{newer.version} needs {needs}; this base runs {runs}."
    if last.available:
        note += f" Offering {last.target.app}, the newest release that runs here."
    return note
