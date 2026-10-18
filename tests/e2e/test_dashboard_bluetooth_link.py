"""End-to-end: the Bluetooth link row of the dashboard's Input Source card.

The e2e server has no Relay input configured, so the Relay is stopped:
the card says "Not running" and has no Bluetooth link row.  A live
verdict needs a Bluetooth Input profile; the row's content is covered
at the Bluetooth link strength monitor seam.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_no_bluetooth_link_row_while_the_relay_is_stopped(
    page: Page, base_url: str
) -> None:
    page.goto(f"{base_url}/")

    expect(page.get_by_text("Input Source", exact=True)).to_be_visible()
    expect(page.get_by_text("Not running").first).to_be_visible()
    expect(page.get_by_test_id("bluetooth-link-row")).to_have_count(0)
    expect(page.get_by_text("Bluetooth link", exact=True)).to_have_count(0)
