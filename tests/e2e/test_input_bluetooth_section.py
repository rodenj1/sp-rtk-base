"""End-to-end render test for the Input page's Bluetooth section.

The Bluetooth section's real workflow needs BlueZ over D-Bus, which a
headless runner cannot supply — but *rendering* it needs nothing but a
browser, and rendering is the only thing that executes the page body at
all. `ui/pages/*` is excluded from the coverage gate, so without this
the whole section is unexecuted by any automated test and a typo in it
would first be seen by an operator.

What this pins is deliberately narrow: the section builds without
raising, the fields that should exist do, and the "RFCOMM Channel"
field that #131 removed stays gone.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_bluetooth_section_renders_without_the_rfcomm_channel_field(
    page: Page,
    base_url: str,
) -> None:
    """Switch the source to Bluetooth and inspect what the form offers.

    The RFCOMM Channel input was removed because it controlled nothing:
    it was never persisted, it read a config key nothing wrote, and the
    relay's ``BluetoothConfig`` has no channel parameter, so the value
    could not have been honoured even if it had been saved.
    """
    page.goto(f"{base_url}/input")
    expect(page.locator("text=Input Source").first).to_be_visible(timeout=15_000)

    page.get_by_label("Source Type").click()
    page.get_by_role("option", name="bluetooth").click()

    # The fields that survive.
    expect(page.get_by_label("Device Address (MAC)")).to_be_visible(timeout=10_000)
    expect(page.get_by_label("PIN Code")).to_be_visible(timeout=10_000)

    # The field that does not.
    expect(page.get_by_label("RFCOMM Channel")).to_have_count(0)

    # The Test Connection block is where every closure added by #131
    # lives — the verification call, the consent dialog, the countdown
    # timer.  It is guarded by `bt_available`, which is true whenever
    # `sp_rtk_base_relay.core.bluetooth_manager` merely *imports*;
    # dbus-fast is pure Python and needs no running daemon, so this
    # holds on a headless runner as well as on real hardware.  Asserting
    # it is what makes this test evidence that the new code executed,
    # rather than evidence that it was skipped.
    expect(page.get_by_role("button", name="Test Connection")).to_be_visible()

    # A section that raised while building would leave the page without
    # its Save button, so this is the "it built at all" assertion.
    expect(page.get_by_role("button", name="Save Input Config")).to_be_visible()


@pytest.mark.e2e
def test_save_and_start_is_not_offered_without_a_green(
    page: Page,
    base_url: str,
) -> None:
    """ "Save & Start now →" is a Green's affordance, not a standing button.

    It appears only for the lifetime of a Green and disappears with it,
    so on a freshly-loaded form — which holds no Green — it must not be
    on screen at all.
    """
    page.goto(f"{base_url}/input")
    expect(page.locator("text=Input Source").first).to_be_visible(timeout=15_000)

    page.get_by_label("Source Type").click()
    page.get_by_role("option", name="bluetooth").click()
    expect(page.get_by_label("Device Address (MAC)")).to_be_visible(timeout=10_000)

    expect(page.get_by_role("button", name="Save & Start now →")).not_to_be_visible()


@pytest.mark.e2e
def test_switching_source_away_and_back_rebuilds_cleanly(
    page: Page,
    base_url: str,
) -> None:
    """The Bluetooth section survives being torn down and rebuilt.

    Each rebuild installs a fresh one-second countdown timer, and a
    leaked one from a previous build would keep writing to the labels of
    a section that no longer exists. A page that has been round-tripped
    and still renders and saves is the observable end of that.
    """
    page.goto(f"{base_url}/input")
    expect(page.locator("text=Input Source").first).to_be_visible(timeout=15_000)

    for _ in range(3):
        page.get_by_label("Source Type").click()
        page.get_by_role("option", name="bluetooth").click()
        expect(page.get_by_label("Device Address (MAC)")).to_be_visible(timeout=10_000)

        page.get_by_label("Source Type").click()
        page.get_by_role("option", name="tcp").click()
        expect(page.get_by_label("Host")).to_be_visible(timeout=10_000)

    page.get_by_label("Source Type").click()
    page.get_by_role("option", name="bluetooth").click()
    expect(page.get_by_label("Device Address (MAC)")).to_be_visible(timeout=10_000)
    expect(page.get_by_role("button", name="Test Connection")).to_be_visible()
    expect(page.get_by_label("RFCOMM Channel")).to_have_count(0)


@pytest.mark.e2e
def test_an_unproven_pin_asks_before_it_destroys_anything(
    page: Page,
    base_url: str,
) -> None:
    """Test Connection on an unproven PIN prompts rather than demolishing.

    This exercises the whole wiring — page → service → refusal → dialog
    — with no hardware involved, because the refusal is raised *before*
    a `BluetoothManager` is ever constructed. That ordering is the point
    of the refusal, so a headless runner is a fair test of it.

    The operator typed a PIN, so they expect something to change; they
    do not expect a working pairing to be dropped to test it.
    """
    page.goto(f"{base_url}/input")
    expect(page.locator("text=Input Source").first).to_be_visible(timeout=15_000)

    page.get_by_label("Source Type").click()
    page.get_by_role("option", name="bluetooth").click()
    expect(page.get_by_label("Device Address (MAC)")).to_be_visible(timeout=10_000)

    page.get_by_label("Device Address (MAC)").fill("AA:BB:CC:DD:EE:FF")
    page.get_by_label("PIN Code").fill("4321")
    page.get_by_role("button", name="Test Connection").click()

    expect(page.locator("text=Re-pair with this PIN?")).to_be_visible(timeout=15_000)
    # The risk is stated at the only moment the operator can weigh it,
    # with the device in front of them.
    expect(page.locator("text=will be left unpaired")).to_be_visible()

    # Cancelling touches nothing and leaves the form as it was.
    page.get_by_role("button", name="Cancel").click()
    expect(page.locator("text=Re-pair with this PIN?")).not_to_be_visible()
    expect(page.get_by_label("Device Address (MAC)")).to_have_value("AA:BB:CC:DD:EE:FF")


@pytest.mark.e2e
def test_a_verification_that_cannot_run_reports_legibly(
    page: Page,
    base_url: str,
) -> None:
    """Confirming the repair on a runner with no adapter fails readably.

    There is no BlueZ adapter here, so the Verification cannot get past
    building its manager. What matters is that the failure reaches the
    operator as a sentence in the status line rather than as a silent
    no-op or an unhandled traceback.
    """
    page.goto(f"{base_url}/input")
    expect(page.locator("text=Input Source").first).to_be_visible(timeout=15_000)

    page.get_by_label("Source Type").click()
    page.get_by_role("option", name="bluetooth").click()
    expect(page.get_by_label("Device Address (MAC)")).to_be_visible(timeout=10_000)

    page.get_by_label("Device Address (MAC)").fill("AA:BB:CC:DD:EE:FF")
    page.get_by_label("PIN Code").fill("4321")
    page.get_by_role("button", name="Test Connection").click()
    expect(page.locator("text=Re-pair with this PIN?")).to_be_visible(timeout=15_000)

    page.get_by_role("button", name="Remove pairing and test").click()

    # Either a stage-attributed Red or the "could not run" line — both
    # are legible sentences, and neither is a raw traceback.  The button
    # must also come back, or the operator is stuck.
    expect(page.locator("text=/Red —|could not run/")).to_be_visible(timeout=30_000)
    expect(page.get_by_role("button", name="Test Connection")).to_be_enabled(
        timeout=30_000
    )
