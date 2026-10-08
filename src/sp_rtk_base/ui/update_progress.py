"""What the page says about an Update (sp-rtk-base#240).

The confirm dialog, the "<phase>… (step n of 5)" bar, the page-wide
banner and Settings' outcome stripe. ``ui/pages/*``, ``ui/layout.py`` and
``ui/components/*`` are excluded from the coverage gate, so the wording is
decided here, in a covered module.

Later Update work adds to the tables here rather than to the pages:
:data:`FAILED_OUTCOMES` (an outcome per ``reason`` code) and
:func:`banner` (the banner after a failed Update).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sp_rtk_base.ui.update_status import format_checked_at
from sp_rtk_base.update.state import (
    REASON_BAD_REQUEST,
    REASON_CHECK_FAILED,
    REASON_DIDNT_START,
    REASON_HOST_REQUIREMENTS,
    REASON_HOST_SETUP,
    REASON_NEWER_RELEASE,
    UpdateStatus,
    Versions,
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

PHASE_STEPS: list[tuple[str, str]] = [
    ("requested", "Waiting for the host"),
    ("resolving", "Checking the release"),
    ("installing", "Installing"),
    ("restarting", "Restarting"),
    ("verifying", "Checking it started"),
]
"""The phases the bar shows, in order, with what it calls each."""


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
    phases = [phase for phase, _ in PHASE_STEPS]
    if status.phase not in phases:  # pragma: no cover - every running phase is listed
        return None
    index = phases.index(status.phase)
    steps = len(PHASE_STEPS)
    name = PHASE_STEPS[index][1]
    return ProgressLine(
        text=f"{name}… (step {index + 1} of {steps})",
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
    page reconnects by itself.", then "Checking X started…". Once it has
    updated: "Now on X.", until dismissed (``acknowledged``). An Update
    that changed nothing shows on Settings only.
    """
    if status is None:
        return None
    target = status.to.app if status.to is not None else None
    if not status.finished:
        if target is None:
            return Banner("Updating…", busy=True)
        if status.phase == "restarting":
            text = f"Restarting into {target}. This page reconnects by itself."
        elif status.phase == "verifying":
            text = f"Checking {target} started…"
        else:
            text = f"Updating to {target}…"
        return Banner(text, busy=True)
    if acknowledged:
        return None
    if status.phase == "done" and target is not None:
        return Banner(f"Now on {target}.", kind="positive", dismissible=True)
    return None


# ---------------------------------------------------------------------------
# Settings' outcome stripe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """The last Update's outcome, a stripe at the top of the card."""

    text: str
    kind: Kind


def _target(status: UpdateStatus) -> str:
    return f"Update to {status.to.app}" if status.to is not None else "Update"


def _not_started(status: UpdateStatus) -> Outcome:
    error = (status.error or "").strip()
    reason = f": {error}" if error else ""
    return Outcome(
        f"{_target(status)} not started{reason} {NOTHING_CHANGED}", "warning"
    )


FAILED_OUTCOMES: dict[str, Callable[[UpdateStatus], Outcome]] = {
    REASON_DIDNT_START: lambda status: Outcome(
        "Update didn't start: the host didn't pick up the request within 30 s. "
        + NOTHING_CHANGED,
        "warning",
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
}
"""The outcome of a failed Update, by its ``reason`` code."""


def _failed(status: UpdateStatus) -> Outcome:
    error = (status.error or "").strip() or "see journalctl -u sp-rtk-base-update."
    return Outcome(f"{_target(status)} failed: {error}", "negative")


def outcome(status: UpdateStatus | None) -> Outcome | None:
    """ "Updated a → b on 7 Oct 14:02.", or why it failed; ``None`` while
    an Update runs or when there never was one."""
    if status is None or not status.finished:
        return None
    if status.phase == "done":
        start = status.from_.app if status.from_ is not None else "?"
        end = status.to.app if status.to is not None else "?"
        when = format_checked_at(status.updated_at)
        return Outcome(f"Updated {start} → {end} on {when}.", "positive")
    return FAILED_OUTCOMES.get(status.reason or "", _failed)(status)
