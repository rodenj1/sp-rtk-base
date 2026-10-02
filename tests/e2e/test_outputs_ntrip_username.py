"""End-to-end tests: an NTRIP v2 output needs a username (issue #198).

NTRIP v2 casters authenticate a server with Basic auth, and the Relay
refuses a v2 NTRIP destination without a username. The Outputs page
refuses to save one, and flags one that was saved before this rule.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


def _open_add_ntrip_dialog(page: Page, base_url: str, name: str, version: str) -> None:
    page.goto(f"{base_url}/outputs")
    expect(page.locator("text=Destinations").first).to_be_visible(timeout=15_000)
    page.get_by_role("button", name="Add Destination").click()
    expect(page.locator("text=Add Destination").nth(1)).to_be_visible(timeout=5_000)
    page.get_by_label("Name", exact=True).fill(name)
    page.get_by_label("Type", exact=True).click()
    page.get_by_role("option", name="ntrip", exact=True).click()
    page.get_by_label("Caster Host", exact=True).fill("caster.example.com")
    page.get_by_label("Mountpoint", exact=True).fill("MP1")
    page.get_by_label("Password", exact=True).fill("secret")
    page.get_by_label("NTRIP Version", exact=True).click()
    page.get_by_role("option", name=version, exact=True).click()


def _saved_names(page: Page, api_base_url: str) -> list[str]:
    response = page.request.get(f"{api_base_url}/api/destinations")
    assert response.ok
    return [d["name"] for d in response.json()["destinations"]]


@pytest.mark.e2e
def test_a_v2_output_without_a_username_is_refused(
    page: Page, base_url: str, api_base_url: str, clean_config: None
) -> None:
    name = "e2e-ntrip-v2-no-user"
    _open_add_ntrip_dialog(page, base_url, name, version="2.0")

    page.get_by_role("button", name="Add", exact=True).click()

    expect(page.locator("text=needs a username").first).to_be_visible(timeout=10_000)
    assert name not in _saved_names(page, api_base_url)


@pytest.mark.e2e
def test_a_v1_output_needs_no_username(
    page: Page, base_url: str, api_base_url: str, clean_config: None
) -> None:
    name = "e2e-ntrip-v1"
    _open_add_ntrip_dialog(page, base_url, name, version="1.0")

    page.get_by_role("button", name="Add", exact=True).click()

    expect(page.locator(f"text=Added '{name}'").first).to_be_visible(timeout=10_000)
    assert name in _saved_names(page, api_base_url)


@pytest.mark.e2e
def test_a_saved_v2_output_without_a_username_is_flagged_until_fixed(
    page: Page, base_url: str, api_base_url: str, clean_config: None
) -> None:
    name = "e2e-ntrip-legacy"
    # Saved before the rule existed (straight through the REST API)
    created = page.request.post(
        f"{api_base_url}/api/destinations",
        data={
            "name": name,
            "type": "ntrip",
            "enabled": True,
            "config": {
                "caster": "caster.example.com",
                "port": 2101,
                "mountpoint": "MP1",
                "password": "secret",
                "version": "2.0",
            },
            "filter": {},
        },
    )
    assert created.ok, created.text()

    page.goto(f"{base_url}/outputs")
    expect(page.locator(f"text={name}").first).to_be_visible(timeout=15_000)
    expect(page.locator("text=needs a username").first).to_be_visible(timeout=5_000)

    # Editing it without a username is refused too: the dialog opens it as
    # v2 (no saved version means v2) and won't save it unchanged
    page.get_by_role("button", name=f"Edit {name}").click()
    page.get_by_role("button", name="Save", exact=True).click()
    expect(
        page.locator(".q-notification", has_text="needs a username").first
    ).to_be_visible(timeout=10_000)
    expect(page.locator(f"text=Updated '{name}'")).to_have_count(0)

    # Fix it: add a username
    page.get_by_label("Username", exact=True).fill("me@example.com")
    page.get_by_role("button", name="Save", exact=True).click()

    expect(page.locator(f"text=Updated '{name}'").first).to_be_visible(timeout=10_000)
    expect(page.locator("text=needs a username")).to_have_count(0, timeout=10_000)
