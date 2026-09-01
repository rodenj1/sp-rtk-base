"""E2E tests for the Fixed Position three-step card (issue #96).

The card is an accepted layout element of the T5 prototype
(``origin/prototype/gps-page-t5:tools/prototype-gps-page/index.html``,
operator-approved) that was missing from the shipped Advanced GPS
page entirely. It shows the Apply -> survey-in -> fixed-position
sequence as three ordered steps, states explicitly that fixed
position is *not* part of the profile, and links to the Survey page
for the survey-in/position-setting actions rather than duplicating
that page's own controls.

Covers the acceptance criteria from the issue:

- Hidden when disconnected, like every other card but Connection.
- Renders with three ordered steps once connected, indicating step 1
  (Apply profile) as current for a freshly-connected, never-surveyed
  receiver.
- Reflects the receiver's live state — step 2 current mid-survey-in,
  step 3 current once fixed-position mode is reached — driven through
  ``FakeGpsDriver`` via the real ``/api/device/configure/survey-in``
  and ``/api/device/configure/fixed-base`` endpoints (not fixture
  internals), then observed after a fresh page load, matching how the
  page itself refreshes this card (``_load_receiver_config_form``).
- States the "not part of the profile" copy.
- Survey-in/position-setting actions navigate to ``/survey`` rather
  than duplicating those controls here.

Locators target the stable ``fixed-position-*`` CSS class hooks the
page renders (see ``ui/pages/gps_config.py``) rather than text
substrings, matching the rest of this suite's convention
(``test_gps_profile_picker.py`` docstring explains why).
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect


def _goto_gps_config(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/gps-config")
    expect(page.locator("text=Advanced GPS Configuration").first).to_be_visible(
        timeout=15_000
    )


@pytest.mark.e2e
def test_card_hidden_when_disconnected(page: Page, base_url: str) -> None:
    """The card exists in the DOM but is not visible while disconnected."""
    page.goto(f"{base_url}/gps-config")
    expect(page.locator("text=Advanced GPS Configuration").first).to_be_visible(
        timeout=15_000
    )
    expect(page.locator(".fixed-position-card")).to_be_hidden(timeout=5_000)


@pytest.mark.e2e
def test_card_visible_with_step_1_current_after_connect(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """A freshly-connected, never-surveyed receiver shows step 1 current.

    ``FakeGpsDriver`` starts every connection in ``BaseMode.DISABLED``
    with no survey-in ever started, so this is the baseline state.
    """
    _goto_gps_config(page, base_url)

    card = page.locator(".fixed-position-card")
    expect(card).to_be_visible(timeout=10_000)

    # States it's not part of the profile.
    expect(card.locator(".fixed-position-not-in-profile")).to_be_visible()
    expect(card).to_contain_text("not part of the profile")

    # Three ordered steps, step 1 current, steps 2/3 pending.
    expect(card.locator(".fixed-position-step-1")).to_have_class(
        "fixed-position-step-1 q-pa-xs is-current"
    )
    expect(card.locator(".fixed-position-step-2")).to_have_class(
        "fixed-position-step-2 q-pa-xs is-pending"
    )
    expect(card.locator(".fixed-position-step-3")).to_have_class(
        "fixed-position-step-3 q-pa-xs is-pending"
    )


@pytest.mark.e2e
def test_survey_and_manual_links_navigate_to_survey_page(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """The card links to /survey rather than duplicating its controls."""
    _goto_gps_config(page, base_url)

    card = page.locator(".fixed-position-card")
    expect(card).to_be_visible(timeout=10_000)

    card.locator(".fixed-position-survey-link").click()
    expect(page).to_have_url(f"{base_url}/survey", timeout=10_000)

    _goto_gps_config(page, base_url)
    card = page.locator(".fixed-position-card")
    expect(card).to_be_visible(timeout=10_000)
    card.locator(".fixed-position-manual-link").click()
    expect(page).to_have_url(f"{base_url}/survey", timeout=10_000)


@pytest.mark.e2e
def test_step_2_current_while_survey_in_is_running(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """Driving the fake driver into survey-in reflects step 2 as current.

    ``FakeGpsDriver.get_survey_in_status`` fast-completes a survey
    after ~3s (``_SURVEY_FAST_COMPLETE_SECONDS``), so this posts the
    real API endpoint and reloads the page immediately — well inside
    that window — rather than waiting for any UI-side timer.
    """
    resp = httpx.post(
        f"{api_base_url}/api/device/configure/survey-in",
        json={"min_duration_seconds": 3600, "accuracy_limit_mm": 1000},
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text

    _goto_gps_config(page, base_url)

    card = page.locator(".fixed-position-card")
    expect(card).to_be_visible(timeout=10_000)
    expect(card.locator(".fixed-position-step-2")).to_have_class(
        "fixed-position-step-2 q-pa-xs is-current"
    )
    expect(card.locator(".fixed-position-step-1")).to_have_class(
        "fixed-position-step-1 q-pa-xs is-done"
    )
    expect(card.locator(".fixed-position-step-3")).to_have_class(
        "fixed-position-step-3 q-pa-xs is-pending"
    )


@pytest.mark.e2e
def test_step_3_current_once_fixed_position_is_set(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """Driving the fake driver into fixed mode reflects step 3 as current,
    with the surveyed coordinates shown."""
    resp = httpx.post(
        f"{api_base_url}/api/device/configure/fixed-base",
        json={
            "latitude": 32.7329015,
            "longitude": -117.2362788,
            "altitude_m": 27.940,
            "accuracy_mm": 47308,
        },
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text

    _goto_gps_config(page, base_url)

    card = page.locator(".fixed-position-card")
    expect(card).to_be_visible(timeout=10_000)
    expect(card.locator(".fixed-position-step-3")).to_have_class(
        "fixed-position-step-3 q-pa-xs is-current"
    )
    expect(card.locator(".fixed-position-step-1")).to_have_class(
        "fixed-position-step-1 q-pa-xs is-done"
    )
    expect(card.locator(".fixed-position-step-2")).to_have_class(
        "fixed-position-step-2 q-pa-xs is-done"
    )
    expect(card.locator(".fixed-position-step-3")).to_contain_text("32.7329015")
