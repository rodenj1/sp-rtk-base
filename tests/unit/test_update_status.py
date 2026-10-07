"""What Settings' Version & Update card and the header badge say."""

from __future__ import annotations

from datetime import datetime

from sp_rtk_base.services.update_check import UpdateCheck, UpdateCheckStatus
from sp_rtk_base.ui.update_status import (
    VersionRow,
    badge_text,
    check_line,
    python_note,
    up_to_date,
    version_rows,
)
from sp_rtk_base.update.release import NewerNeedsPython, ReleaseTarget

CHECKED_AT = datetime(2026, 10, 7, 9, 12).astimezone()


def _check(
    app: str = "0.10.1",
    relay: str = "4.2.0",
    newer: NewerNeedsPython | None = None,
    python: str = "3.11.2",
) -> UpdateCheck:
    return UpdateCheck(
        running_app="0.9.0",
        running_relay="4.1.0",
        running_python=python,
        target=ReleaseTarget(app=app, relay=relay, newer_needs_python=newer),
        checked_at=CHECKED_AT,
    )


AVAILABLE = UpdateCheckStatus(last_good=_check())
UP_TO_DATE = UpdateCheckStatus(last_good=_check(app="0.9.0", relay="4.1.0"))
FAILED_AFTER_GOOD = UpdateCheckStatus(
    last_good=_check(), last_check_failed=True, error="HTTP 503"
)


class TestCheckLine:
    def test_last_checked_time(self) -> None:
        assert check_line(AVAILABLE).text == "Last checked 7 Oct 09:12"
        assert not check_line(AVAILABLE).failed

    def test_failed_check_names_the_last_good_one(self) -> None:
        line = check_line(FAILED_AFTER_GOOD)

        assert line.text == "Couldn't check (last checked 7 Oct 09:12)"
        assert line.failed

    def test_failed_first_check(self) -> None:
        line = check_line(UpdateCheckStatus(last_check_failed=True, error="x"))

        assert line.text == "Couldn't check"
        assert line.failed

    def test_while_checking(self) -> None:
        line = check_line(FAILED_AFTER_GOOD.model_copy(update={"checking": True}))

        assert line.text == "Checking…"
        assert line.checking
        assert not line.failed

    def test_before_the_first_check(self) -> None:
        assert check_line(UpdateCheckStatus()).text == "Not checked yet"


class TestVersionRows:
    def test_current_to_target(self) -> None:
        assert version_rows(AVAILABLE, "0.9.0", "4.1.0") == [
            VersionRow("SP-Base", "0.9.0", "0.10.1"),
            VersionRow("SP-Base Relay", "4.1.0", "4.2.0"),
        ]

    def test_a_relay_that_stays_has_no_arrow(self) -> None:
        status = UpdateCheckStatus(last_good=_check(relay="4.1.0"))

        assert version_rows(status, "0.9.0", "4.1.0")[1] == VersionRow(
            "SP-Base Relay", "4.1.0", None
        )

    def test_up_to_date_has_no_arrows(self) -> None:
        assert version_rows(UP_TO_DATE, "0.9.0", "4.1.0") == [
            VersionRow("SP-Base", "0.9.0", None),
            VersionRow("SP-Base Relay", "4.1.0", None),
        ]
        assert up_to_date(UP_TO_DATE)

    def test_a_failed_check_still_shows_the_last_good_target(self) -> None:
        assert version_rows(FAILED_AFTER_GOOD, "0.9.0", "4.1.0")[0].target == "0.10.1"

    def test_before_the_first_check_the_running_versions(self) -> None:
        assert version_rows(UpdateCheckStatus(), "0.9.0", "4.1.0") == [
            VersionRow("SP-Base", "0.9.0", None),
            VersionRow("SP-Base Relay", "4.1.0", None),
        ]
        assert not up_to_date(UpdateCheckStatus())

    def test_available_is_not_up_to_date(self) -> None:
        assert not up_to_date(AVAILABLE)


class TestBadge:
    def test_names_the_available_update(self) -> None:
        assert badge_text(AVAILABLE) == "Update 0.10.1"

    def test_none_when_up_to_date(self) -> None:
        assert badge_text(UP_TO_DATE) is None

    def test_never_shows_a_failed_check(self) -> None:
        assert badge_text(UpdateCheckStatus(last_check_failed=True)) is None
        assert badge_text(FAILED_AFTER_GOOD) == "Update 0.10.1"


class TestPythonNote:
    def test_names_the_newer_release_and_both_pythons(self) -> None:
        status = UpdateCheckStatus(
            last_good=_check(newer=NewerNeedsPython(version="0.11.0", python="3.12"))
        )

        assert python_note(status) == (
            "0.11.0 needs Python 3.12; this base runs 3.11. Offering 0.10.1, "
            "the newest release that runs here."
        )

    def test_without_an_update_that_fits(self) -> None:
        status = UpdateCheckStatus(
            last_good=_check(
                app="0.9.0",
                relay="4.1.0",
                newer=NewerNeedsPython(version="0.11.0", python="3.12"),
            )
        )

        assert python_note(status) == "0.11.0 needs Python 3.12; this base runs 3.11."

    def test_an_unreadable_python_floor(self) -> None:
        status = UpdateCheckStatus(
            last_good=_check(
                app="0.9.0",
                relay="4.1.0",
                newer=NewerNeedsPython(version="0.11.0", python=None),
            )
        )

        assert python_note(status) == (
            "0.11.0 needs a newer Python; this base runs 3.11."
        )

    def test_a_patch_level_floor_names_both_patches(self) -> None:
        status = UpdateCheckStatus(
            last_good=_check(
                app="0.9.0",
                relay="4.1.0",
                newer=NewerNeedsPython(version="0.10.0", python="3.11.4"),
            )
        )

        assert python_note(status) == (
            "0.10.0 needs Python 3.11.4; this base runs 3.11.2."
        )

    def test_none_without_one(self) -> None:
        assert python_note(AVAILABLE) is None
        assert python_note(UpdateCheckStatus()) is None
