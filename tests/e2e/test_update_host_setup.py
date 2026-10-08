"""The Host setup gate on Settings (sp-rtk-base#242), in the browser.

The test plays the host: it writes the units' state into the fake Host
setup file (instead of systemd), and changes what the Available update
needs through ``deploy/plumbing-version`` at its tag in ``fake_pypi_dir``,
then checks again.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from sp_rtk_base.update.host_setup import PLUMBING_VERSION
from tests.e2e.conftest import E2E_UPDATE_APP

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("clean_update")]

INSTALL_COMMAND = (
    "curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/"
    "deploy/install.sh | sudo bash"
)
MISSING_TEXT = (
    "Update needs a one-time setup on this host. Run this on the base, then come back:"
)
TURNED_OFF_TEXT = "Update is turned off on this host."
OUTDATED_TEXT = (
    "This release needs newer Host setup, a one-time step. Run this on the base, "
    "then come back:"
)
UNKNOWN_TEXT = "Couldn't check this release's host requirements. Check again."
DRIFT_TEXT = (
    "This version needs newer Host setup than the host has. Some features "
    "may not work until you run:"
)


def _host_is(path: Path, *, installed: bool, enabled: bool, plumbing: int) -> None:
    path.write_text(
        json.dumps({"installed": installed, "enabled": enabled, "plumbing": plumbing})
    )


@pytest.fixture()
def requirement(
    fake_pypi_dir: Path, tmp_path: Path, api_base_url: str
) -> Iterator[Path]:
    """The Available update's ``deploy/plumbing-version``, put back (and
    checked again) after the test."""
    path = (
        fake_pypi_dir
        / "raw.githubusercontent.com/rodenj1/sp-rtk-base"
        / f"v{E2E_UPDATE_APP}/deploy/plumbing-version"
    )
    saved = tmp_path / "plumbing-version"
    shutil.copy(path, saved)
    try:
        yield path
    finally:
        shutil.copy(saved, path)
        httpx.post(f"{api_base_url}/api/update/check", timeout=10.0)


def _settings(page: Page, base_url: str) -> Locator:
    page.goto(f"{base_url}/settings")
    card = page.get_by_test_id("version-update-card")
    expect(card.get_by_test_id("update-button")).to_be_visible(timeout=15_000)
    return card


def _check_now(card: Locator) -> None:
    card.get_by_test_id("update-check-now").click()
    expect(card.get_by_test_id("update-check-line")).to_contain_text(
        "Last checked", timeout=10_000
    )


def _the_rest_still_works(page: Page, card: Locator) -> None:
    """The badge, the check and the Release notes work in every state."""
    expect(page.get_by_test_id("update-badge")).to_have_text(f"Update {E2E_UPDATE_APP}")
    expect(card.get_by_test_id("version-row-sp-base")).to_contain_text(E2E_UPDATE_APP)
    expect(card.get_by_test_id("update-check-now")).to_be_enabled()
    notes = card.get_by_test_id("release-notes")
    notes.get_by_role("button", name='Expand "Release notes"').click()
    expect(notes.get_by_test_id("release-notes-sp-base")).to_contain_text(
        E2E_UPDATE_APP
    )


def _blocked(card: Locator, text: str) -> None:
    expect(card.get_by_test_id("update-refusal")).to_have_text(text, timeout=10_000)
    expect(card.get_by_test_id("update-button")).to_be_disabled()


def test_a_set_up_host_can_update(page: Page, base_url: str, host_setup: Path) -> None:
    card = _settings(page, base_url)

    expect(card.get_by_test_id("update-button")).to_be_enabled(timeout=10_000)
    expect(card.get_by_test_id("update-refusal")).to_have_count(0)
    expect(card.get_by_test_id("update-drift")).to_have_count(0)


def test_unit_missing(page: Page, base_url: str, host_setup: Path) -> None:
    _host_is(host_setup, installed=False, enabled=False, plumbing=0)

    card = _settings(page, base_url)

    _blocked(card, MISSING_TEXT)
    expect(card.get_by_test_id("update-refusal-command")).to_contain_text(
        INSTALL_COMMAND
    )
    # The block shows the command; the drift warning doesn't repeat it.
    expect(card.get_by_test_id("update-drift")).to_have_count(0)
    _the_rest_still_works(page, card)
    _check_now(card)
    _blocked(card, MISSING_TEXT)


def test_turned_off_on_this_host(page: Page, base_url: str, host_setup: Path) -> None:
    _host_is(host_setup, installed=True, enabled=False, plumbing=PLUMBING_VERSION)

    card = _settings(page, base_url)

    _blocked(card, TURNED_OFF_TEXT)
    expect(card.get_by_test_id("update-refusal-command")).to_have_count(0)
    _the_rest_still_works(page, card)


def test_the_release_needs_newer_host_setup(
    page: Page, base_url: str, host_setup: Path, requirement: Path
) -> None:
    requirement.write_text(f"{PLUMBING_VERSION + 1}\n")
    card = _settings(page, base_url)

    _check_now(card)

    _blocked(card, OUTDATED_TEXT)
    expect(card.get_by_test_id("update-refusal-command")).to_contain_text(
        INSTALL_COMMAND
    )
    _the_rest_still_works(page, card)


def test_the_requirement_cant_be_read(
    page: Page, base_url: str, host_setup: Path, requirement: Path
) -> None:
    requirement.unlink()
    card = _settings(page, base_url)

    _check_now(card)

    _blocked(card, UNKNOWN_TEXT)
    expect(card.get_by_test_id("update-refusal-command")).to_have_count(0)
    _the_rest_still_works(page, card)

    # Check again, once the requirement can be read.
    requirement.write_text(f"{PLUMBING_VERSION}\n")
    _check_now(card)
    expect(card.get_by_test_id("update-button")).to_be_enabled(timeout=10_000)


def test_missing_comes_before_turned_off_and_the_release(
    page: Page, base_url: str, host_setup: Path, requirement: Path
) -> None:
    requirement.unlink()
    _host_is(host_setup, installed=False, enabled=False, plumbing=0)
    card = _settings(page, base_url)

    _check_now(card)

    _blocked(card, MISSING_TEXT)


def test_drift_warns_with_the_command(
    page: Page, base_url: str, host_setup: Path, requirement: Path
) -> None:
    """The running version needs newer Host setup than the host has (a
    manual pip install), but the Available update itself would fit."""
    requirement.write_text("0\n")
    _host_is(host_setup, installed=True, enabled=True, plumbing=PLUMBING_VERSION - 1)
    card = _settings(page, base_url)

    _check_now(card)

    expect(card.get_by_test_id("update-drift")).to_have_text(DRIFT_TEXT, timeout=10_000)
    expect(card.get_by_test_id("update-drift-command")).to_contain_text(INSTALL_COMMAND)
    expect(card.get_by_test_id("update-button")).to_be_enabled()


def test_the_api_refuses_too(api_base_url: str, host_setup: Path) -> None:
    _host_is(host_setup, installed=False, enabled=False, plumbing=0)

    response = httpx.post(
        f"{api_base_url}/api/update",
        json={"app": E2E_UPDATE_APP, "relay": "4.1.0"},
        timeout=10.0,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "host_setup_missing"
    assert response.json()["command"] == INSTALL_COMMAND
