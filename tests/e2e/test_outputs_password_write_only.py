"""End-to-end tests: destination passwords are write-only on the Outputs page (#181)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

SECRET = "s3cret-pa55"


@pytest.mark.e2e
def test_the_edit_dialog_never_shows_the_saved_password_and_a_blank_save_keeps_it(
    page: Page, base_url: str, api_base_url: str, clean_config: None
) -> None:
    created = page.request.post(
        f"{api_base_url}/api/destinations",
        data={
            "name": "caster",
            "type": "ntrip",
            "enabled": False,
            "config": {
                "caster": "caster.example.com",
                "port": 2101,
                "mountpoint": "MP1",
                "username": "me",
                "password": SECRET,
                "version": "2.0",
            },
        },
    )
    assert created.ok, created.text()

    page.goto(f"{base_url}/outputs")
    expect(page.locator("text=caster").first).to_be_visible(timeout=15_000)
    assert SECRET not in page.content()  # not on the card either

    page.get_by_role("button", name="Edit caster").click()
    password = page.get_by_label("Password", exact=True)
    expect(password).to_have_value("")
    expect(password).to_have_attribute("placeholder", "•••••• (saved)")
    assert SECRET not in page.content()

    page.get_by_label("Mountpoint", exact=True).fill("MP2")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator("text=Updated 'caster'").first).to_be_visible(timeout=10_000)

    saved = page.request.get(f"{api_base_url}/api/destinations/caster").json()
    assert saved["config"]["mountpoint"] == "MP2"
    assert saved["has_password"] is True


@pytest.mark.e2e
def test_remove_saved_password_clears_it(
    page: Page, base_url: str, api_base_url: str, clean_config: None
) -> None:
    created = page.request.post(
        f"{api_base_url}/api/destinations",
        data={
            "name": "sp",
            "type": "surepath",
            "enabled": False,
            "config": {
                "host": "sp.example.com",
                "port": 50010,
                "username": "me",
                "password": SECRET,
            },
        },
    )
    assert created.ok, created.text()

    page.goto(f"{base_url}/outputs")
    page.get_by_role("button", name="Edit sp").click()
    page.get_by_role("button", name="Remove saved password").click()
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator("text=Updated 'sp'").first).to_be_visible(timeout=10_000)

    saved = page.request.get(f"{api_base_url}/api/destinations/sp").json()
    assert saved["has_password"] is False
    # It can't run without one, and its card says so.
    expect(page.locator("text=has no password").first).to_be_visible(timeout=10_000)
