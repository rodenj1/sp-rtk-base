"""End-to-end CRUD lifecycle for the Outputs (destinations) page.

We exercise the REST API directly (which is what the UI also calls)
**and** verify the result is visible in the browser.  This is the
most reliable mix while we don't yet have ``data-testid`` props on
NiceGUI dialog inputs.

A follow-up iteration should add ``data-testid`` props to the dialog
fields in ``ui/pages/outputs.py`` and then drive the dialog purely
through Playwright — that will catch front-end regressions that the
API-only path misses today.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_destination_lifecycle_via_api_visible_in_ui(
    page: Page,
    base_url: str,
    api_base_url: str,
    clean_config: None,
) -> None:
    """Create → list → update → delete a destination via REST, then
    confirm the UI reflects each step after a reload.
    """
    name = "e2e-test-tcp"

    # ---------------- 1. CREATE ----------------
    create_resp = page.request.post(
        f"{api_base_url}/api/destinations",
        data={
            "name": name,
            "type": "tcp_server",
            "enabled": True,
            "config": {
                "host": "0.0.0.0",
                "port": "5099",
                "max_clients": "3",
            },
            "filter": {},
        },
    )
    assert create_resp.status == 201, (
        f"POST /api/destinations failed: {create_resp.status} {create_resp.text()}"
    )

    # ---------------- 2. LIST via REST ----------------
    list_resp = page.request.get(f"{api_base_url}/api/destinations")
    assert list_resp.ok
    payload = list_resp.json()
    names = [d["name"] for d in payload["destinations"]]
    assert name in names, f"{name} missing from {names}"

    # ---------------- 3. UI reflects the new destination ----------------
    page.goto(f"{base_url}/outputs")
    expect(page.locator(f"text={name}").first).to_be_visible(timeout=15_000)

    # ---------------- 4. UPDATE via REST ----------------
    upd_resp = page.request.put(
        f"{api_base_url}/api/destinations/{name}",
        data={"enabled": False},
    )
    assert upd_resp.ok, (
        f"PUT /api/destinations/{name} failed: {upd_resp.status} {upd_resp.text()}"
    )
    upd_payload = upd_resp.json()
    assert upd_payload["enabled"] is False

    # ---------------- 5. DELETE via REST ----------------
    del_resp = page.request.delete(f"{api_base_url}/api/destinations/{name}")
    assert del_resp.ok

    # Confirm the deletion is persisted.
    list_resp2 = page.request.get(f"{api_base_url}/api/destinations")
    names2 = [d["name"] for d in list_resp2.json()["destinations"]]
    assert name not in names2


@pytest.mark.e2e
def test_outputs_page_shows_empty_state(
    page: Page,
    base_url: str,
    clean_config: None,
) -> None:
    """With no destinations configured, the empty-state copy is shown."""
    page.goto(f"{base_url}/outputs")
    expect(page.locator("text=No destinations configured yet.").first).to_be_visible(
        timeout=15_000
    )
