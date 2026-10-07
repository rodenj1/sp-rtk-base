"""Release notes for the Available update (sp-rtk-base#237).

Runs against GitHub as recorded on 2026-10-07 (``tests/fixtures/github``:
``CHANGELOG.md`` at sp-rtk-base v0.9.0 and Relay v4.1.0, and the Releases of
the versions the changelogs miss) and PyPI as recorded the same day. Ranges
reach back far enough to cross the known gaps: sp-rtk-base 0.5.1 (a Release
body, no section), the withdrawn 0.3.23 (nothing anywhere), Relay 2.1.3 (a
Release body, no section) and the 0.4.0 betas.
"""

from __future__ import annotations

import pytest

from sp_rtk_base.update.release import ReleaseTarget
from sp_rtk_base.update.release_notes import (
    PackageNotes,
    ReleaseNote,
    package_notes,
    release_notes,
)
from tests.fixtures.fake_github import FakeGitHub, Web, changelog_url, release_url
from tests.fixtures.fake_pypi import FakePyPI


@pytest.fixture()
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture()
def pypi() -> FakePyPI:
    return FakePyPI()


@pytest.fixture()
def web(pypi: FakePyPI, github: FakeGitHub) -> Web:
    return Web(pypi, github)


def _versions(notes: PackageNotes) -> list[str]:
    return [r.version for r in notes.releases]


def _note(notes: PackageNotes, version: str) -> ReleaseNote:
    return next(r for r in notes.releases if r.version == version)


class TestFromTheChangelog:
    def test_every_release_after_the_running_one_up_to_the_target(
        self, web: Web
    ) -> None:
        notes = package_notes(web, "sp-rtk-base", running="0.7.0", target="0.9.0")

        assert notes.loaded
        assert _versions(notes) == ["0.9.0", "0.8.1", "0.8.0"]

    def test_each_release_has_its_date_and_its_own_section(self, web: Web) -> None:
        notes = package_notes(web, "sp-rtk-base", running="0.8.0", target="0.9.0")

        note = _note(notes, "0.8.1")
        assert note.date == "2026-10-05"
        assert note.source == "changelog"
        assert note.body.startswith(
            "### Security: destination passwords are write-only\n\n"
        )
        # Its section ends where the next release's begins.
        assert "## v0.8.0" not in note.body

    def test_one_changelog_fetch_at_the_target_tag(
        self, web: Web, github: FakeGitHub
    ) -> None:
        package_notes(web, "sp-rtk-base", running="0.6.0", target="0.9.0")

        assert github.fetched == [changelog_url("sp-rtk-base", "0.9.0")]

    def test_the_relay(self, web: Web) -> None:
        notes = package_notes(web, "sp-rtk-base-relay", running="3.1.1", target="4.1.0")

        assert _versions(notes) == ["4.1.0", "4.0.0", "3.2.0"]
        assert _note(notes, "4.0.0").date == "2026-10-01"


class TestChangelogShape:
    def test_a_heading_that_isnt_a_release_ends_the_section_above(
        self, web: Web, github: FakeGitHub
    ) -> None:
        github.publish_changelog(
            "sp-rtk-base",
            "0.10.0",
            "## v0.10.0 (2026-10-20)\n\n- Ten.\n\n"
            "## vNext (unreleased)\n\n- Not yet.\n\n"
            "## Upgrading\n\n- Read this.",
            on_top_of="0.9.0",
        )

        notes = package_notes(web, "sp-rtk-base", running="0.9.0", target="0.10.0")

        assert [(r.version, r.body) for r in notes.releases] == [("0.10.0", "- Ten.")]


