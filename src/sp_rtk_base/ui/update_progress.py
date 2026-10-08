"""What the page says about an Update (sp-rtk-base#240).

The confirm dialog, the "<phase>… (step n of 5)" bar, the page-wide
banner and Settings' outcome stripe. ``ui/pages/*``, ``ui/layout.py`` and
``ui/components/*`` are excluded from the coverage gate, so the wording is
decided here, in a covered module.

Later Update work adds to the tables here rather than to the pages:
:data:`FAILED_OUTCOMES` (an outcome per ``reason`` code) and
:func:`banner` (the banner after a failed Update). #241 added the Rollback
outcomes and banners, and :func:`failed_here`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sp_rtk_base.services.update_service import DIDNT_PICK_UP
from sp_rtk_base.ui.formatting import format_checked_at
from sp_rtk_base.update.state import (
    REASON_BAD_REQUEST,
    REASON_CHECK_FAILED,
    REASON_DIDNT_START,
    REASON_FAILED_TO_START,
    REASON_HOST_REQUIREMENTS,
    REASON_HOST_SETUP,
    REASON_INTERRUPTED,
    REASON_NEWER_RELEASE,
    REASON_NO_DISK_SPACE,
    REASON_SNAPSHOT_FAILED,
    REASON_STOPPED,
    Phase,
    Reason,
    UpdateStatus,
    Versions,
    recovery_text,
)

Kind = Literal["info", "positive", "warning", "negative"]
"""How a banner or an outcome is coloured."""

KIND_COLOURS: dict[Kind, str] = {
    "info": "#1e3a5f",
    "positive": "#1b5e20",
    "warning": "#7a5200",
    "negative": "#7f1d1d",
}

NOTHING_CHANGED = "Nothing changed."

RELAY_RUNNING_WARNING = (
    "The Relay is running. Corrections stop for about a minute and resume on their own."
)


@dataclass(frozen=True)
class PhaseWords:
    """What the bar and the banner call a running phase."""

    step: str
    """The bar's "<step>… (step n of 5)"."""
    banner: str
    """The banner, with ``{target}`` for the SP-Base being installed."""


PHASE_WORDS: dict[Phase, PhaseWords] = {
    "requested": PhaseWords("Waiting for the host", "Updating to {target}…"),
    "resolving": PhaseWords("Checking the release", "Updating to {target}…"),
    "installing": PhaseWords("Installing", "Updating to {target}…"),
    "restarting": PhaseWords(
        "Restarting", "Restarting into {target}. This page reconnects by itself."
    ),
    "verifying": PhaseWords("Checking it started", "Checking {target} started…"),
}
"""The phases the bar shows, in order, with what the bar and the banner
say during each. ``rolling_back`` is the last step, worded by its reason."""

PHASE_STEPS: list[tuple[Phase, str]] = [
    (phase, words.step) for phase, words in PHASE_WORDS.items()
]
"""The bar's steps, in order."""


def update_button_text(target_app: str) -> str:
    """The button at the bottom of the card."""
    return f"Update to {target_app}"


def confirm_text(running: Versions, target: Versions) -> str:
    """The confirm dialog: both version changes, and that the page reconnects."""
    relay = (
        f"Relay {running.relay} (unchanged)"
        if target.relay == running.relay
        else f"Relay {running.relay} → {target.relay}"
    )
    return (
        f"SP-Base {running.app} → {target.app}, {relay}. "
        "The base restarts; this page reconnects by itself."
    )


# ---------------------------------------------------------------------------
# The step bar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressLine:
    """The thin bar under the versions while an Update runs."""

    text: str
    step: int
    steps: int
    value: float
    """How far along the bar is, between 0 and 1."""


def progress_line(status: UpdateStatus | None) -> ProgressLine | None:
    """ "Installing… (step 3 of 5)", or ``None`` when no Update runs."""
    if status is None or status.finished:
        return None
    steps = len(PHASE_STEPS)
    if status.phase == "rolling_back":
        return ProgressLine(
            text=f"Rolling back to {status.from_app}… (step {steps} of {steps})",
            step=steps,
            steps=steps,
            value=(steps - 0.5) / steps,
        )
    phases = [phase for phase, _ in PHASE_STEPS]
    if status.phase not in phases:  # pragma: no cover - every running phase is listed
        return None
    index = phases.index(status.phase)
    return ProgressLine(
        text=f"{PHASE_WORDS[status.phase].step}… (step {index + 1} of {steps})",
        step=index + 1,
        steps=steps,
        value=(index + 0.5) / steps,
    )


# ---------------------------------------------------------------------------
# The page-wide banner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Banner:
    """The page-wide banner, on every page."""

    text: str
    kind: Kind = "info"
    busy: bool = False
    """An Update runs: a spinner shows."""
    dismissible: bool = False
    """An outcome, shown until the operator dismisses it."""


