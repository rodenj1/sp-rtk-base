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
    failed_here,
    outcome,
    progress_line,
    update_button_text,
)
from sp_rtk_base.update.state import (
    NEWER_RELEASE_ERROR,
    REASON_CHECK_FAILED,
    REASON_DIDNT_START,
    REASON_FAILED_TO_START,
    REASON_INSTALL_FAILED,
    REASON_NEWER_RELEASE,
    REASON_NO_DISK_SPACE,
    REASON_SNAPSHOT_FAILED,
    REASON_STOPPED,
    UpdateStatus,
    Versions,
)

FROM = Versions(app="0.9.0", relay="0.6.2")
TO = Versions(app="0.10.1", relay="0.7.0")
AT = datetime(2026, 10, 7, 14, 2).astimezone()
FINISHED = datetime(2026, 10, 7, 14, 4).astimezone()


def _status(phase: str, **extra: object) -> UpdateStatus:
    return UpdateStatus.model_validate(
        {"phase": phase, "from": FROM, "to": TO, "updated_at": AT, **extra}
    )


def _rolled_back() -> UpdateStatus:
    return _status(
        "failed",
        reason=REASON_FAILED_TO_START,
        error="SP-Base 0.10.1 didn't start within 90 s.",
        rolled_back=True,
        finished=FINISHED,
    )


def _double_failure(reason: str = REASON_FAILED_TO_START) -> UpdateStatus:
    return _status(
        "failed",
        reason=reason,
        error="SP-Base 0.10.1 didn't start within 90 s.",
        rollback_error="SP-Base 0.9.0 didn't start within 90 s.",
        finished=FINISHED,
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

    def test_rolling_back(self) -> None:
        line = progress_line(_status("rolling_back"))

        assert line is not None
        assert line.text == "Rolling back to 0.9.0… (step 5 of 5)"
        assert line.step == 5

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

    def test_rolling_back(self) -> None:
        shown = banner(_status("rolling_back"), acknowledged=False)

        assert shown is not None
        assert shown.text == "0.10.1 failed to start; rolling back to 0.9.0…"
        assert shown.busy

    def test_failed_to_start_once_until_dismissed(self) -> None:
        shown = banner(_rolled_back(), acknowledged=False)

        assert shown is not None
        assert shown.text == "Update to 0.10.1 failed to start; still on 0.9.0."
        assert shown.kind == "warning"
        assert shown.dismissible
        assert banner(_rolled_back(), acknowledged=True) is None

    @pytest.mark.parametrize("reason", [REASON_FAILED_TO_START, REASON_INSTALL_FAILED])
    def test_a_double_failure_points_to_settings(self, reason: str) -> None:
        shown = banner(_double_failure(reason), acknowledged=False)

        assert shown is not None
        assert shown.text == "Update failed and could not roll back. See Settings."
        assert shown.kind == "negative"
        assert shown.dismissible

    def test_a_pip_failure_that_was_rolled_back_shows_on_settings_only(self) -> None:
        failed = _status(
            "failed", reason=REASON_INSTALL_FAILED, error="pip", rolled_back=True
        )

        assert banner(failed, acknowledged=False) is None

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

    def test_failed_to_start_and_rolled_back(self) -> None:
        shown = outcome(_rolled_back())

        assert shown is not None
        assert shown.text == (
            "Update to 0.10.1 failed to start; rolled back to 0.9.0 on 7 Oct 14:04."
        )
        assert shown.kind == "warning"

    def test_not_enough_disk_space(self) -> None:
        shown = outcome(
            _status(
                "failed",
                reason=REASON_NO_DISK_SPACE,
                error="Not enough disk space: the Update needs 420 MiB and 100 MiB is free.",
            )
        )

        assert shown is not None
        assert shown.text == (
            "Update to 0.10.1 not started: not enough disk space. Nothing changed."
        )
        assert shown.kind == "warning"

    def test_the_snapshot_couldnt_be_taken(self) -> None:
        shown = outcome(
            _status("failed", reason=REASON_SNAPSHOT_FAILED, error="Permission denied.")
        )

        assert shown is not None
        assert shown.text == (
            "Update to 0.10.1 not started: Permission denied. Nothing changed."
        )

    @pytest.mark.parametrize(
        "reason", [REASON_FAILED_TO_START, REASON_INSTALL_FAILED, REASON_STOPPED]
    )
    def test_a_double_failure(self, reason: str) -> None:
        shown = outcome(_double_failure(reason))

        assert shown is not None
        assert shown.text == (
            "Update to 0.10.1 failed and the rollback to 0.9.0 failed too. "
            "On the base run: sudo deploy/upgrade.sh 0.9.0, and see "
            "journalctl -u sp-rtk-base-update."
        )
        assert shown.kind == "negative"

    @pytest.mark.parametrize("reason", [REASON_INSTALL_FAILED, REASON_STOPPED])
    def test_a_failure_before_the_restart_that_was_rolled_back(
        self, reason: str
    ) -> None:
        shown = outcome(
            _status("failed", reason=reason, error="pip exited 1.", rolled_back=True)
        )

        assert shown is not None
        assert shown.text == "Update to 0.10.1 failed: pip exited 1. Nothing changed."
        assert shown.kind == "warning"

    @pytest.mark.parametrize("phase", ["requested", "installing", "rolling_back"])
    def test_none_while_updating(self, phase: str) -> None:
        assert outcome(_status(phase)) is None

    def test_none_without_an_update(self) -> None:
        assert outcome(None) is None


class TestFailedHere:
    """A release that failed to start here is still offered, captioned."""

    def test_the_release_that_failed(self) -> None:
        assert failed_here(_rolled_back(), "0.10.1") == (
            "0.10.1 failed to start here on 7 Oct 14:04."
        )

    def test_after_a_double_failure_too(self) -> None:
        assert failed_here(_double_failure(), "0.10.1") == (
            "0.10.1 failed to start here on 7 Oct 14:04."
        )

    def test_another_release(self) -> None:
        assert failed_here(_rolled_back(), "0.10.2") is None

    @pytest.mark.parametrize(
        "status",
        [
            None,
            _status("done"),
            _status("failed", reason=REASON_INSTALL_FAILED, rolled_back=True),
            _status("rolling_back", reason=REASON_FAILED_TO_START),
        ],
    )
    def test_nothing_else_is_captioned(self, status: UpdateStatus | None) -> None:
        assert failed_here(status, "0.10.1") is None