class TestKnownGaps:
    """PyPI versions the recorded changelogs have no section for."""

    def test_a_missing_section_falls_back_to_the_release_body(self, web: Web) -> None:
        notes = package_notes(web, "sp-rtk-base", running="0.5.0", target="0.9.0")

        note = _note(notes, "0.5.1")
        assert note.source == "release"
        assert note.date == "2026-08-26"
        assert note.body.startswith(
            "## What's Changed\n* fix(ublox): persist RTCM message/port selection"
        )

    def test_the_relay_gap_falls_back_too(self, web: Web) -> None:
        notes = package_notes(web, "sp-rtk-base-relay", running="2.1.2", target="4.1.0")

        assert _versions(notes) == [
            "4.1.0", "4.0.0", "3.2.0", "3.1.1", "3.1.0", "3.0.0", "2.1.3",
        ]  # fmt: skip
        note = _note(notes, "2.1.3")
        assert note.source == "release"
        assert note.body.startswith("## Summary\n\nReplaces v2.1.2's hardcoded 5 s")

    def test_0_3_23_has_no_notes_anywhere(self, web: Web, github: FakeGitHub) -> None:
        notes = package_notes(web, "sp-rtk-base", running="0.3.22", target="0.9.0")

        note = _note(notes, "0.3.23")
        assert note.source == "none"
        assert note.body == ""
        # Only the gaps cost a REST call.
        assert github.fetched == [
            changelog_url("sp-rtk-base", "0.9.0"),
            release_url("sp-rtk-base", "0.5.1"),
            release_url("sp-rtk-base", "0.3.23"),
        ]

    def test_an_empty_release_body_is_no_notes(
        self, web: Web, github: FakeGitHub
    ) -> None:
        github.publish_release("sp-rtk-base", "0.5.1", "  \n", published="2026-08-26")

        note = _note(
            package_notes(web, "sp-rtk-base", running="0.5.0", target="0.9.0"), "0.5.1"
        )

        assert note.source == "none"
        assert note.date == "2026-08-26"


class TestPreReleases:
    def test_pre_release_sections_fold_under_their_stable_release(
        self, web: Web
    ) -> None:
        notes = package_notes(web, "sp-rtk-base", running="0.3.30", target="0.9.0")

        note = _note(notes, "0.4.0")
        assert note.source == "changelog"
        assert [p.version for p in note.includes] == [
            "0.4.0-beta.3",
            "0.4.0-beta.2",
            "0.4.0-beta.1",
        ]
        assert note.includes[2].date == "2026-07-31"
        assert note.includes[2].body.startswith(
            "- fix(release): use SemVer-compliant pre-release format"
        )
        # Never a release of their own.
        assert "0.4.0b1" not in _versions(notes)
        assert not any("beta" in v for v in _versions(notes))

    def test_a_pre_release_of_the_target(self, web: Web, github: FakeGitHub) -> None:
        github.publish_changelog(
            "sp-rtk-base",
            "0.10.0",
            "## v0.10.0 (2026-10-20)\n\n- Update from the web UI.\n\n"
            "## v0.10.0-beta.2 (2026-10-15)\n\n- Beta two.\n\n"
            "## v0.10.0-beta.1 (2026-10-10)\n\n- Beta one.\n",
            on_top_of="0.9.0",
        )

        notes = package_notes(web, "sp-rtk-base", running="0.9.0", target="0.10.0")

        (note,) = notes.releases
        assert note.version == "0.10.0"
        assert note.body == "- Update from the web UI."
        assert [(p.version, p.body) for p in note.includes] == [
            ("0.10.0-beta.2", "- Beta two."),
            ("0.10.0-beta.1", "- Beta one."),
        ]

    def test_pre_releases_outside_the_range_are_left_out(self, web: Web) -> None:
        notes = package_notes(web, "sp-rtk-base", running="0.4.0", target="0.9.0")

        assert all(not r.includes for r in notes.releases)