def banner(status: UpdateStatus | None, *, acknowledged: bool) -> Banner | None:
    """The banner for ``status``, or ``None``.

    While an Update runs: "Updating to X…", then "Restarting into X. This
    page reconnects by itself.", then "Checking X started…" (or "X failed
    to start; rolling back to Y…"). Once it has updated: "Now on X.",
    until dismissed (``acknowledged``); likewise "Update to X failed to
    start; still on Y." after a Rollback, and a pointer to Settings after a
    double failure. Any other failure changed nothing that runs and shows
    on Settings only.
    """
    if status is None:
        return None
    target = status.to.app if status.to is not None else None
    start = status.from_app
    if not status.finished:
        if target is None:
            return Banner("Updating…", busy=True)
        if status.phase == "rolling_back" and status.reason == REASON_STOPPED:
            text = f"Update to {target} stopped part-way; rolling back to {start}…"
        elif status.phase == "rolling_back":
            text = f"{target} failed to start; rolling back to {start}…"
        else:
            words = PHASE_WORDS.get(status.phase, PHASE_WORDS["installing"])
            text = words.banner.format(target=target)
        return Banner(text, busy=True)
    if acknowledged:
        return None
    if status.phase == "done" and target is not None:
        return Banner(f"Now on {target}.", kind="positive", dismissible=True)
    if status.rollback_error is not None:
        return Banner(
            "Update failed and could not roll back. See Settings.",
            kind="negative",
            dismissible=True,
        )
    if status.reason == REASON_INTERRUPTED:
        return Banner(
            f"{_update_label(status)} was interrupted. See Settings.",
            kind="negative",
            dismissible=True,
        )
    if status.reason == REASON_FAILED_TO_START and status.rolled_back:
        return Banner(
            f"{_update_label(status)} failed to start; still on {start}.",
            kind="warning",
            dismissible=True,
        )
    if status.reason == REASON_STOPPED and status.rolled_back:
        return Banner(
            f"{_update_label(status)} stopped part-way; still on {start}.",
            kind="warning",
            dismissible=True,
        )
    return None


# ---------------------------------------------------------------------------
# Settings' outcome stripe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """The last Update's outcome, a stripe at the top of the card."""

    text: str
    kind: Kind


def _update_label(status: UpdateStatus) -> str:
    """ "Update to X", or "Update" before the target is known."""
    return f"Update to {status.to.app}" if status.to is not None else "Update"


def _not_started(status: UpdateStatus) -> Outcome:
    error = (status.error or "").strip()
    reason = f": {error}" if error else ""
    return Outcome(
        f"{_update_label(status)} not started{reason} {NOTHING_CHANGED}", "warning"
    )


def _ended_at(status: UpdateStatus) -> str:
    return format_checked_at(status.finished_at or status.updated_at)


def _rolled_back(status: UpdateStatus) -> Outcome:
    return Outcome(
        f"{_update_label(status)} failed to start; rolled back to {status.from_app} "
        f"on {_ended_at(status)}.",
        "warning",
    )


def _double_failure(status: UpdateStatus) -> Outcome:
    start = status.from_app
    return Outcome(
        f"{_update_label(status)} failed and the Rollback to {start} failed too. "
        + recovery_text(start),
        "negative",
    )


FAILED_OUTCOMES: dict[Reason, Callable[[UpdateStatus], Outcome]] = {
    REASON_DIDNT_START: lambda status: Outcome(
        f"Update didn't start: {DIDNT_PICK_UP}. {NOTHING_CHANGED}", "warning"
    ),
    REASON_NEWER_RELEASE: lambda status: Outcome(
        "Update not started: a newer release appeared since you checked. "
        "Check again and read its notes. " + NOTHING_CHANGED,
        "warning",
    ),
    REASON_CHECK_FAILED: _not_started,
    REASON_BAD_REQUEST: _not_started,
    REASON_HOST_SETUP: _not_started,
    REASON_HOST_REQUIREMENTS: _not_started,
    REASON_SNAPSHOT_FAILED: _not_started,
    REASON_NO_DISK_SPACE: lambda status: Outcome(
        f"{_update_label(status)} not started: not enough disk space. {NOTHING_CHANGED}",
        "warning",
    ),
    REASON_FAILED_TO_START: _rolled_back,
    REASON_INTERRUPTED: lambda status: Outcome(
        f"{_update_label(status)} was interrupted: {(status.error or '').strip()}",
        "negative",
    ),
}
"""The outcome of a failed Update, by its ``reason`` code. A double failure
(``rollback_error``) says so whatever the reason."""


def _failed(status: UpdateStatus) -> Outcome:
    error = (status.error or "").strip() or "see journalctl -u sp-rtk-base-update."
    if status.rolled_back:
        return Outcome(
            f"{_update_label(status)} failed: {error} {NOTHING_CHANGED}", "warning"
        )
    return Outcome(f"{_update_label(status)} failed: {error}", "negative")


def outcome(status: UpdateStatus | None) -> Outcome | None:
    """ "Updated a → b on 7 Oct 14:02.", or why it failed; ``None`` while
    an Update runs or when there never was one."""
    if status is None or not status.finished:
        return None
    if status.phase == "done":
        start = status.from_app
        end = status.to.app if status.to is not None else "?"
        when = format_checked_at(status.updated_at)
        return Outcome(f"Updated {start} → {end} on {when}.", "positive")
    if status.rollback_error is not None:
        return _double_failure(status)
    shown = FAILED_OUTCOMES.get(status.reason) if status.reason is not None else None
    return (shown or _failed)(status)


def failed_here(status: UpdateStatus | None, target_app: str) -> str | None:
    """ "X failed to start here on …", under Update while the offered
    release is one that failed its health check on this base."""
    if (
        status is None
        or status.phase != "failed"
        or status.reason != REASON_FAILED_TO_START
        or status.to is None
        or status.to.app != target_app
    ):
        return None
    return f"{target_app} failed to start here on {_ended_at(status)}."
