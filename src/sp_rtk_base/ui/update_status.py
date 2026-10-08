"""What the Version & Update card and the header badge say (sp-rtk-base#236).

``ui/pages/*`` and ``ui/layout.py`` are excluded from the coverage gate,
so the wording is decided here, in a covered module.

A failed check shows on Settings only, as "Couldn't check (last checked
…)"; the last good result stays shown, and the badge never mentions it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import markdown2  # type: ignore[import-untyped]

from sp_rtk_base.services.update_check import UpdateCheckStatus
from sp_rtk_base.update.release_notes import PackageNotes, ReleaseNote

UP_TO_DATE_TEXT = "Up to date."
UPDATING_BADGE_TEXT = "Updating…"
NO_NOTES_TEXT = "No notes for this release."
NOTE_NOT_LOADED_TEXT = "Notes for this release couldn't be loaded."


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


def badge_text(status: UpdateCheckStatus, *, updating: bool = False) -> str | None:
    """The header badge: "Updating…" while an Update runs, else "Update X",
    or ``None``. Never a failed check."""
    if updating:
        return UPDATING_BADGE_TEXT
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


# ---------------------------------------------------------------------------
# Release notes (sp-rtk-base#237)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreReleaseView:
    """A pre-release folded under its stable release."""

    heading: str
    html: str


@dataclass(frozen=True)
class ReleaseView:
    """One release in a notes tab: its heading, then its notes or a line."""

    heading: str
    html: str | None
    """The notes as safe HTML (see :func:`notes_html`), or ``None``."""
    text: str | None = None
    """Shown instead of ``html``: there are no notes, or they didn't load."""
    includes: str | None = None
    """"Includes 0.10.0-beta.1, 0.10.0-beta.2", when pre-releases fold here."""
    pre_releases: list[PreReleaseView] = field(default_factory=list[PreReleaseView])


@dataclass(frozen=True)
class NotesTab:
    """The SP-Base or the Relay tab of the Release notes expander."""

    label: str
    text: str | None
    """A line instead of releases: GitHub failed, or the package doesn't change."""
    releases: list[ReleaseView]
    """Newest first."""


def notes_html(markdown: str) -> str:
    """Render notes from GitHub as HTML, with any raw HTML in them escaped.

    A changelog can't put markup on the page: tags show as text, and
    ``javascript:`` links are dropped.
    """
    html: str = markdown2.markdown(
        markdown, safe_mode="escape", extras=["fenced-code-blocks", "tables"]
    )
    return html


def notes_tabs(status: UpdateCheckStatus) -> list[NotesTab] | None:
    """The Release notes tabs, or ``None`` when there's nothing to show."""
    last = status.last_good
    if last is None or not last.available or last.notes is None:
        return None
    return [
        _tab("SP-Base", last.notes.app, last.target.app),
        _tab("Relay", last.notes.relay, last.target.relay),
    ]


def _tab(label: str, notes: PackageNotes, target: str) -> NotesTab:
    if not notes.loaded:
        return NotesTab(
            label,
            f"{label} notes couldn't be loaded (GitHub didn't answer). "
            "Update still works.",
            [],
        )
    if not notes.releases:
        return NotesTab(label, f"The {label} stays on {target}.", [])
    return NotesTab(label, None, [_release(label, r) for r in notes.releases])


def _dated(title: str, date: str | None) -> str:
    return f"{title} · {date}" if date else title


def _release(label: str, note: ReleaseNote) -> ReleaseView:
    heading = _dated(f"{label} {note.version}", note.date)
    pre_releases = [
        PreReleaseView(_dated(p.version, p.date), notes_html(p.body))
        for p in note.includes
    ]
    includes = (
        "Includes " + ", ".join(p.version for p in reversed(note.includes))
        if note.includes
        else None
    )
    if note.source in ("changelog", "release"):
        return ReleaseView(heading, notes_html(note.body), None, includes, pre_releases)
    text = NOTE_NOT_LOADED_TEXT if note.source == "unavailable" else NO_NOTES_TEXT
    return ReleaseView(heading, None, text, includes, pre_releases)