class TestGitHubFailing:
    @pytest.mark.parametrize("code", [403, 429, 500])
    def test_the_changelog_failing_means_not_loaded(
        self, web: Web, github: FakeGitHub, code: int
    ) -> None:
        github.fail(changelog_url("sp-rtk-base", "0.9.0"), code)

        notes = package_notes(web, "sp-rtk-base", running="0.8.0", target="0.9.0")

        assert not notes.loaded
        assert notes.releases == ()
        assert notes.error is not None

    def test_no_changelog_at_the_target_tag_means_not_loaded(self, web: Web) -> None:
        notes = package_notes(web, "sp-rtk-base", running="0.9.0", target="0.9.1")

        assert not notes.loaded

    def test_a_rate_limited_fallback_marks_the_gaps_unavailable(
        self, web: Web, github: FakeGitHub
    ) -> None:
        github.fail(release_url("sp-rtk-base", "0.5.1"), 403)

        notes = package_notes(web, "sp-rtk-base", running="0.3.22", target="0.9.0")

        assert notes.loaded
        assert _note(notes, "0.5.1").source == "unavailable"
        assert _note(notes, "0.3.23").source == "unavailable"
        assert _note(notes, "0.6.0").source == "changelog"
        # Once rate limited, it stops asking.
        assert release_url("sp-rtk-base", "0.3.23") not in github.fetched

    def test_a_fallback_that_isnt_json(self, web: Web, github: FakeGitHub) -> None:
        github.files[release_url("sp-rtk-base", "0.5.1")] = b"<html>busy</html>"

        notes = package_notes(web, "sp-rtk-base", running="0.5.0", target="0.9.0")

        assert _note(notes, "0.5.1").source == "unavailable"

    def test_a_fallback_answer_that_isnt_a_release(
        self, web: Web, github: FakeGitHub
    ) -> None:
        github.files[release_url("sp-rtk-base", "0.5.1")] = b"[]"

        notes = package_notes(web, "sp-rtk-base", running="0.5.0", target="0.9.0")

        assert _note(notes, "0.5.1").source == "none"

    def test_pypi_failing_leaves_the_changelog_sections(
        self, web: Web, pypi: FakePyPI
    ) -> None:
        pypi.down.add("https://pypi.org/pypi/sp-rtk-base/json")

        notes = package_notes(web, "sp-rtk-base", running="0.5.0", target="0.9.0")

        assert notes.loaded
        assert "0.5.2" in _versions(notes)


class TestAnUpdate:
    def test_both_packages(self, web: Web) -> None:
        notes = release_notes(
            web, "0.8.1", "4.0.0", ReleaseTarget(app="0.9.0", relay="4.1.0")
        )

        assert _versions(notes.app) == ["0.9.0"]
        assert _versions(notes.relay) == ["4.1.0"]

    def test_an_unchanged_relay_has_no_notes_and_costs_nothing(
        self, web: Web, github: FakeGitHub
    ) -> None:
        notes = release_notes(
            web, "0.8.1", "4.1.0", ReleaseTarget(app="0.9.0", relay="4.1.0")
        )

        assert notes.relay == PackageNotes()
        assert github.fetched == [changelog_url("sp-rtk-base", "0.9.0")]

    def test_a_failure_in_one_repo_leaves_the_other(
        self, web: Web, github: FakeGitHub
    ) -> None:
        github.fail(changelog_url("sp-rtk-base-relay", "4.1.0"), 429)

        notes = release_notes(
            web, "0.8.1", "4.0.0", ReleaseTarget(app="0.9.0", relay="4.1.0")
        )

        assert notes.app.loaded
        assert not notes.relay.loaded

    def test_a_running_version_that_isnt_a_version(self, web: Web) -> None:
        notes = package_notes(web, "sp-rtk-base", running="unknown", target="0.9.0")

        assert not notes.loaded

    def test_markup_in_notes_is_kept_as_written(self, web: Web) -> None:
        # Escaping is the page's job (``ui.update_status.notes_html``).
        notes = package_notes(web, "sp-rtk-base", running="0.3.30", target="0.9.0")

        beta3 = _note(notes, "0.4.0").includes[0]
        assert "nmcli connection up id <ap_ssid> requires" in beta3.body
