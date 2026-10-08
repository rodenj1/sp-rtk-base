"""What the page says about an Update: the confirm dialog, the step bar,
the page-wide banner and Settings' outcome stripe (sp-rtk-base#240).

The texts come from the spec and the Update UX prototype (variant A).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from sp_rtk_base.ui.update_progress import (
    RELAY_RUNNING_WARNING,
    banner,
    confirm_text,
    outcome,
    progress_line,
    update_button_text,
)
from sp_rtk_base.update.state import (
    NEWER_RELEASE_ERROR,
    REASON_CHECK_FAILED,
    REASON_DIDNT_START,
    REASON_INSTALL_FAILED,
    REASON_NEWER_RELEASE,
    UpdateStatus,
    Versions,
)

FROM = Versions(app="0.9.0", relay="0.6.2")
TO = Versions(app="0.10.1", relay="0.7.0")
AT = datetime(2026, 10, 7, 14, 2).astimezone()


def _status(phase: str, **extra: object) -> UpdateStatus:
    return UpdateStatus.model_validate(
        {"phase": phase, "from": FROM, "to": TO, "updated_at": AT, **extra}
    )


class TestConfirm:
    def test_names_both_version_changes(self) -> None:
        assert confirm_text(FROM, TO) == (
            "SP-Base 0.9.0 → 0.10.1, Relay 0.6.2 → 0.7.0. "
            "The base restarts; this page reconnects by itself."
        )

    def test_an_unchanged_relay_says_so(self) -> None:
        same_relay = Versions(app="0.10.1", relay="0.6.2")

        assert confirm_text(FROM, same_relay) == (
            "SP-Base 0.9.0 → 0.10.1, Relay 0.6.2 (unchanged). "
            "The base restarts; this page reconnects by itself."
        )

    def test_the_relay_running_warning(self) -> None:
        assert RELAY_RUNNING_WARNING == (
            "The Relay is running. Corrections stop for about a minute "
            "and resume on their own."
        )

    def test_the_button(self) -> None:
        assert update_button_text("0.10.1") == "Update to 0.10.1"


class TestProgressLine:
    @pytest.mark.parametrize(
        ("phase", "text", "step"),
        [
            ("requested", "Waiting for the host… (step 1 of 5)", 1),
            ("resolving", "Checking the release… (step 2 of 5)", 2),
            ("installing", "Installing… (step 3 of 5)", 3),
            ("restarting", "Restarting… (step 4 of 5)", 4),
            ("verifying", "Checking it started… (step 5 of 5)", 5),
        ],
    )
    def test_every_phase_shows(self, phase: str, text: str, step: int) -> None:
        line = progress_line(_status(phase))

        assert line is not None
        assert line.text == text
        assert line.step == step
        assert 0 < line.value < 1

    def test_the_bar_moves_forward(self) -> None:
        values = [
            line.value
            for phase in ("requested", "resolving", "installing", "restarting")
            if (line := progress_line(_status(phase))) is not None
        ]

        assert values == sorted(values)

    @pytest.mark.parametrize("phase", ["done", "failed"])
    def test_no_bar_once_finished(self, phase: str) -> None:
        assert progress_line(_status(phase)) is None

    def test_no_bar_without_an_update(self) -> None:
        assert progress_line(None) is None


class TestBanner:
    @pytest.mark.parametrize("phase", ["requested", "resolving", "installing"])
    def test_updating_to_x(self, phase: str) -> None:
        shown = banner(_status(phase), acknowledged=False)

        assert shown is not None
        assert shown.text == "Updating to 0.10.1…"
        assert shown.busy
        assert not shown.dismissible

    def test_without_a_target_yet(self) -> None:
        shown = banner(_status("resolving", to=None), acknowledged=False)

        assert shown is not None
        assert shown.text == "Updating…"

    def test_restarting(self) -> None:
        shown = banner(_status("restarting"), acknowledged=False)

        assert shown is not None
        assert shown.text == "Restarting into 0.10.1. This page reconnects by itself."

    def test_verifying(self) -> None:
        shown = banner(_status("verifying"), acknowledged=False)

        assert shown is not None
        assert shown.text == "Checking 0.10.1 started…"

    def test_now_on_x_until_dismissed(self) -> None:
        shown = banner(_status("done"), acknowledged=False)

        assert shown is not None
        assert shown.text == "Now on 0.10.1."
        assert shown.kind == "positive"
        assert shown.dismissible
        assert not shown.busy
        assert banner(_status("done"), acknowledged=True) is None

    def test_a_refusal_that_changed_nothing_shows_on_settings_only(self) -> None:
        refused = _status("failed", reason=REASON_DIDNT_START)

        assert banner(refused, acknowledged=False) is None

    def test_no_update(self) -> None:
        assert banner(None, acknowledged=False) is None


class TestOutcome:
    def test_updated(self) -> None:
        shown = outcome(_status("done"))

        assert shown is not None
        assert shown.text == "Updated 0.9.0 → 0.10.1 on 7 Oct 14:02."
        assert shown.kind == "positive"

    def test_didnt_start(self) -> None:
        shown = outcome(_status("failed", reason=REASON_DIDNT_START))

        assert shown is not None
        assert shown.text == (
            "Update didn't start: the host didn't pick up the request "
            "within 30 s. Nothing changed."
        )
        assert shown.kind == "warning"

    def test_a_newer_release_appeared(self) -> None:
        shown = outcome(
            _status("failed", reason=REASON_NEWER_RELEASE, error=NEWER_RELEASE_ERROR)
        )

        assert shown is not None
        assert shown.text == (
            "Update not started: a newer release appeared since you checked. "
            "Check again and read its notes. Nothing changed."
        )
        assert shown.kind == "warning"

    def test_the_host_couldnt_check_the_release(self) -> None:
        shown = outcome(
            _status("failed", reason=REASON_CHECK_FAILED, error="PyPI didn't answer.")
        )

        assert shown is not None
        assert shown.text == (
            "Update to 0.10.1 not started: PyPI didn't answer. Nothing changed."
        )

    def test_any_other_failure_says_what_failed(self) -> None:
        shown = outcome(
            _status("failed", reason=REASON_INSTALL_FAILED, error="pip exited 1.")
        )

        assert shown is not None
        assert shown.text == "Update to 0.10.1 failed: pip exited 1."
        assert shown.kind == "negative"

    @pytest.mark.parametrize("phase", ["requested", "installing"])
    def test_none_while_updating(self, phase: str) -> None:
        assert outcome(_status(phase)) is None

    def test_none_without_an_update(self) -> None:
        assert outcome(None) is None
