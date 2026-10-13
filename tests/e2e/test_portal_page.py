"""The local Portal page (tickets #83 and #86, spec #82).

The e2e server talks to a fake Portal agent on a socket in a temp dir
(``portal_agent`` in conftest), which starts unclaimed: "Not connected",
offering "Connect to Portal". Tests script what it answers next.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests.fixtures.fake_agent_socket import FakeAgentSocket


def _pairing(base_url: str) -> dict[str, object]:
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    return {
        "state": "pairing",
        "user_code": "BCDFGHJK",
        "approve_url": f"{base_url}/claim-on-portal?code=BCDFGHJK",
        "expires_at": expires.isoformat(),
    }


CONNECTED = {
    "state": "connected",
    "base_id": "k3mnp4qrst",
    "account": "op@example.com",
    "portal": "sp-rtk-base.big-spray.com",
    "last_contact": "2026-01-01T12:03:00+00:00",
}


@pytest.mark.e2e
def test_an_unclaimed_base_is_not_connected(
    page: Page, base_url: str, portal_agent: FakeAgentSocket
) -> None:
    page.goto(f"{base_url}/portal")

    expect(page.get_by_test_id("portal-headline")).to_have_text(
        "Not connected", timeout=15_000
    )
    expect(page.get_by_role("button", name="Connect to Portal")).to_be_visible()


@pytest.mark.e2e
def test_the_drawer_links_to_the_portal_page(
    page: Page, base_url: str, portal_agent: FakeAgentSocket
) -> None:
    page.goto(f"{base_url}/")
    expect(page.locator("text=SP-Base").first).to_be_visible(timeout=15_000)

    # The drawer starts closed (behavior=mobile); the header's menu opens it.
    page.locator(".q-header button").first.click()
    page.locator(".q-drawer a[href='/portal']").click()

    expect(page).to_have_url(f"{base_url}/portal")
    expect(page.get_by_test_id("portal-headline")).to_have_text(
        "Not connected", timeout=15_000
    )


@pytest.mark.e2e
def test_connect_shows_the_code_a_countdown_and_approve_on_portal(
    page: Page, base_url: str, portal_agent: FakeAgentSocket
) -> None:
    portal_agent.after_claim = _pairing(base_url)
    page.goto(f"{base_url}/portal")

    page.get_by_role("button", name="Connect to Portal").click()

    expect(page.get_by_test_id("portal-headline")).to_have_text(
        "Pairing", timeout=15_000
    )
    expect(page.get_by_test_id("portal-code")).to_have_text("BCDF-GHJK")
    countdown = page.get_by_test_id("portal-countdown")
    expect(countdown).to_have_text(re.compile(r"^(10:00|9:5\d)$"))
    first = countdown.inner_text()
    expect(countdown).not_to_have_text(first, timeout=5_000)  # it ticks
    assert "start_claim" in portal_agent.ops
    with page.expect_popup() as popup:
        page.get_by_test_id("portal-approve").click()
    assert popup.value.url == f"{base_url}/claim-on-portal?code=BCDFGHJK"


@pytest.mark.e2e
def test_once_approved_the_page_shows_connected_with_the_claim(
    page: Page, base_url: str, portal_agent: FakeAgentSocket
) -> None:
    portal_agent.after_claim = _pairing(base_url)
    page.goto(f"{base_url}/portal")
    page.get_by_role("button", name="Connect to Portal").click()
    expect(page.get_by_test_id("portal-code")).to_be_visible(timeout=15_000)

    portal_agent.status = CONNECTED

    expect(page.get_by_test_id("portal-headline")).to_have_text(
        "Connected", timeout=10_000
    )
    expect(page.get_by_test_id("portal-account")).to_have_text("op@example.com")
    expect(page.get_by_test_id("portal-portal")).to_have_text(
        "sp-rtk-base.big-spray.com"
    )
    expect(page.get_by_test_id("portal-base-id")).to_have_text("k3mnp4qrst")
    expect(page.get_by_test_id("portal-last-contact")).to_have_text(
        "2026-01-01 12:03 UTC"
    )
    expect(page.get_by_role("button", name="Connect to Portal")).to_have_count(0)


@pytest.mark.e2e
def test_a_connect_that_fails_says_why_and_stays_not_connected(
    page: Page, base_url: str, portal_agent: FakeAgentSocket
) -> None:
    portal_agent.claim_error = "Couldn't reach the Portal (timed out)."
    page.goto(f"{base_url}/portal")

    page.get_by_role("button", name="Connect to Portal").click()

    expect(page.get_by_text("Couldn't reach the Portal (timed out).")).to_be_visible(
        timeout=10_000
    )
    expect(page.get_by_test_id("portal-headline")).to_have_text("Not connected")


@pytest.mark.e2e
def test_an_expired_pairing_says_so_and_offers_connect_again(
    page: Page, base_url: str, portal_agent: FakeAgentSocket
) -> None:
    portal_agent.status = {
        "state": "not_connected",
        "message": "The code expired before it was approved. Connect again for a new one.",
    }

    page.goto(f"{base_url}/portal")

    expect(page.get_by_test_id("portal-detail")).to_contain_text(
        "The code expired before it was approved.", timeout=15_000
    )
    expect(page.get_by_role("button", name="Connect to Portal")).to_be_enabled()


@pytest.mark.e2e
def test_a_polling_base_shows_its_wake_mode_and_the_portals_notices(
    page: Page, base_url: str, portal_agent: FakeAgentSocket
) -> None:
    portal_agent.status = CONNECTED | {
        "wake_mode": "polling",
        "poll_interval": 60,
        "metered": True,
        "notices": ["Protocol 1 ends on 2027-06-01."],
    }

    page.goto(f"{base_url}/portal")

    expect(page.get_by_test_id("portal-wake-mode")).to_have_text(
        "Polling every 60 s (metered)", timeout=15_000
    )
    expect(page.get_by_test_id("portal-last-contact")).to_have_text(
        "2026-01-01 12:03 UTC"
    )
    expect(page.get_by_test_id("portal-notice")).to_have_text(
        "Protocol 1 ends on 2027-06-01."
    )


@pytest.mark.e2e
def test_a_base_too_old_for_the_portal_says_to_upgrade(
    page: Page, base_url: str, portal_agent: FakeAgentSocket
) -> None:
    portal_agent.status = CONNECTED | {"needs_upgrade": True}

    page.goto(f"{base_url}/portal")

    expect(page.get_by_test_id("portal-headline")).to_have_text(
        "Upgrade sp-rtk-base to keep using the Portal", timeout=15_000
    )
    expect(page.get_by_test_id("portal-base-id")).to_have_text("k3mnp4qrst")


@pytest.mark.e2e
def test_un_claim_on_the_local_page_returns_it_to_not_connected(
    page: Page, base_url: str, portal_agent: FakeAgentSocket
) -> None:
    portal_agent.status = CONNECTED
    page.goto(f"{base_url}/portal")
    expect(page.get_by_test_id("portal-headline")).to_have_text(
        "Connected", timeout=15_000
    )

    page.get_by_test_id("portal-unclaim").click()
    # A confirmation first; Cancel keeps the Claim.
    expect(page.get_by_text("Un-claim this base?")).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    assert "unclaim" not in portal_agent.ops

    page.get_by_test_id("portal-unclaim").click()
    page.get_by_test_id("portal-unclaim-confirm").click()

    expect(page.get_by_test_id("portal-headline")).to_have_text(
        "Not connected", timeout=10_000
    )
    expect(page.get_by_test_id("portal-detail")).to_contain_text("Un-claimed.")
    expect(page.get_by_role("button", name="Connect to Portal")).to_be_enabled()
    assert portal_agent.ops.count("unclaim") == 1
