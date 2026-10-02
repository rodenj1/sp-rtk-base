"""End-to-end tests: Correction sources on the Survey page (issue #192).

The Survey-In card's Plain / Corrected toggle, and the New / Edit Correction
source dialogs. Corrected Start arrives in a later slice; until then it is
unavailable.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

SECRET = "s3cret-pa55"


def _open_survey(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)


def _sources(page: Page, api_base_url: str) -> dict[str, Any]:
    response = page.request.get(f"{api_base_url}/api/correction-sources")
    assert response.ok, response.text()
    payload: dict[str, Any] = response.json()
    return payload


def _create_in_dialog(page: Page, name: str) -> None:
    page.get_by_role("button", name="New Correction source").click()
    expect(page.get_by_text("New Correction source", exact=True)).to_be_visible(
        timeout=5_000
    )
    page.get_by_label("Name", exact=True).fill(name)
    page.get_by_label("Caster", exact=True).fill("caster.example.com")
    page.get_by_label("Mountpoint", exact=True).fill("MP1")
    page.get_by_label("Username", exact=True).fill("me@example.com")
    page.get_by_label("Password", exact=True).fill(SECRET)
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(f"text=Saved '{name}'").first).to_be_visible(timeout=10_000)


@pytest.mark.e2e
def test_the_corrected_toggle_swaps_the_limits_and_shows_the_source_row(
    page: Page, base_url: str, connected_gps: None, clean_config: None
) -> None:
    _open_survey(page, base_url)
    duration = page.get_by_label("Min Duration (seconds)", exact=True)
    accuracy = page.get_by_label("Accuracy Limit (mm)", exact=True)
    expect(duration).to_have_value("120")
    expect(accuracy).to_have_value("50000")
    expect(page.get_by_label("Correction source", exact=True)).to_be_hidden()

    page.get_by_role("button", name="Corrected (cm-level)").click()

    expect(page.get_by_label("Correction source", exact=True)).to_be_visible()
    expect(page.locator("text=sends no RTCM").first).to_be_visible()
    fixed_time = page.get_by_label("Min RTK Fixed time (seconds)", exact=True)
    expect(fixed_time).to_have_value("300")
    expect(accuracy).to_have_value("50")
    expect(accuracy).to_have_attribute("min", "10")
    expect(accuracy).to_have_attribute("max", "1000")
    expect(page.get_by_role("button", name="Start Survey-In")).to_be_disabled()

    page.get_by_role("button", name="Plain").click()

    expect(page.get_by_label("Min Duration (seconds)", exact=True)).to_have_value("120")
    expect(accuracy).to_have_value("50000")
    expect(accuracy).to_have_attribute("min", "1000")
    expect(accuracy).to_have_attribute("max", "500000")
    expect(page.get_by_role("button", name="Start Survey-In")).to_be_enabled()


@pytest.mark.e2e
def test_a_new_source_is_saved_selected_and_its_password_never_shown(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    clean_config: None,
) -> None:
    _open_survey(page, base_url)
    page.get_by_role("button", name="Corrected (cm-level)").click()

    _create_in_dialog(page, "my-caster")

    expect(page.get_by_test_id("correction-source-select")).to_contain_text("my-caster")
    listed = _sources(page, api_base_url)
    assert [s["name"] for s in listed["sources"]] == ["my-caster"]
    assert listed["sources"][0]["has_password"] is True
    assert SECRET not in str(listed)
    assert listed["last_used"] == "my-caster"


@pytest.mark.e2e
def test_editing_keeps_a_blank_password_removes_it_on_request_and_deletes(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    clean_config: None,
) -> None:
    _open_survey(page, base_url)
    page.get_by_role("button", name="Corrected (cm-level)").click()
    _create_in_dialog(page, "my-caster")

    # Edit: the password is not shown back; leaving it blank keeps it
    page.get_by_role("button", name="Edit Correction source").click()
    password = page.get_by_label("Password", exact=True)
    expect(password).to_have_value("")
    page.locator("text=Advanced (Relay defaults)").first.click()
    expect(page.locator("text=Connection timeout: 15.0 s").first).to_be_visible()
    expect(page.locator("text=Data timeout: 30.0 s").first).to_be_visible()
    page.get_by_label("Port", exact=True).fill("2102")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator("text=Saved 'my-caster'").first).to_be_visible(timeout=10_000)
    source = _sources(page, api_base_url)["sources"][0]
    assert (source["port"], source["has_password"]) == (2102, True)

    # Remove the saved password explicitly
    page.get_by_role("button", name="Edit Correction source").click()
    page.get_by_role("button", name="Remove saved password").click()
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator("text=Saved 'my-caster'").first).to_be_visible(timeout=10_000)
    assert _sources(page, api_base_url)["sources"][0]["has_password"] is False

    # Delete it
    page.get_by_role("button", name="Edit Correction source").click()
    page.get_by_role("button", name="Delete", exact=True).click()
    expect(page.locator("text=Deleted 'my-caster'").first).to_be_visible(timeout=10_000)
    assert _sources(page, api_base_url)["sources"] == []


@pytest.mark.e2e
def test_the_last_used_source_is_preselected_on_return(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    clean_config: None,
) -> None:
    for name in ("first", "second"):
        page.request.post(
            f"{api_base_url}/api/correction-sources",
            data={"name": name, "caster": "c.example.com", "mountpoint": "MP1"},
        )
    _open_survey(page, base_url)
    page.get_by_role("button", name="Corrected (cm-level)").click()
    page.get_by_test_id("correction-source-select").click()
    page.get_by_role("option", name="second", exact=True).click()

    _open_survey(page, base_url)
    page.get_by_role("button", name="Corrected (cm-level)").click()

    expect(page.get_by_test_id("correction-source-select")).to_contain_text("second")
