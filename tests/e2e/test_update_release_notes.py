"""Release notes for the Available update (sp-rtk-base#237), in the browser.

The e2e server reads GitHub from ``fake_pypi_dir`` too: ``CHANGELOG.md`` at
``E2E_UPDATE_APP``'s tag adds its section (with markup in it) and a beta's;
``E2E_RELEASE_BODY_APP`` has only a GitHub Release, ``E2E_NO_NOTES_APP``
nothing. The Relay doesn't change.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect
from sp_rtk_base_relay import __version__ as running_relay

from tests.e2e.conftest import (
    E2E_NO_NOTES_APP,
    E2E_RELEASE_BODY_APP,
    E2E_UPDATE_APP,
)

pytestmark = pytest.mark.e2e


@pytest.fixture()
def github_down(
    fake_pypi_dir: Path, tmp_path: Path, api_base_url: str
) -> Iterator[None]:
    """GitHub fails until the test ends (the changelog is gone)."""
    changelog = (
        fake_pypi_dir
        / "raw.githubusercontent.com/rodenj1/sp-rtk-base"
        / f"v{E2E_UPDATE_APP}/CHANGELOG.md"
    )
    saved = tmp_path / "CHANGELOG.md"
    shutil.move(changelog, saved)
    try:
        yield
    finally:
        shutil.move(saved, changelog)
        # Leave the notes loaded for the tests that follow.
        httpx.post(f"{api_base_url}/api/update/check", timeout=10.0)


def _open_notes(page: Page, base_url: str) -> Locator:
    page.goto(f"{base_url}/settings")
    notes = page.get_by_test_id("release-notes")
    expect(notes).to_be_visible(timeout=15_000)
    notes.get_by_role("button", name='Expand "Release notes"').click()
    return notes


def test_the_notes_start_collapsed(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/settings")

    notes = page.get_by_test_id("release-notes")
    expect(notes).to_be_visible(timeout=15_000)
    expect(notes.get_by_test_id("release-notes-sp-base")).to_be_hidden()


def test_every_release_up_to_the_target_newest_first(page: Page, base_url: str) -> None:
    notes = _open_notes(page, base_url)
    app = notes.get_by_test_id("release-notes-sp-base")

    expect(app).to_be_visible()
    headings = app.locator(".text-subtitle2")
    expect(headings).to_have_text(
        [
            f"SP-Base {E2E_UPDATE_APP} · 2026-10-20",
            f"SP-Base {E2E_NO_NOTES_APP}",
            f"SP-Base {E2E_RELEASE_BODY_APP} · 2026-10-15",
        ]
    )
    expect(app.get_by_text("Release notes before you Update.")).to_be_visible()
    expect(app.get_by_text("No notes for this release.")).to_be_visible()
    expect(app.get_by_text("Notes from the GitHub Release.")).to_be_visible()


def test_a_beta_folds_under_its_release(page: Page, base_url: str) -> None:
    app = _open_notes(page, base_url).get_by_test_id("release-notes-sp-base")

    expect(app.get_by_text(f"Includes {E2E_UPDATE_APP}-beta.1")).to_be_visible()
    expect(app.get_by_text("Notes written up in a beta.")).to_be_visible()


def test_markup_in_the_notes_shows_as_text(page: Page, base_url: str) -> None:
    app = _open_notes(page, base_url).get_by_test_id("release-notes-sp-base")

    expect(app.get_by_text("Markup shows as text: <b>not bold</b>")).to_be_visible()
    expect(app.locator("b")).to_have_count(0)
    expect(app.locator("img")).to_have_count(0)
    assert page.evaluate("window.__notesInjected") is None


def test_the_relay_tab(page: Page, base_url: str) -> None:
    notes = _open_notes(page, base_url)

    notes.get_by_test_id("release-notes-tab-relay").click()

    expect(notes.get_by_test_id("release-notes-relay")).to_have_text(
        f"The Relay stays on {running_relay}."
    )


@pytest.mark.usefixtures("github_down")
def test_github_failing_never_fails_the_check(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/settings")
    card = page.get_by_test_id("version-update-card")
    expect(card.get_by_test_id("update-check-line")).to_contain_text(
        "Last checked", timeout=15_000
    )

    card.get_by_test_id("update-check-now").click()
    notes = card.get_by_test_id("release-notes")
    expect(notes).to_be_visible(timeout=10_000)
    notes.get_by_role("button", name='Expand "Release notes"').click()

    expect(notes.get_by_test_id("release-notes-sp-base")).to_have_text(
        "SP-Base notes couldn't be loaded (GitHub didn't answer). Update still works.",
        timeout=10_000,
    )
    expect(card.get_by_test_id("update-check-line")).to_contain_text("Last checked")
    expect(card.get_by_test_id("version-row-sp-base")).to_contain_text(E2E_UPDATE_APP)
    expect(page.get_by_test_id("update-badge")).to_have_text(f"Update {E2E_UPDATE_APP}")
