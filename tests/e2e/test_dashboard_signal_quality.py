"""End-to-end: the Signal Quality card on the dashboard.

The e2e server has no Relay input configured, so the Relay is stopped
and the card explains that Signal Quality shows while it runs.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_dashboard_card_says_the_relay_is_stopped(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/")

    card = page.get_by_test_id("signal-quality-card")
    expect(card).to_contain_text("Signal Quality")
    expect(card).to_contain_text("No data")
    expect(card).to_contain_text(
        "Relay is stopped. Signal Quality shows while it runs."
    )
