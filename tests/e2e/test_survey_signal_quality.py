"""End-to-end: the Signal Quality card on the Survey-In page.

With the fake receiver connected, the background poller feeds
Signal Quality and the card shows the clear-sky verdict and its three
measures.  Without a receiver it asks for one.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_survey_shows_good_signal_quality_with_the_fake_receiver(
    page: Page, base_url: str, connected_gps: None
) -> None:
    page.goto(f"{base_url}/survey")

    card = page.get_by_test_id("signal-quality-card")
    expect(card).to_contain_text("Signal Quality")
    expect(card).to_contain_text("Good", timeout=10_000)
    expect(card).to_contain_text("L1 Band strength")
    expect(card).to_contain_text("52.0 dB-Hz")
    expect(card).to_contain_text("51.0 dB-Hz")
    expect(card).to_contain_text("Usable satellites")
    expect(card).to_contain_text("28")


@pytest.mark.e2e
def test_survey_asks_for_a_receiver_when_none_is_connected(
    page: Page, base_url: str, api_base_url: str
) -> None:
    import httpx

    httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
    page.goto(f"{base_url}/survey")

    card = page.get_by_test_id("signal-quality-card")
    expect(card).to_contain_text("No data")
    expect(card).to_contain_text("Connect the receiver to see Signal Quality.")
