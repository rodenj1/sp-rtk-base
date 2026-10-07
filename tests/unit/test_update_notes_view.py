"""What the Release notes expander shows (sp-rtk-base#237)."""

from __future__ import annotations

from datetime import datetime

from sp_rtk_base.services.update_check import UpdateCheck, UpdateCheckStatus
from sp_rtk_base.ui.update_status import NotesTab, notes_html, notes_tabs
from sp_rtk_base.update.release import ReleaseTarget
from sp_rtk_base.update.release_notes import (
    PackageNotes,
    PreReleaseNote,
    ReleaseNote,
    ReleaseNotes,
)

CHECKED_AT = datetime(2026, 10, 7, 9, 12).astimezone()

APP = PackageNotes(
    releases=(
        ReleaseNote(
            version="0.10.1",
            date="2026-10-02",
            source="changelog",
            body="### Fixed\n- Survey-in no longer stalls.",
        ),
        ReleaseNote(
            version="0.10.0",
            date="2026-09-20",
            source="changelog",
            body="### Added\n- **Update** from the web UI.",
            includes=(
                PreReleaseNote(version="0.10.0-beta.2", date="2026-09-15", body="- B2"),
                PreReleaseNote(version="0.10.0-beta.1", date=None, body="- B1"),
            ),
        ),
        ReleaseNote(version="0.9.2", date="2026-09-10", source="release", body="Hi"),
        ReleaseNote(version="0.9.1", date=None, source="none"),
        ReleaseNote(version="0.9.0.1", date=None, source="unavailable"),
    )
)
RELAY = PackageNotes(
    releases=(
        ReleaseNote(version="4.2.0", date="2026-09-18", source="changelog", body="x"),
    )
)


def _status(
    notes: ReleaseNotes | None, *, app: str = "0.10.1", relay: str = "4.2.0"
) -> UpdateCheckStatus:
    return UpdateCheckStatus(
        last_good=UpdateCheck(
            running_app="0.9.0",
            running_relay="4.1.0",
            running_python="3.11.2",
            target=ReleaseTarget(app=app, relay=relay),
            checked_at=CHECKED_AT,
            notes=notes,
        )
    )


def _tabs(app: PackageNotes = APP, relay: PackageNotes = RELAY) -> list[NotesTab]:
    tabs = notes_tabs(_status(ReleaseNotes(app=app, relay=relay)))
    assert tabs is not None
    return tabs


class TestTabs:
    def test_sp_base_then_relay(self) -> None:
        assert [t.label for t in _tabs()] == ["SP-Base", "Relay"]

    def test_none_without_an_available_update(self) -> None:
        assert notes_tabs(UpdateCheckStatus()) is None
        assert notes_tabs(_status(None, app="0.9.0", relay="4.1.0")) is None

    def test_none_before_notes_were_loaded(self) -> None:
        assert notes_tabs(_status(None)) is None


class TestReleases:
    def test_newest_first_headed_by_version_and_date(self) -> None:
        app = _tabs()[0]

        assert [r.heading for r in app.releases] == [
            "SP-Base 0.10.1 · 2026-10-02",
            "SP-Base 0.10.0 · 2026-09-20",
            "SP-Base 0.9.2 · 2026-09-10",
            "SP-Base 0.9.1",
            "SP-Base 0.9.0.1",
        ]
        assert _tabs()[1].releases[0].heading == "Relay 4.2.0 · 2026-09-18"

    def test_pre_releases_fold_under_their_stable_release(self) -> None:
        release = _tabs()[0].releases[1]

        assert release.includes == "Includes 0.10.0-beta.1, 0.10.0-beta.2"
        assert [p.heading for p in release.pre_releases] == [
            "0.10.0-beta.2 · 2026-09-15",
            "0.10.0-beta.1",
        ]
        assert release.pre_releases[0].html == "<ul>\n<li>B2</li>\n</ul>\n"
        assert _tabs()[0].releases[0].includes is None

    def test_notes_are_rendered_markdown(self) -> None:
        release = _tabs()[0].releases[1]

        assert release.html == (
            "<h3>Added</h3>\n\n<ul>\n<li><strong>Update</strong> from the web UI.</li>\n"
            "</ul>\n"
        )
        assert release.text is None

    def test_a_release_body_reads_like_a_section(self) -> None:
        assert _tabs()[0].releases[2].html == "<p>Hi</p>\n"

    def test_a_release_without_notes(self) -> None:
        release = _tabs()[0].releases[3]

        assert release.html is None
        assert release.text == "No notes for this release."

    def test_a_release_whose_notes_couldnt_be_loaded(self) -> None:
        release = _tabs()[0].releases[4]

        assert release.html is None
        assert release.text == "Notes for this release couldn't be loaded."


class TestWholeTab:
    def test_github_failing(self) -> None:
        tabs = _tabs(app=PackageNotes(loaded=False, error="HTTP 403"))

        assert tabs[0].text == (
            "SP-Base notes couldn't be loaded (GitHub didn't answer). "
            "Update still works."
        )
        assert tabs[0].releases == []
        assert tabs[1].text is None

    def test_a_relay_that_doesnt_change(self) -> None:
        tabs = notes_tabs(
            _status(ReleaseNotes(app=APP, relay=PackageNotes()), relay="4.1.0")
        )

        assert tabs is not None
        assert tabs[1].text == "The Relay stays on 4.1.0."
        assert tabs[1].releases == []


class TestRawHtmlIsText:
    """A changelog can't put markup on the page: raw HTML shows as text."""

    def test_tags_are_escaped(self) -> None:
        html = notes_html("- <b>not bold</b> <script>alert(1)</script>")

        assert html == (
            "<ul>\n<li>&lt;b&gt;not bold&lt;/b&gt; "
            "&lt;script&gt;alert(1)&lt;/script&gt;</li>\n</ul>\n"
        )

    def test_a_block_of_html(self) -> None:
        html = notes_html('<div onclick="steal()">x</div>\n<img src=x onerror=y>')

        assert "<div" not in html
        assert "<img" not in html
        assert "&lt;div onclick=" in html

    def test_angle_brackets_in_code_show_once_escaped(self) -> None:
        # From the recorded 0.8.0 changelog: ``address=/#/<ap_gateway_ip>``.
        html = notes_html("- feeds `address=/#/<ap_gateway_ip>` to dnsmasq")

        assert "<code>address=/#/&lt;ap_gateway_ip&gt;</code>" in html

    def test_javascript_links_are_dropped(self) -> None:
        html = notes_html("[click](javascript:alert(1)) [ok](https://github.com)")

        assert "javascript:" not in html
        assert '<a href="https://github.com">ok</a>' in html
