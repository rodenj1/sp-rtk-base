"""End-to-end button-click tests for the Survey-In page.

Covers the button paths on ``/survey`` that aren't already exercised
by :mod:`tests.e2e.test_survey_save_position`:

1. **Start Survey-In** → confirmation dialog → **Cancel** → no
   side-effects (dialog closes, no toast).
2. **Start Survey-In** → confirmation dialog → **Start Survey** →
   ``Survey-in started`` toast → progress card visible → REST
   reports ``status == "in_progress"``.
3. **Edit** Fixed Base → edit mode visible → **Cancel** → readonly
   view restored, no commit.
4. **Edit** Fixed Base → tweak values → **Commit** → device
   "Position committed" toast → REST reflects the new coordinates.

The :func:`connected_gps` fixture wires the live server up to the
:class:`FakeGpsDriver` so all of these UI paths are reachable without
real hardware.  See ``services/drivers/fake.py`` for the in-process
state machine — Survey-In runs entirely synchronously inside that
driver so the assertions below don't have to wait on receiver
convergence.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_start_survey_confirmation_cancel(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """**Start Survey-In** → confirmation dialog → **Cancel**: no-op."""
    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)

    # The page button.
    page.get_by_role("button", name="Start Survey-In").click()

    # Confirmation dialog heading.
    expect(page.locator("text=Start Survey-In?").first).to_be_visible(timeout=5_000)

    # Cancel.
    page.get_by_role("button", name="Cancel", exact=True).first.click()

    # Dialog closed (heading gone) and the device REST surface still
    # reports IDLE — no survey-in was triggered.
    expect(page.locator("text=Start Survey-In?").first).not_to_be_visible(timeout=5_000)
    status = page.request.get(f"{api_base_url}/api/device/survey-in")
    assert status.ok, status.text()
    payload: dict[str, Any] = status.json()
    assert payload.get("active") is False, (
        f"survey-in should not be running after Cancel; got {payload!r}"
    )


@pytest.mark.e2e
def test_start_survey_confirmation_confirms_and_starts(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """**Start Survey-In** → **Start Survey**: toast + REST status flips."""
    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)

    page.get_by_role("button", name="Start Survey-In").click()
    expect(page.locator("text=Start Survey-In?").first).to_be_visible(timeout=5_000)

    # The confirmation button is labelled "Start Survey" (singular —
    # the page button is "Start Survey-In", so role+exact-name picks
    # the dialog one unambiguously).
    page.get_by_role("button", name="Start Survey", exact=True).click()

    # Success toast (literal message from the handler).
    expect(page.locator("text=Survey-in started").first).to_be_visible(timeout=10_000)

    # REST mirror: the FakeGpsDriver transitions IDLE → IN_PROGRESS
    # synchronously when ``configure_survey_in`` is called.
    status = page.request.get(f"{api_base_url}/api/device/survey-in")
    assert status.ok, status.text()
    payload: dict[str, Any] = status.json()
    assert payload.get("active") is True, (
        f"expected survey-in to be running after Start Survey, got {payload!r}"
    )


@pytest.mark.e2e
def test_fixed_base_edit_cancel_restores_readonly_view(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """Fixed-Base **Edit** → **Cancel**: edit mode hides, no commit."""
    # Seed a known fixed-base config so the Fixed-Base card has data
    # to render and the Edit/Cancel buttons are meaningful.
    seed = page.request.post(
        f"{api_base_url}/api/device/configure/fixed-base",
        data={
            "latitude": 32.7329015,
            "longitude": -117.2362788,
            "altitude_m": 27.940,
            "accuracy_mm": 47308,
        },
    )
    assert seed.ok, seed.text()

    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)

    # Wait for the read-back label to populate (the page polls the
    # base config every 2 s — the seeded latitude tells us we're
    # past the first poll).
    expect(page.locator("text=32.7329015°").first).to_be_visible(timeout=10_000)

    page.get_by_role("button", name="Edit", exact=True).click()

    # Edit mode reveals the numeric inputs — the "Latitude (°)" label
    # is unique to the edit form.
    expect(page.get_by_label("Latitude (°)")).to_be_visible(timeout=5_000)

    # Edit-mode also has a Cancel button (different from the dialog
    # Cancel above) — pick the visible one.
    cancel_btns = page.get_by_role("button", name="Cancel", exact=True)
    cancel_btns.first.click()

    # Edit form gone, read-back view restored (lat label still shown).
    expect(page.get_by_label("Latitude (°)")).not_to_be_visible(timeout=5_000)
    expect(page.locator("text=32.7329015°").first).to_be_visible()


@pytest.mark.e2e
def test_fixed_base_edit_commit_writes_new_coordinates(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """Fixed-Base **Edit** → tweak → **Commit**: toast + REST reflects."""
    # Start from a known baseline.
    seed = page.request.post(
        f"{api_base_url}/api/device/configure/fixed-base",
        data={
            "latitude": 32.7329015,
            "longitude": -117.2362788,
            "altitude_m": 27.940,
            "accuracy_mm": 47308,
        },
    )
    assert seed.ok, seed.text()

    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)
    expect(page.locator("text=32.7329015°").first).to_be_visible(timeout=10_000)

    page.get_by_role("button", name="Edit", exact=True).click()

    # Tweak the latitude — a small but distinguishable change.
    new_lat = 33.0000000
    lat_input = page.get_by_label("Latitude (°)")
    expect(lat_input).to_be_visible(timeout=5_000)
    lat_input.fill(str(new_lat))

    # Commit.
    page.get_by_role("button", name="Commit", exact=True).click()

    # Toast.
    expect(page.locator("text=Position committed to device").first).to_be_visible(
        timeout=10_000
    )

    # REST reflects the new value.  The fake driver's
    # ``configure_fixed_base`` stashes the config in-process so a
    # follow-up GET returns it verbatim.  ``CurrentBaseConfig`` is
    # serialised as a flat object — ``latitude`` is a top-level key.
    resp = page.request.get(f"{api_base_url}/api/device/base-config")
    assert resp.ok, resp.text()
    payload: dict[str, Any] = resp.json()
    assert payload.get("latitude") == pytest.approx(new_lat, abs=1e-6), (
        f"expected latitude {new_lat}, got {payload.get('latitude')!r}"
    )
