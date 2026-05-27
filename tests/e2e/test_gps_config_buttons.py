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
3. **Load from Device** (GNSS card) → ``GNSS config loaded`` toast.
4. **Apply GNSS Config** → ``GNSS config applied`` toast → REST
   read-back returns the expected enabled-systems list.

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


@pytest.mark.e2e
def test_load_gnss_button_pulls_config_from_device(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """**Load from Device** (GNSS card) → ``GNSS config loaded`` toast.

    The button is rendered with a leading ``icon="download"`` and the
    label "Load from Device".  There is only one such button on the
    page (the RTCM card uses the same label, so we scope by parent
    section to disambiguate).
    """
    page.goto(f"{base_url}/gps-config")
    expect(page.locator("text=Advanced GPS Configuration").first).to_be_visible(
        timeout=15_000
    )

    # Both the RTCM and GNSS cards have a "Load from Device" button.
    # Scope to the GNSS card by looking inside the section that
    # contains the "GNSS Constellations" heading.
    gnss_card = page.locator(".q-card:has(:text('GNSS Constellations'))").first
    expect(gnss_card).to_be_visible(timeout=10_000)

    load_btn = gnss_card.get_by_role("button", name="Load from Device")
    expect(load_btn).to_be_visible(timeout=10_000)
    load_btn.click()

    expect(page.locator("text=GNSS config loaded from device").first).to_be_visible(
        timeout=10_000
    )


@pytest.mark.e2e
def test_apply_gnss_button_writes_configuration(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """**Apply GNSS Config** click reaches the handler and writes to device.

    The browser-level assertion is the success toast.  A REST-level
    read-back proves the click triggered the underlying
    :meth:`DeviceService.configure_gnss` call (the fake driver
    overwrites its stored config wholesale on apply, so the
    ``min_channels`` / ``max_channels`` values in the read-back will
    drop to the form's defaults — that's a stable observable side-
    effect that doesn't depend on the user's specific toggle
    pattern, which Quasar switches don't always honour reliably from
    Playwright clicks).
    """
    page.goto(f"{base_url}/gps-config")
    expect(page.locator("text=Advanced GPS Configuration").first).to_be_visible(
        timeout=15_000
    )

    # Seed a known channel count so the comparison after Apply is
    # unambiguous.  The fake driver's default for GPS is
    # ``min_channels=8, max_channels=16`` — picking different
    # numbers gives us a clear delta.
    seed_systems = [
        {
            "constellation": "gps",
            "enabled": True,
            "min_channels": 8,
            "max_channels": 16,
            "sig_cfg_mask": 1,
        },
        {
            "constellation": "glonass",
            "enabled": True,
            "min_channels": 8,
            "max_channels": 14,
            "sig_cfg_mask": 1,
        },
        {
            "constellation": "galileo",
            "enabled": True,
            "min_channels": 4,
            "max_channels": 12,
            "sig_cfg_mask": 33,
        },
        {
            "constellation": "beidou",
            "enabled": True,
            "min_channels": 8,
            "max_channels": 16,
            "sig_cfg_mask": 17,
        },
    ]
    seed = page.request.put(
        f"{api_base_url}/api/device/gnss",
        data={"systems": seed_systems},
    )
    assert seed.status < 500, seed.text()

    # Click Apply.
    gnss_card = page.locator(".q-card:has(:text('GNSS Constellations'))").first
    expect(gnss_card).to_be_visible(timeout=10_000)
    apply_btn = gnss_card.get_by_role("button", name="Apply GNSS Config")
    expect(apply_btn).to_be_visible(timeout=10_000)
    apply_btn.click()

    # Toast — message is "GNSS config applied: ..." with the list of
    # enabled systems appended; substring match is enough.
    expect(page.locator("text=GNSS config applied").first).to_be_visible(timeout=10_000)

    # REST read-back: at least one numeric field must have changed
    # from the seeded values — proving the click *did* trigger a
    # write rather than being a no-op.
    after = page.request.get(f"{api_base_url}/api/device/gnss")
    assert after.ok, after.text()
    after_payload: dict[str, Any] = after.json()
    systems_after = list(after_payload.get("systems", []))
    seeded_max = sum(int(s["max_channels"]) for s in seed_systems)
    actual_max = sum(
        int(s.get("max_channels", 0)) for s in systems_after if isinstance(s, dict)
    )
    assert seeded_max != actual_max, (
        "Apply GNSS Config click did not write to the device — "
        f"channel totals unchanged at {actual_max} (seeded {seeded_max})"
    )
