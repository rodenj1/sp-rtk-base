"""End-to-end render test for the Input page's Bluetooth section.

The Bluetooth section's real workflow needs BlueZ over D-Bus, which a
headless runner cannot supply — but *rendering* it needs nothing but a
browser, and rendering is the only thing that executes the page body at
all. `ui/pages/*` is excluded from the coverage gate, so without this
the whole section is unexecuted by any automated test and a typo in it
would first be seen by an operator.

What this pins is deliberately narrow: the section builds without
raising, the fields that should exist do, and the "RFCOMM Channel"
field that #131 removed stays gone. It also pins how the section names
the selected module (rtk_development#225): by its Module name, with the
MAC text box behind "Enter address manually".
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from sp_rtk_base.services.drivers.fake import (
    FAKE_BT_MODULE_MAC,
    FAKE_BT_MODULE_NAME,
    FAKE_BT_NAMELESS_MAC,
)

MAC = "98:D3:71:FE:FC:47"
MODULE_NAME = "RTK_BASE_TST"


def _put_input(api_base_url: str, source: str, config: dict[str, Any]) -> None:
    resp = httpx.put(
        f"{api_base_url}/api/input",
        json={"source": source, "config": config},
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text


@pytest.fixture()
def saved_input(api_base_url: str) -> Iterator[None]:
    """Put the saved Input profile back as it was after the test."""
    before = httpx.get(f"{api_base_url}/api/input", timeout=5.0).json()
    try:
        yield
    finally:
        if before.get("configured"):
            _put_input(api_base_url, before["source"], before["config"])


def _choose_bluetooth(page: Page) -> None:
    """Switch the form to Bluetooth and open "Enter address manually".

    Whether the manual entry starts open depends on the saved Input
    profile another test may have left, so open it only if it's shut.
    """
    page.get_by_label("Source Type").click()
    page.get_by_role("option", name="bluetooth").click()
    expect(page.get_by_text("Enter address manually")).to_be_visible(timeout=10_000)
    mac_field = page.get_by_label("Device Address (MAC)")
    if not mac_field.is_visible():
        page.get_by_text("Enter address manually").click()
    expect(mac_field).to_be_visible(timeout=10_000)


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

    _choose_bluetooth(page)

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

    _choose_bluetooth(page)

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
        _choose_bluetooth(page)

        page.get_by_label("Source Type").click()
        page.get_by_role("option", name="tcp").click()
        expect(page.get_by_label("Host")).to_be_visible(timeout=10_000)

    _choose_bluetooth(page)
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

    _choose_bluetooth(page)

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
    """Confirming the repair on a module that can't be reached fails readably.

    Only the fake GPS's Bluetooth modules are in range, so a
    Verification of any other MAC is Red. What matters is that the failure reaches the
    operator as a sentence in the status line rather than as a silent
    no-op or an unhandled traceback.
    """
    page.goto(f"{base_url}/input")
    expect(page.locator("text=Input Source").first).to_be_visible(timeout=15_000)

    _choose_bluetooth(page)

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


@pytest.mark.e2e
def test_the_selected_module_is_shown_by_its_module_name(
    page: Page, base_url: str, api_base_url: str, saved_input: None
) -> None:
    """The section leads with the Module name; the MAC sits behind a link.

    Editing the MAC clears the Module name at once: a name never sits
    next to a MAC it wasn't heard from, and Save doesn't persist it.
    """
    _put_input(
        api_base_url,
        "bluetooth",
        {"mac_address": MAC, "device_name": MODULE_NAME, "pin": "1234"},
    )
    page.goto(f"{base_url}/input")

    selected = page.get_by_test_id("bt-selected-module")
    selected_mac = page.get_by_test_id("bt-selected-module-mac")
    mac_field = page.get_by_label("Device Address (MAC)")
    expect(selected).to_have_text(MODULE_NAME, timeout=15_000)
    expect(selected_mac).to_have_text(f"MAC {MAC}")
    # A saved profile that names its module keeps the MAC box shut.
    expect(mac_field).to_be_hidden()

    page.get_by_text("Enter address manually").click()
    expect(mac_field).to_be_visible()
    expect(mac_field).to_have_value(MAC)

    mac_field.fill("AA:BB:CC:DD:EE:FF")
    expect(selected).to_have_text("Unnamed module")
    expect(selected_mac).to_have_text("MAC AA:BB:CC:DD:EE:FF")

    page.get_by_role("button", name="Save Input Config").click()
    expect(page.get_by_text("Input source saved")).to_be_visible(timeout=10_000)
    saved = httpx.get(f"{api_base_url}/api/input", timeout=5.0).json()
    assert saved["config"]["mac_address"] == "AA:BB:CC:DD:EE:FF"
    assert not saved["config"].get("device_name")


@pytest.mark.e2e
def test_saving_keeps_the_module_name_that_came_with_the_mac(
    page: Page, base_url: str, api_base_url: str, saved_input: None
) -> None:
    """A save that leaves the MAC alone keeps its Module name."""
    _put_input(
        api_base_url,
        "bluetooth",
        {"mac_address": MAC, "device_name": MODULE_NAME, "pin": "1234"},
    )
    page.goto(f"{base_url}/input")
    expect(page.get_by_test_id("bt-selected-module")).to_have_text(
        MODULE_NAME, timeout=15_000
    )

    page.get_by_label("PIN Code").fill("4321")
    page.get_by_role("button", name="Save Input Config").click()
    expect(page.get_by_text("Input source saved")).to_be_visible(timeout=10_000)

    saved = httpx.get(f"{api_base_url}/api/input", timeout=5.0).json()
    assert saved["config"]["mac_address"] == MAC
    assert saved["config"]["device_name"] == MODULE_NAME


@pytest.mark.e2e
def test_a_saved_mac_without_a_module_name_opens_manual_entry(
    page: Page, base_url: str, api_base_url: str, saved_input: None
) -> None:
    """With no name to show, the MAC is shown where it was typed."""
    _put_input(api_base_url, "bluetooth", {"mac_address": MAC, "pin": "1234"})
    page.goto(f"{base_url}/input")

    expect(page.get_by_test_id("bt-selected-module")).to_have_text(
        "Unnamed module", timeout=15_000
    )
    expect(page.get_by_test_id("bt-selected-module-mac")).to_have_text(f"MAC {MAC}")
    expect(page.get_by_label("Device Address (MAC)")).to_be_visible()
    expect(page.get_by_label("Device Address (MAC)")).to_have_value(MAC)


@pytest.mark.e2e
def test_picking_a_scan_result_captures_its_module_name(
    page: Page, base_url: str, api_base_url: str, saved_input: None
) -> None:
    """A scan pick selects the module by name, and Save keeps MAC and name.

    The fake GPS stands in for BlueZ's scan (it lists a named module and
    a nameless one). Setting the MAC from a pick is not the operator
    editing it, so the Module name that came with the pick survives.
    """
    _put_input(api_base_url, "bluetooth", {"mac_address": MAC, "pin": "1234"})
    page.goto(f"{base_url}/input")
    expect(page.get_by_test_id("bt-selected-module")).to_have_text(
        "Unnamed module", timeout=15_000
    )

    page.get_by_role("button", name="Scan for Devices").click()
    cards = page.get_by_test_id("bt-scan-card-name")
    expect(cards).to_have_text([FAKE_BT_MODULE_NAME, "Unnamed module"], timeout=15_000)

    cards.filter(has_text=FAKE_BT_MODULE_NAME).click()
    expect(page.get_by_test_id("bt-selected-module")).to_have_text(FAKE_BT_MODULE_NAME)
    expect(page.get_by_test_id("bt-selected-module-mac")).to_have_text(
        f"MAC {FAKE_BT_MODULE_MAC}"
    )

    page.get_by_role("button", name="Save Input Config").click()
    expect(page.get_by_text("Input source saved")).to_be_visible(timeout=10_000)
    saved = httpx.get(f"{api_base_url}/api/input", timeout=5.0).json()
    assert saved["config"]["mac_address"] == FAKE_BT_MODULE_MAC
    assert saved["config"]["device_name"] == FAKE_BT_MODULE_NAME


@pytest.mark.e2e
def test_picking_a_nameless_scan_result_saves_no_module_name(
    page: Page, base_url: str, api_base_url: str, saved_input: None
) -> None:
    """A module that advertised no name is never saved under a placeholder."""
    _put_input(
        api_base_url,
        "bluetooth",
        {"mac_address": MAC, "device_name": MODULE_NAME, "pin": "1234"},
    )
    page.goto(f"{base_url}/input")
    expect(page.get_by_test_id("bt-selected-module")).to_have_text(
        MODULE_NAME, timeout=15_000
    )

    page.get_by_role("button", name="Scan for Devices").click()
    nameless = page.get_by_test_id("bt-scan-card-name").filter(
        has_text="Unnamed module"
    )
    expect(nameless).to_be_visible(timeout=15_000)
    nameless.click()
    expect(page.get_by_test_id("bt-selected-module")).to_have_text("Unnamed module")
    expect(page.get_by_test_id("bt-selected-module-mac")).to_have_text(
        f"MAC {FAKE_BT_NAMELESS_MAC}"
    )

    page.get_by_role("button", name="Save Input Config").click()
    expect(page.get_by_text("Input source saved")).to_be_visible(timeout=10_000)
    saved = httpx.get(f"{api_base_url}/api/input", timeout=5.0).json()
    assert saved["config"]["mac_address"] == FAKE_BT_NAMELESS_MAC
    assert not saved["config"].get("device_name")


@pytest.mark.e2e
def test_a_verification_fills_in_the_module_name_for_a_typed_mac(
    page: Page, base_url: str, api_base_url: str, saved_input: None
) -> None:
    """A MAC typed by hand gains the Module name a Verification heard.

    The fake GPS's named Bluetooth module (``FAKE_BT_MODULE_MAC``) answers the
    Verification as a real one would. The Verification never writes the
    saved profile itself: the name reaches it only through the next Save.
    """
    _put_input(api_base_url, "tcp", {"host": "127.0.0.1", "port": 5015})
    page.goto(f"{base_url}/input")
    expect(page.locator("text=Input Source").first).to_be_visible(timeout=15_000)
    _choose_bluetooth(page)

    selected = page.get_by_test_id("bt-selected-module")
    page.get_by_label("Device Address (MAC)").fill(FAKE_BT_MODULE_MAC)
    page.get_by_label("PIN Code").fill("2468")
    expect(selected).to_have_text("Unnamed module")

    page.get_by_role("button", name="Test Connection").click()
    # A PIN this server hasn't proven is re-paired, with consent.
    page.get_by_role("button", name="Remove pairing and test").click()

    expect(page.get_by_role("button", name="Save & Start now →")).to_be_visible(
        timeout=30_000
    )
    expect(selected).to_have_text(FAKE_BT_MODULE_NAME)
    expect(page.get_by_test_id("bt-selected-module-mac")).to_have_text(
        f"MAC {FAKE_BT_MODULE_MAC}"
    )
    unsaved = httpx.get(f"{api_base_url}/api/input", timeout=5.0).json()
    assert unsaved["source"] == "tcp"

    page.get_by_role("button", name="Save Input Config").click()
    expect(page.get_by_text("Input source saved")).to_be_visible(timeout=10_000)
    saved = httpx.get(f"{api_base_url}/api/input", timeout=5.0).json()
    assert saved["config"]["mac_address"] == FAKE_BT_MODULE_MAC
    assert saved["config"]["device_name"] == FAKE_BT_MODULE_NAME
