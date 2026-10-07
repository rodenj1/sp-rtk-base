"""Checking for an Available update (sp-rtk-base#236), in the browser.

The e2e server reads PyPI from a directory (``fake_pypi_dir``) holding the
recorded responses plus an Available update, ``E2E_UPDATE_APP``, and a newer
release that needs a Python no base runs.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from sp_rtk_base import __version__ as running_app
from tests.e2e.conftest import E2E_NEEDS_PYTHON_APP, E2E_UPDATE_APP

pytestmark = pytest.mark.e2e

RUNNING_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"


@pytest.fixture()
def pypi_down(fake_pypi_dir: Path, tmp_path: Path, api_base_url: str) -> Iterator[None]:
    """PyPI fails until the test ends (the app index is gone)."""
    index = fake_pypi_dir / "sp-rtk-base.json"
    saved = tmp_path / "sp-rtk-base.json"
    shutil.move(index, saved)
    try:
        yield
    finally:
        shutil.move(saved, index)
        # Leave a good check behind for the tests that follow.
        httpx.post(f"{api_base_url}/api/update/check", timeout=10.0)


def _card(page: Page) -> Locator:
    return page.get_by_test_id("version-update-card")


@pytest.mark.parametrize("path", ["/", "/input", "/outputs", "/survey", "/settings"])
def test_the_badge_shows_on_every_page(page: Page, base_url: str, path: str) -> None:
    page.goto(f"{base_url}{path}")

    expect(page.get_by_test_id("update-badge")).to_have_text(
        f"Update {E2E_UPDATE_APP}", timeout=15_000
    )


def test_the_badge_links_to_settings(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/")

    page.get_by_test_id("update-badge").click()

    expect(page).to_have_url(f"{base_url}/settings", timeout=10_000)


def test_the_card_shows_current_to_target(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/settings")
    card = _card(page)

    expect(card.get_by_text("Version & Update")).to_be_visible(timeout=15_000)
    expect(card.get_by_test_id("update-check-line")).to_contain_text("Last checked")
    app_row = card.get_by_test_id("version-row-sp-base")
    expect(app_row).to_contain_text(running_app)
    expect(app_row).to_contain_text(E2E_UPDATE_APP)
    expect(card.get_by_test_id("version-row-sp-base-relay")).to_contain_text("4.1.0")
    expect(card.get_by_test_id("update-python-note")).to_have_text(
        f"{E2E_NEEDS_PYTHON_APP} needs Python 3.99; this base runs {RUNNING_PYTHON}. "
        f"Offering {E2E_UPDATE_APP}, the newest release that runs here."
    )


def test_the_result_is_in_the_api(api_base_url: str) -> None:
    body = httpx.get(f"{api_base_url}/api/update", timeout=5.0).json()

    assert body["last_good"]["available"] is True
    assert body["last_good"]["target"]["app"] == E2E_UPDATE_APP


@pytest.mark.usefixtures("pypi_down")
def test_a_failed_check_shows_on_settings_only(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/settings")
    card = _card(page)
    expect(card.get_by_test_id("update-check-line")).to_contain_text(
        "Last checked", timeout=15_000
    )

    card.get_by_test_id("update-check-now").click()

    expect(card.get_by_test_id("update-check-line")).to_contain_text(
        "Couldn't check (last checked ", timeout=10_000
    )
    # The last good result stays shown, and the header says nothing new.
    expect(card.get_by_test_id("version-row-sp-base")).to_contain_text(E2E_UPDATE_APP)
    expect(page.get_by_test_id("update-badge")).to_have_text(f"Update {E2E_UPDATE_APP}")
    expect(page.locator(".q-header").get_by_text("Couldn't check")).to_have_count(0)

    page.goto(f"{base_url}/")
    expect(page.get_by_test_id("update-badge")).to_have_text(
        f"Update {E2E_UPDATE_APP}", timeout=15_000
    )
