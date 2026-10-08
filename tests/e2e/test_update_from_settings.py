"""Update from Settings (sp-rtk-base#240), in the browser.

The e2e server's update directory is the fake update backend: the test
plays the host, reading the request file the app writes and driving the
Update by writing ``status.json`` the way the updater does.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from sp_rtk_base import __version__ as running_app
from tests.e2e.conftest import E2E_UPDATE_APP

pytestmark = pytest.mark.e2e

RELAY = "4.1.0"
"""The Relay the e2e server runs; the Update leaves it unchanged."""

STEPS = [
    ("requested", "Waiting for the host… (step 1 of 5)"),
    ("resolving", "Checking the release… (step 2 of 5)"),
    ("installing", "Installing… (step 3 of 5)"),
    ("restarting", "Restarting… (step 4 of 5)"),
    ("verifying", "Checking it started… (step 5 of 5)"),
]


def _host_writes(
    update_dir: Path, phase: str, *, at: datetime | None = None, **extra: object
) -> None:
    status = {
        "format": 1,
        "phase": phase,
        "from": {"app": running_app, "relay": RELAY},
        "to": {"app": E2E_UPDATE_APP, "relay": RELAY},
        "updated_at": (at or datetime.now(timezone.utc)).isoformat(),
        **extra,
    }
    (update_dir / "status.json").write_text(json.dumps(status))


def _card(page: Page) -> Locator:
    return page.get_by_test_id("version-update-card")


def _open_settings(page: Page, base_url: str) -> Locator:
    page.goto(f"{base_url}/settings")
    card = _card(page)
    expect(card.get_by_test_id("update-button")).to_be_visible(timeout=15_000)
    return card


@pytest.fixture()
def relay_running(api_base_url: str, clean_config: None) -> Iterator[None]:
    """The Relay runs, from a TCP input (a listener here) to a TCP server output."""
    source = socket.create_server(("127.0.0.1", 0))
    port = source.getsockname()[1]
    resp = httpx.put(
        f"{api_base_url}/api/input",
        json={"source": "tcp", "config": {"host": "127.0.0.1", "port": port}},
        timeout=5.0,
    )
    assert resp.status_code in (200, 201), resp.text
    resp = httpx.post(
        f"{api_base_url}/api/destinations",
        json={
            "name": "e2e-update-dest",
            "type": "tcp_server",
            "enabled": True,
            "filter": {"mode": "pass_all", "message_ids": []},
            "config": {"host": "0.0.0.0", "port": 5097, "max_clients": 5},
        },
        timeout=5.0,
    )
    assert resp.status_code == 201, resp.text
    resp = httpx.post(f"{api_base_url}/api/relay/start", timeout=15.0)
    if resp.status_code != 200:
        source.close()
    assert resp.status_code == 200, resp.text
    try:
        yield
    finally:
        httpx.post(f"{api_base_url}/api/relay/stop", timeout=15.0)
        source.close()


@pytest.mark.usefixtures("clean_update")
class TestRequest:
    def test_confirm_names_both_version_changes(
        self, page: Page, base_url: str
    ) -> None:
        card = _open_settings(page, base_url)
        expect(card.get_by_test_id("update-button")).to_have_text(
            f"system_updateUpdate to {E2E_UPDATE_APP}"
        )

        card.get_by_test_id("update-button").click()

        expect(page.get_by_test_id("update-confirm-text")).to_have_text(
            f"SP-Base {running_app} → {E2E_UPDATE_APP}, Relay {RELAY} (unchanged). "
            "The base restarts; this page reconnects by itself."
        )
        expect(page.get_by_test_id("update-confirm-relay-warning")).to_have_count(0)

    @pytest.mark.usefixtures("relay_running")
    def test_confirm_warns_when_the_relay_runs(self, page: Page, base_url: str) -> None:
        card = _open_settings(page, base_url)

        card.get_by_test_id("update-button").click()

        expect(page.get_by_test_id("update-confirm-relay-warning")).to_have_text(
            "The Relay is running. Corrections stop for about a minute "
            "and resume on their own."
        )

    def test_cancel_writes_nothing(
        self, page: Page, base_url: str, update_dir: Path
    ) -> None:
        card = _open_settings(page, base_url)
        card.get_by_test_id("update-button").click()

        page.get_by_test_id("update-confirm-cancel").click()

        expect(page.get_by_test_id("update-confirm")).to_have_count(0)
        assert not (update_dir / "request.json").exists()

    def test_confirm_writes_the_request_and_enters_updating(
        self, page: Page, base_url: str, update_dir: Path
    ) -> None:
        card = _open_settings(page, base_url)
        card.get_by_test_id("update-button").click()

        page.get_by_test_id("update-confirm-go").click()

        expect(page.get_by_test_id("update-banner")).to_contain_text(
            f"Updating to {E2E_UPDATE_APP}…", timeout=10_000
        )
        request = json.loads((update_dir / "request.json").read_text())
        assert (request["app"], request["relay"]) == (E2E_UPDATE_APP, RELAY)
        status = json.loads((update_dir / "status.json").read_text())
        assert status["phase"] == "requested"
        expect(page.get_by_test_id("update-badge")).to_have_text("Updating…")
        expect(card.get_by_test_id("update-progress")).to_have_text(
            "Waiting for the host… (step 1 of 5)"
        )
        expect(card.get_by_test_id("update-button")).to_be_disabled()
        expect(card.get_by_test_id("update-check-now")).to_be_disabled()


@pytest.mark.usefixtures("clean_update")
class TestRefused:
    @pytest.mark.usefixtures("connected_gps")
    def test_while_a_console_link_is_connected(
        self, page: Page, base_url: str, api_base_url: str, update_dir: Path
    ) -> None:
        card = _open_settings(page, base_url)

        expect(card.get_by_test_id("update-refusal")).to_have_text(
            "A Console link is connected. Disconnect it to update.", timeout=10_000
        )
        expect(card.get_by_test_id("update-button")).to_be_disabled()
        refused = httpx.post(
            f"{api_base_url}/api/update",
            json={"app": E2E_UPDATE_APP, "relay": RELAY},
            timeout=5.0,
        )
        assert refused.status_code == 409
        assert refused.json()["code"] == "console_connected"
        assert not (update_dir / "request.json").exists()

    @pytest.mark.usefixtures("connected_gps")
    def test_while_a_survey_in_runs(
        self, page: Page, base_url: str, api_base_url: str
    ) -> None:
        started = httpx.post(
            f"{api_base_url}/api/device/configure/survey-in",
            json={"min_duration_seconds": 3600, "accuracy_limit_mm": 1000},
            timeout=10.0,
        )
        assert started.status_code == 200, started.text
        try:
            card = _open_settings(page, base_url)

            expect(card.get_by_test_id("update-refusal")).to_have_text(
                "A Survey-in is running. Update once it has finished.",
                timeout=10_000,
            )
            expect(card.get_by_test_id("update-button")).to_be_disabled()
        finally:
            httpx.post(f"{api_base_url}/api/device/cancel-survey-in", timeout=10.0)


@pytest.mark.usefixtures("clean_update")
class TestWhileUpdating:
    @pytest.mark.parametrize(
        "path", ["/", "/input", "/outputs", "/survey", "/settings"]
    )
    def test_the_banner_and_badge_show_on_every_page(
        self, page: Page, base_url: str, update_dir: Path, path: str
    ) -> None:
        _host_writes(update_dir, "installing")

        page.goto(f"{base_url}{path}")

        expect(page.get_by_test_id("update-banner")).to_have_text(
            f"Updating to {E2E_UPDATE_APP}…", timeout=10_000
        )
        expect(page.get_by_test_id("update-badge")).to_have_text("Updating…")

    def test_every_phase_shows_in_the_bar(
        self, page: Page, base_url: str, update_dir: Path
    ) -> None:
        _host_writes(update_dir, "requested")
        card = _open_settings(page, base_url)

        for phase, text in STEPS:
            _host_writes(update_dir, phase)
            expect(card.get_by_test_id("update-progress")).to_have_text(
                text, timeout=10_000
            )

    def test_the_banner_follows_the_restart(
        self, page: Page, base_url: str, update_dir: Path
    ) -> None:
        _host_writes(update_dir, "restarting")
        page.goto(f"{base_url}/")

        expect(page.get_by_test_id("update-banner")).to_have_text(
            f"Restarting into {E2E_UPDATE_APP}. This page reconnects by itself.",
            timeout=10_000,
        )
        _host_writes(update_dir, "verifying")
        expect(page.get_by_test_id("update-banner")).to_have_text(
            f"Checking {E2E_UPDATE_APP} started…", timeout=10_000
        )

    def test_start_survey_in_console_connect_and_check_now_are_refused(
        self, api_base_url: str, update_dir: Path
    ) -> None:
        _host_writes(update_dir, "installing")

        start = httpx.post(f"{api_base_url}/api/relay/start", timeout=5.0)
        connect = httpx.post(
            f"{api_base_url}/api/device/connect",
            json={"vendor": "fake", "port": "FAKE", "baud_rate": 115200},
            timeout=5.0,
        )
        survey = httpx.post(
            f"{api_base_url}/api/device/configure/survey-in",
            json={"min_duration_seconds": 60, "accuracy_limit_mm": 2000},
            timeout=5.0,
        )
        check = httpx.post(f"{api_base_url}/api/update/check", timeout=5.0)

        assert (start.status_code, start.json()["code"]) == (409, "updating")
        assert connect.status_code == 409
        assert "An Update is running" in connect.json()["detail"]
        assert survey.status_code == 409
        assert "An Update is running" in survey.json()["detail"]
        assert (check.status_code, check.json()["code"]) == (409, "updating")


@pytest.mark.usefixtures("clean_update")
class TestOutcome:
    def test_didnt_start_within_30_s(
        self, page: Page, base_url: str, update_dir: Path
    ) -> None:
        _host_writes(
            update_dir,
            "requested",
            at=datetime.now(timezone.utc) - timedelta(seconds=31),
        )
        (update_dir / "request.json").write_text(
            json.dumps({"format": 1, "app": E2E_UPDATE_APP, "relay": RELAY})
        )

        card = _open_settings(page, base_url)

        expect(card.get_by_test_id("update-outcome")).to_have_text(
            "Update didn't start: the host didn't pick up the request within 30 s. "
            "Nothing changed.",
            timeout=10_000,
        )
        expect(page.get_by_test_id("update-banner")).to_have_count(0)
        expect(card.get_by_test_id("update-button")).to_be_enabled()
        assert not (update_dir / "request.json").exists()

    def test_a_newer_release_appeared(
        self, page: Page, base_url: str, update_dir: Path
    ) -> None:
        _host_writes(
            update_dir,
            "failed",
            reason="newer_release",
            error="A newer release appeared; check again.",
        )

        card = _open_settings(page, base_url)

        expect(card.get_by_test_id("update-outcome")).to_have_text(
            "Update not started: a newer release appeared since you checked. "
            "Check again and read its notes. Nothing changed.",
            timeout=10_000,
        )

    def test_now_on_x_once_until_dismissed(
        self, page: Page, base_url: str, update_dir: Path
    ) -> None:
        _host_writes(update_dir, "done")

        page.goto(f"{base_url}/")
        expect(page.get_by_test_id("update-banner-text")).to_have_text(
            f"Now on {E2E_UPDATE_APP}.", timeout=10_000
        )
        page.reload()
        expect(page.get_by_test_id("update-banner-text")).to_have_text(
            f"Now on {E2E_UPDATE_APP}.", timeout=10_000
        )

        page.get_by_test_id("update-banner-dismiss").click()

        expect(page.get_by_test_id("update-banner")).to_have_count(0)
        page.goto(f"{base_url}/settings")
        expect(_card(page).get_by_test_id("update-outcome")).to_contain_text(
            f"Updated {running_app} → {E2E_UPDATE_APP} on ", timeout=10_000
        )
        expect(page.get_by_test_id("update-banner")).to_have_count(0)
