"""End-to-end tests for the survey-in base-invariants pre-flight check (issue #63).

Covers the Start Survey-In confirmation dialog's non-blocking warning
banner: a receiver missing the base invariants (stationary dynamics,
RTCM on every data-link port) shows a warning with a one-click "Apply
Base Invariants Now" remedy, but Start Survey remains clickable
regardless — and a correctly configured receiver shows no warning.

The :class:`~sp_rtk_base.services.drivers.fake.FakeGpsDriver` defaults
to ``DynModel.PORTABLE`` and leaves UART2 with zero RTCM rows enabled
(only UART1 defaults to on), so a fresh ``connected_gps`` connection
is exactly the "under-configured" case this check exists for.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_warning_shown_and_start_still_works(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """A freshly connected (under-configured) receiver warns but still starts."""
    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)

    page.get_by_role("button", name="Start Survey-In").click()
    expect(page.locator("text=Start Survey-In?").first).to_be_visible(timeout=5_000)

    # FakeGpsDriver defaults to portable dynamics and no RTCM on UART2.
    expect(page.locator("text=Dynamics model is portable").first).to_be_visible(
        timeout=5_000
    )
    expect(
        page.locator("text=No RTCM messages are enabled on UART2").first
    ).to_be_visible()
    apply_now = page.get_by_role("button", name="Apply Base Invariants Now")
    expect(apply_now).to_be_visible()

    # Non-blocking: Start Survey is still clickable despite the warning.
    page.get_by_role("button", name="Start Survey", exact=True).click()
    expect(page.locator("text=Survey-in started").first).to_be_visible(timeout=10_000)


@pytest.mark.e2e
def test_apply_base_invariants_now_clears_the_warning(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """Clicking the one-click remedy applies the built-in profile and clears the banner."""
    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)

    page.get_by_role("button", name="Start Survey-In").click()
    expect(page.locator("text=Start Survey-In?").first).to_be_visible(timeout=5_000)
    expect(page.locator("text=Dynamics model is portable").first).to_be_visible(
        timeout=5_000
    )

    page.get_by_role("button", name="Apply Base Invariants Now").click()
    expect(page.locator("text=Base invariants applied").first).to_be_visible(
        timeout=10_000
    )

    # The banner re-checks and clears once the remedy lands.
    expect(page.locator("text=Dynamics model is portable")).not_to_be_visible(
        timeout=5_000
    )
    expect(page.locator("text=No RTCM messages are enabled")).not_to_be_visible()

    # REST mirror: the built-in profile's dyn_model is now live.
    status = page.request.get(f"{api_base_url}/api/device/base-invariants")
    assert status.ok, status.text()
    payload: dict[str, Any] = status.json()
    assert payload.get("warnings") == [], f"expected no warnings, got {payload!r}"


@pytest.mark.e2e
def test_no_warning_when_already_correctly_configured(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """A receiver already matching the built-in profile shows no warning."""
    remedy = page.request.post(f"{api_base_url}/api/device/apply-base-invariants")
    assert remedy.ok, remedy.text()

    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)

    page.get_by_role("button", name="Start Survey-In").click()
    expect(page.locator("text=Start Survey-In?").first).to_be_visible(timeout=5_000)

    expect(
        page.get_by_role("button", name="Apply Base Invariants Now")
    ).not_to_be_visible(timeout=5_000)
