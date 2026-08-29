"""End-to-end button-click tests for the Advanced GPS Config page.

This file exercises the click-handlers on ``/gps-config`` that
:mod:`tests.e2e.test_gps_data_flow` skipped (the latter is REST-only
except for a visibility check).  We drive the **real** UI through
Playwright with the in-memory :class:`FakeGpsDriver` so every
button-handler runs end-to-end.

Buttons covered here:

1. **Disconnect** → Quasar ``Disconnected`` toast → REST device
   status flips to disconnected.
2. **Save to Flash** → Quasar ``Saved to flash!`` toast → REST
   confirms (the fake driver's ``save_to_flash`` is a no-op that
   succeeds, so we only assert the toast appears).

The shave-by-shave RTCM/GNSS editing controls (checkboxes, switches,
their own Load/Apply buttons) this page used to have were replaced by
the read-only profile picker + live-seeded form (issue #64) — see
``test_gps_profile_picker.py`` for that coverage.

The **Connect** button is intentionally *not* tested through the UI
here because the ``connected_gps`` fixture already calls
``POST /api/device/connect`` directly, which is what the button does
internally.  Driving the dropdown widget (Quasar's
``with_input=True`` ``ui.select``) through Playwright is brittle and
adds no coverage over the REST path the fixture exercises.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_disconnect_button_emits_toast_and_changes_status(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """Click **Disconnect** on Advanced GPS → toast + REST shows disconnected."""
    page.goto(f"{base_url}/gps-config")
    expect(page.locator("text=Advanced GPS Configuration").first).to_be_visible(
        timeout=15_000
    )

    # The Disconnect button only renders meaningfully once the page
    # observes the connected state via its ``_update_ui_state`` poll.
    # The fixture has already POSTed connect, so it should be visible
    # almost immediately.
    disconnect_btn = page.get_by_role("button", name="Disconnect")
    expect(disconnect_btn).to_be_visible(timeout=10_000)
    disconnect_btn.click()

    # Toast — "Disconnected" (type=info).
    expect(page.locator("text=Disconnected").first).to_be_visible(timeout=10_000)

    # REST mirror: ``DeviceStatus.state`` flips to "disconnected".
    status = page.request.get(f"{api_base_url}/api/device/status")
    assert status.ok, status.text()
    payload: dict[str, Any] = status.json()
    assert payload.get("state") == "disconnected", (
        f"expected device.state=='disconnected' after click; got {payload!r}"
    )


@pytest.mark.e2e
def test_save_to_flash_button_emits_success_toast(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Click **Save to Flash** → ``Saved to flash!`` Quasar toast.

    The fake driver's ``save_to_flash`` is a no-op that returns
    success, so we only verify the click reaches the handler and the
    handler emits its positive notification.  REST-level persistence
    is already covered by ``test_gps_data_flow.py``.
    """
    page.goto(f"{base_url}/gps-config")
    expect(page.locator("text=Advanced GPS Configuration").first).to_be_visible(
        timeout=15_000
    )

    save_btn = page.get_by_role("button", name="Save to Flash")
    expect(save_btn).to_be_visible(timeout=10_000)
    save_btn.click()

    expect(page.locator("text=Saved to flash!").first).to_be_visible(timeout=10_000)
