"""What Settings and the banner say after a Rollback (sp-rtk-base#241), in
the browser.

As in ``test_update_from_settings``, the test plays the host by writing
``status.json`` the way the updater does.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from sp_rtk_base import __version__ as running_app
from tests.e2e.conftest import E2E_UPDATE_APP
from tests.e2e.test_update_from_settings import _card, _host_writes, _open_settings

pytestmark = pytest.mark.e2e

FINISHED = datetime(2026, 10, 7, 14, 4).astimezone()
WHEN = "7 Oct 14:04"


@pytest.mark.usefixtures("clean_update")
class TestRollbackOutcomes:
    def test_rolled_back_once_until_dismissed_and_still_offered(
        self, page: Page, base_url: str, update_dir: Path
    ) -> None:
        _host_writes(
            update_dir,
            "failed",
            reason="failed_to_start",
            error=f"SP-Base {E2E_UPDATE_APP} didn't start within 90 s.",
            rolled_back=True,
            finished=FINISHED.isoformat(),
        )

        page.goto(f"{base_url}/")
        expect(page.get_by_test_id("update-banner-text")).to_have_text(
            f"Update to {E2E_UPDATE_APP} failed to start; still on {running_app}.",
            timeout=10_000,
        )
        page.get_by_test_id("update-banner-dismiss").click()
        expect(page.get_by_test_id("update-banner")).to_have_count(0)

        card = _open_settings(page, base_url)

        expect(card.get_by_test_id("update-outcome")).to_have_text(
            f"Update to {E2E_UPDATE_APP} failed to start; rolled back to "
            f"{running_app} on {WHEN}.",
            timeout=10_000,
        )
        expect(card.get_by_test_id("update-failed-here")).to_have_text(
            f"{E2E_UPDATE_APP} failed to start here on {WHEN}."
        )
        expect(card.get_by_test_id("update-button")).to_be_enabled()
        expect(page.get_by_test_id("update-banner")).to_have_count(0)

    def test_a_double_failure(
        self, page: Page, base_url: str, update_dir: Path
    ) -> None:
        _host_writes(
            update_dir,
            "failed",
            reason="failed_to_start",
            error=f"SP-Base {E2E_UPDATE_APP} didn't start within 90 s.",
            rollback_error=f"SP-Base {running_app} didn't start within 90 s.",
            finished=FINISHED.isoformat(),
        )

        card = _open_settings(page, base_url)

        expect(page.get_by_test_id("update-banner-text")).to_have_text(
            "Update failed and could not roll back. See Settings.", timeout=10_000
        )
        expect(card.get_by_test_id("update-outcome")).to_have_text(
            f"Update to {E2E_UPDATE_APP} failed and the rollback to {running_app} "
            f"failed too. On the base run: sudo deploy/upgrade.sh {running_app}, "
            "and see journalctl -u sp-rtk-base-update.",
            timeout=10_000,
        )

    def test_not_enough_disk_space(
        self, page: Page, base_url: str, update_dir: Path
    ) -> None:
        _host_writes(
            update_dir,
            "failed",
            reason="no_disk_space",
            error="Not enough disk space: the Update needs 420 MiB and 100 MiB is free.",
        )

        card = _open_settings(page, base_url)

        expect(card.get_by_test_id("update-outcome")).to_have_text(
            f"Update to {E2E_UPDATE_APP} not started: not enough disk space. "
            "Nothing changed.",
            timeout=10_000,
        )
        expect(page.get_by_test_id("update-banner")).to_have_count(0)
        expect(_card(page).get_by_test_id("update-failed-here")).to_have_count(0)

    def test_rolling_back(self, page: Page, base_url: str, update_dir: Path) -> None:
        _host_writes(update_dir, "rolling_back", reason="failed_to_start")

        page.goto(f"{base_url}/")

        expect(page.get_by_test_id("update-banner-text")).to_have_text(
            f"{E2E_UPDATE_APP} failed to start; rolling back to {running_app}…",
            timeout=10_000,
        )
