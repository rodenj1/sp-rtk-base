"""End-to-end tests for the Survey-In Cancel button + progress visibility.

These tests are the UX-regression contract for the feature added on
2026-05-27.  Before this change, the operator had no way to abort a
survey-in (apart from disconnecting), and the progress card stayed
hidden if ``configure_survey_in`` failed silently — making the page
look completely unresponsive after pressing Start.

What we verify here:

1. After Start → Start Survey is clicked, the progress card is visible
   *immediately* (no waiting on the configure round-trip).
2. The "Cancel Survey" button appears once a survey is running, and
   clicking it pops a confirmation dialog with "Keep Surveying" and
   "Cancel Survey".
3. Confirming the cancel hits ``POST /api/device/cancel-survey-in``,
   the device reports ``active=False`` again, the page shows
   "Cancelled by operator", and the Start button is restored.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_progress_card_visible_immediately_after_start(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Regression: the Survey-In progress card must appear the moment
    Start is confirmed — *not* only after the configure RPC returns.
    Previously a slow/failing receiver write left the page looking
    frozen with no indication that anything had happened.
    """
    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)

    page.get_by_role("button", name="Start Survey-In").click()
    expect(page.locator("text=Start Survey-In?").first).to_be_visible(timeout=5_000)
    page.get_by_role("button", name="Start Survey", exact=True).click()

    # Progress card heading must be visible within a tight window — the
    # UI shows it synchronously before the configure call returns.
    expect(page.locator("text=Survey-In Progress").first).to_be_visible(timeout=3_000)
    # The new metric labels must be present too.
    expect(page.locator("text=% to target").first).to_be_visible()
    expect(page.locator("text=ETA").first).to_be_visible()


@pytest.mark.e2e
def test_cancel_survey_in_flow(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """Full Start → Cancel → confirmation → REST flip → UI restored."""
    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)

    # ----- Start the survey -----
    page.get_by_role("button", name="Start Survey-In").click()
    expect(page.locator("text=Start Survey-In?").first).to_be_visible(timeout=5_000)
    page.get_by_role("button", name="Start Survey", exact=True).click()
    expect(page.locator("text=Survey-In Progress").first).to_be_visible(timeout=5_000)

    # The fake driver reports active=True as soon as configure_survey_in runs.
    status = page.request.get(f"{api_base_url}/api/device/survey-in")
    assert status.ok, status.text()
    payload: dict[str, Any] = status.json()
    assert payload.get("active") is True, (
        f"survey-in should be active after Start; got {payload!r}"
    )

    # ----- Click Cancel Survey on the page -----
    page.get_by_role("button", name="Cancel Survey").first.click()

    # Confirmation dialog heading.
    expect(page.locator("text=Cancel Survey-In?").first).to_be_visible(timeout=5_000)

    # "Keep Surveying" first — must not actually cancel.
    page.get_by_role("button", name="Keep Surveying", exact=True).click()
    expect(page.locator("text=Cancel Survey-In?").first).not_to_be_visible(
        timeout=5_000
    )
    status = page.request.get(f"{api_base_url}/api/device/survey-in")
    payload = status.json()
    assert payload.get("active") is True, (
        f"Keep Surveying should NOT cancel; got {payload!r}"
    )

    # Now actually cancel.
    page.get_by_role("button", name="Cancel Survey").first.click()
    expect(page.locator("text=Cancel Survey-In?").first).to_be_visible(timeout=5_000)
    # The dialog's confirm button is also labelled "Cancel Survey" — there
    # are now two on the page (page + dialog), but only the dialog one is
    # inside a .q-dialog wrapper.  We disambiguate by scope.
    page.locator(".q-dialog").get_by_role("button", name="Cancel Survey").click()

    # Dialog closed and REST reports the survey is no longer active.
    expect(page.locator("text=Cancel Survey-In?").first).not_to_be_visible(
        timeout=5_000
    )
    status = page.request.get(f"{api_base_url}/api/device/survey-in")
    payload = status.json()
    assert payload.get("active") is False, (
        f"survey-in should be inactive after Cancel; got {payload!r}"
    )

    # UI shows the cancelled state and the Start button is back.
    expect(page.locator("text=Cancelled by operator").first).to_be_visible(
        timeout=5_000
    )
    expect(page.get_by_role("button", name="Start Survey-In")).to_be_visible()
