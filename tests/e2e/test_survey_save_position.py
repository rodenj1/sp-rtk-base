"""Regression test for the Survey-In **Save Position Profile** dialog.

Background — the bug this test locks in
=======================================

On 2026-05-26 the **Save** button inside the *Save Position Profile*
dialog (Survey-In page → Fixed Base card → "Save Position") did
nothing.  No notification, no log entry, no persistence, no dialog
close.  The **Cancel** button worked fine.

Root cause: in NiceGUI 3.x, when ``_save_position_dialog`` was
``async def`` and ``_do_save`` was a **nested ``async def`` defined
inside a ``with ui.row():`` slot context manager**, the entire
handler silently disappeared into the event loop.  No exception
surfaced anywhere.

The unit-level regression test (``tests/unit/test_base_positions.py``)
guards the *data path* — that :meth:`ConfigService.save_base_position`
round-trips correctly through YAML.  But it cannot detect the UI bug
because it never invokes the actual NiceGUI button handler.

This e2e test plugs that gap by driving the **real button** in a
real browser, against a real running server, against the
:class:`FakeGpsDriver` (so the Fixed Base card becomes visible and
the button handler reads non-zero coordinates from
``DeviceService.get_position``).  If anyone re-introduces an
``async def`` closure inside a slot context manager — or any other
change that breaks the button-handler wiring — this test will fail.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_save_position_button_persists_profile(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """Click **Save Position** → name the profile → click **Save**.

    Assertions, in order:
    1. The dialog opens (we can see the "Save Position Profile"
       heading and the **Save** button).
    2. The success toast ``Position '<name>' saved ✓`` appears
       (proves the handler ran).
    3. The dialog auto-closes (proves ``dlg.close()`` was reached).
    4. ``GET /api/device/base-positions`` returns the new profile
       (proves the persistence side-effect actually fired and the
       config service committed the YAML).
    5. The persisted lat/lon/alt match the :class:`FakeGpsDriver`
       fixture values (proves the dialog read live device state
       rather than the all-zero default).

    Cleanup: deletes the profile via REST so reruns are idempotent.
    """
    profile_name = "e2e-regression-save-btn"

    # ------------------------------------------------------------------
    # Setup: make sure the profile we're about to create doesn't
    # already exist from a previous run that aborted before cleanup.
    # ------------------------------------------------------------------
    page.request.delete(f"{api_base_url}/api/device/base-positions/{profile_name}")

    # ------------------------------------------------------------------
    # The Save-Position dialog persists the **currently configured**
    # base-station position (``DeviceService.get_base_config()``), not
    # the live receiver position.  On a freshly-connected fake driver
    # the base config is DISABLED with all-zero coordinates — which
    # would make the saved profile lat/lon/alt also be zero and rob
    # the test of its most meaningful assertion.
    #
    # So we put the fake driver into FIXED-BASE mode with the bug
    # fixture coordinates first, via REST.  That makes
    # ``get_base_config()`` return those numbers and the dialog reads
    # them on open.
    # ------------------------------------------------------------------
    seed_resp = page.request.post(
        f"{api_base_url}/api/device/configure/fixed-base",
        data={
            "latitude": 32.7329015,
            "longitude": -117.2362788,
            "altitude_m": 27.940,
            "accuracy_mm": 47308,
        },
    )
    assert seed_resp.ok, (
        f"failed to seed fixed-base config on fake driver: "
        f"HTTP {seed_resp.status} — {seed_resp.text()}"
    )

    try:
        # --------------------------------------------------------------
        # 1. Navigate to the Survey-In page and wait for the Fixed Base
        #    card to render.  The card is visibility-gated on a
        #    connected device — the ``connected_gps`` fixture takes
        #    care of that.
        # --------------------------------------------------------------
        page.goto(f"{base_url}/survey")
        expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)

        # The "Save Position" button is what triggers the dialog.  We
        # match by exact accessible name to disambiguate from the
        # dialog's inner "Save" button (which appears later).
        save_position_btn = page.get_by_role("button", name="Save Position")
        expect(save_position_btn).to_be_visible(timeout=15_000)

        # Wait until the page's polling timer (`_poll_fixed_base`) has
        # picked up the seeded coordinates.  The page reads them
        # asynchronously every 2 s and stashes them in
        # ``_fb_lat / _fb_lon / _fb_alt / _fb_acc``; the Save dialog
        # captures those values on open.  Polling the read-back label
        # for a non-dash value is a robust signal that the closure
        # has populated.
        expect(page.locator("text=32.7329015°").first).to_be_visible(timeout=10_000)

        save_position_btn.click()

        # --------------------------------------------------------------
        # 2. Dialog opens — assert by header text, then fill the name.
        # --------------------------------------------------------------
        expect(page.locator("text=Save Position Profile").first).to_be_visible(
            timeout=5_000
        )

        # The dialog's "Profile Name" input has a unique placeholder.
        # The example value uses an underscore (not a space) since v0.3.17,
        # which tightened the name regex to ``^[A-Za-z0-9_-]+$``.
        name_input = page.get_by_placeholder("e.g. Office_Roof")
        expect(name_input).to_be_visible(timeout=5_000)
        name_input.fill(profile_name)

        # --------------------------------------------------------------
        # 3. The bug under test — click the dialog's **Save** button.
        #    In the broken pre-2026-05-26 code, this click would have
        #    been a complete no-op: no notify, no API call, no
        #    dialog close.  The test would (correctly) fail at any of
        #    the next three assertions.
        # --------------------------------------------------------------
        dialog_save_btn = page.get_by_role("button", name="Save", exact=True)
        expect(dialog_save_btn).to_be_visible(timeout=5_000)
        dialog_save_btn.click()

        # --------------------------------------------------------------
        # 4. The success Quasar notification appears (handler ran).
        # --------------------------------------------------------------
        expect(
            page.locator(f"text=Position '{profile_name}' saved").first
        ).to_be_visible(timeout=10_000)

        # --------------------------------------------------------------
        # 5. The dialog closes (dlg.close() was reached).
        # --------------------------------------------------------------
        expect(page.locator("text=Save Position Profile").first).not_to_be_visible(
            timeout=5_000
        )

        # --------------------------------------------------------------
        # 6. The profile actually landed in the config — REST sees it.
        # --------------------------------------------------------------
        resp = page.request.get(f"{api_base_url}/api/device/base-positions")
        assert resp.ok, (
            f"GET /api/device/base-positions returned {resp.status}: {resp.text()}"
        )
        positions: object = resp.json()
        assert isinstance(positions, list), (
            f"expected list, got {type(positions).__name__}: {positions!r}"
        )
        saved: dict[str, Any] | None = None
        for entry in positions:
            if isinstance(entry, dict):
                entry_d = cast(dict[str, Any], entry)
                if entry_d.get("name") == profile_name:
                    saved = entry_d
                    break
        assert saved is not None, (
            f"saved profile {profile_name!r} not in returned list — "
            "Save button handler did not persist to ConfigService"
        )

        # --------------------------------------------------------------
        # 7. The persisted coordinates match the FakeGpsDriver fixture.
        #    This proves the dialog read live device state through
        #    ``DeviceService.get_position()`` rather than falling back
        #    to the all-zero defaults that an unconnected receiver
        #    would have produced.
        # --------------------------------------------------------------
        # FakeGpsDriver fixture values (see
        # services/drivers/fake.py:_FAKE_LAT/_FAKE_LON/_FAKE_ALT_M).
        assert saved.get("latitude") == pytest.approx(32.7329015, abs=1e-6), (
            f"expected fake-driver latitude, got {saved.get('latitude')!r}"
        )
        assert saved.get("longitude") == pytest.approx(-117.2362788, abs=1e-6), (
            f"expected fake-driver longitude, got {saved.get('longitude')!r}"
        )
        assert saved.get("altitude_m") == pytest.approx(27.940, abs=1e-3), (
            f"expected fake-driver altitude, got {saved.get('altitude_m')!r}"
        )

    finally:
        # ------------------------------------------------------------------
        # Cleanup: always remove the profile so consecutive test runs are
        # deterministic.  Don't assert here — if the test failed before
        # the profile was created the delete returns 404, which is fine.
        # ------------------------------------------------------------------
        page.request.delete(f"{api_base_url}/api/device/base-positions/{profile_name}")


@pytest.mark.e2e
def test_save_position_dialog_rejects_empty_name(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Empty profile name → warning notify, dialog stays open.

    This is the other branch of the same handler.  In the broken
    pre-2026-05-26 code, *neither* branch ran — both inputs were
    silently dropped.  Exercising the empty-name path confirms the
    guard clause inside the new ``_do_save`` body executes
    synchronously and surfaces to the user.
    """
    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)

    save_position_btn = page.get_by_role("button", name="Save Position")
    expect(save_position_btn).to_be_visible(timeout=15_000)
    save_position_btn.click()

    expect(page.locator("text=Save Position Profile").first).to_be_visible(
        timeout=5_000
    )

    # Click Save without typing a name.
    page.get_by_role("button", name="Save", exact=True).click()

    # Warning notify appears...
    expect(page.locator("text=Enter a profile name").first).to_be_visible(timeout=5_000)
    # ...and the dialog stays open (Save Position Profile heading
    # still visible).  This confirms the early-return path was
    # actually reached — not silently dropped.
    expect(page.locator("text=Save Position Profile").first).to_be_visible()
