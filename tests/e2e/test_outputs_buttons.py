"""End-to-end button-click tests for the Outputs (destinations) page.

These tests drive the **real** dialogs in the live NiceGUI UI through
Playwright, complementing :mod:`tests.e2e.test_destinations_crud`
which exercises the REST API only.  The point of this file is to
catch front-end regressions — broken on-click wiring, dialog mount
failures, validation-mismatch quirks — that the REST-level tests
cannot see.

Covered button paths:

1. **Add Destination** → dialog opens → fill name + port → click
   **Add** → success toast → card visible in list.
2. **Delete** (red trash icon) → confirmation dialog → click
   **Delete** → toast → card disappears.
3. **Empty-name validation** — click **Add** with name blank → the
   "Name is required" validation surfaces and the warning toast
   fires.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_add_destination_dialog_creates_and_lists(
    page: Page,
    base_url: str,
    api_base_url: str,
    clean_config: None,
) -> None:
    """Full add-destination dialog workflow exercised through the UI.

    Click **Add Destination** → fill the dialog → click **Add** →
    success toast → the new destination card appears in the list
    AND ``GET /api/destinations`` returns it.
    """
    name = "e2e-btn-tcp"
    page.goto(f"{base_url}/outputs")
    expect(page.locator("text=Destinations").first).to_be_visible(timeout=15_000)

    # Open the dialog.
    page.get_by_role("button", name="Add Destination").click()

    # Dialog heading.
    expect(page.locator("text=Add Destination").nth(1)).to_be_visible(timeout=5_000)

    # Fill the Name field (label "Name" is unique inside the dialog).
    page.get_by_label("Name", exact=True).fill(name)

    # Type defaults to tcp_server which exposes Bind Address / Port /
    # Max Clients fields.  Override Port to something unlikely to
    # collide with the developer's running services.
    page.get_by_label("Port", exact=True).fill("5099")

    # Submit.  The dialog's confirm button is labelled "Add" (the
    # outer page button is "Add Destination" so the role+name match
    # picks the dialog one unambiguously).
    page.get_by_role("button", name="Add", exact=True).click()

    # Success toast — the message is literally f"Added '{name}'".
    expect(page.locator(f"text=Added '{name}'").first).to_be_visible(timeout=10_000)

    # The list refresh is deferred via ui.timer(0, ...) — wait for it.
    expect(page.locator(f"text={name}").first).to_be_visible(timeout=10_000)

    # And the REST surface confirms it.
    resp = page.request.get(f"{api_base_url}/api/destinations")
    assert resp.ok
    names = [d["name"] for d in resp.json()["destinations"]]
    assert name in names, f"{name!r} missing from REST list {names!r}"


@pytest.mark.e2e
def test_delete_destination_confirmation_flow(
    page: Page,
    base_url: str,
    api_base_url: str,
    clean_config: None,
) -> None:
    """Delete button → confirmation dialog → confirm → list updates.

    Seeds the destination via REST so the test is focused on the
    delete flow only; the add flow is covered above.
    """
    name = "e2e-btn-delete"

    # Seed.
    create = page.request.post(
        f"{api_base_url}/api/destinations",
        data={
            "name": name,
            "type": "tcp_server",
            "enabled": True,
            "config": {
                "host": "0.0.0.0",
                "port": "5100",
                "max_clients": "3",
            },
            "filter": {},
        },
    )
    assert create.status == 201, create.text()

    page.goto(f"{base_url}/outputs")
    expect(page.locator(f"text={name}").first).to_be_visible(timeout=15_000)

    # The destination card has both Edit (blue) and Delete (red)
    # icon buttons.  Both use icon-only Quasar buttons rendered as
    # ``<button class="q-btn"><i class="q-icon">delete</i></button>``,
    # so a role-based selector pinned to the icon name is the
    # cleanest approach.  We restrict to the row that contains the
    # destination name to disambiguate when the page has multiple
    # cards.
    row = page.locator(f"div:has-text('{name}')").first
    row.locator("button:has(i.q-icon:text('delete'))").first.click()

    # Confirmation dialog.
    expect(page.locator(f"text=Delete destination '{name}'?").first).to_be_visible(
        timeout=5_000
    )

    # The dialog has two buttons: "Cancel" and "Delete".  Clicking
    # "Delete" must match exactly so we don't accidentally re-hit
    # the row's trash icon.
    page.get_by_role("button", name="Delete", exact=True).click()

    # Toast + card gone.
    expect(page.locator(f"text=Deleted '{name}'").first).to_be_visible(timeout=10_000)
    expect(page.locator(f"text={name}").first).not_to_be_visible(timeout=10_000)

    # REST confirms.
    resp = page.request.get(f"{api_base_url}/api/destinations")
    names = [d["name"] for d in resp.json()["destinations"]]
    assert name not in names, f"delete didn't persist: {name!r} still in {names!r}"


@pytest.mark.e2e
def test_add_destination_dialog_blocks_empty_name(
    page: Page,
    base_url: str,
    clean_config: None,
) -> None:
    """Submitting the Add dialog with no name shows the warning toast.

    The handler emits ``ui.notify("Name is required", type="warning")``
    when ``name_input.value`` is empty.  We assert on that toast
    appearing AND the dialog remaining open (no destination was
    created).
    """
    page.goto(f"{base_url}/outputs")
    expect(page.locator("text=Destinations").first).to_be_visible(timeout=15_000)

    page.get_by_role("button", name="Add Destination").click()
    expect(page.locator("text=Add Destination").nth(1)).to_be_visible(timeout=5_000)

    # Click Add without filling the name.
    page.get_by_role("button", name="Add", exact=True).click()

    # Warning toast — the message wording is literal.
    expect(page.locator("text=Name is required").first).to_be_visible(timeout=5_000)

    # Dialog stays open (the "Add Destination" heading inside the
    # dialog is still visible).
    expect(page.locator("text=Add Destination").nth(1)).to_be_visible()
