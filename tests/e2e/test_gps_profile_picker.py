"""E2E tests for the GPS page's profile picker dropdown (issues #64, #105).

Extends the existing GPS page suite (``test_gps_config_buttons.py``,
``test_gps_data_flow.py``). Issue #105 turned the picker's card list
into a Quasar ``q-select`` dropdown, so these tests cover:

- The form (ports, GNSS, RTCM matrix) seeds from the live receiver via
  ``connected_gps``, not from any profile.
- The picker's dropdown lists built-ins before customs, tagging
  compatibility: an incompatible option is disabled (unclickable) and
  carries a tooltip explaining why.
- The deterministic default is visibly highlighted/suggested in the
  dropdown but never written into the form until picked.
- The unconfirmed-hardware banner appears (and no option is
  highlighted as suggested) when the receiver's identity can't be
  confirmed — driven through ``FakeGpsDriver``'s ``FAKE_UNKNOWN_HW_PORT``
  sentinel.
- The matrix highlights the data-link columns and tags 1005 required.

Locators target the stable ``profile-*`` / ``rtcm-*`` CSS class hooks
the page renders (see ``ui/pages/gps_config.py``) rather than text
substrings — several page strings (e.g. the unconfirmed-hardware
banner's "...no profile is suggested") share words with the things
under test, which makes plain ``get_by_text`` matches ambiguous.

The dropdown's option list only exists in the DOM while the popup is
open (it's a Quasar ``q-menu``, mounted/unmounted on open/close), so
every test that inspects an option opens the picker first via
``.profile-picker`` before locating ``.profile-option-*``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from sp_rtk_base.services.drivers.fake import FAKE_UNKNOWN_HW_PORT

CUSTOM_INCOMPATIBLE_PROFILE = {
    "name": "e2e-f9r-only",
    "version": 1,
    "hardware": "ZED-F9R",
    "data_link_port": ["UART1"],
    "rtcm_stream": {"matrix": {"1005": {"UART1": True}}},
}

BUILTIN_NAME = "ublox-f9p-base-standard"


@pytest.fixture()
def custom_incompatible_profile(api_base_url: str) -> Iterator[None]:
    """Create a custom profile incompatible with the fake driver's ZED-F9P.

    ``FakeGpsDriver`` reports a confirmed ``ZED-F9P`` target by
    default. ``ZED-F9R`` is neither that specific model nor in a
    family set matching a *different* model token, so it's reliably
    incompatible (see ``models.hardware_identity.is_compatible``).
    """
    resp = httpx.post(
        f"{api_base_url}/api/profiles",
        json=CUSTOM_INCOMPATIBLE_PROFILE,
        timeout=5.0,
    )
    assert resp.status_code in (201, 409), resp.text
    try:
        yield
    finally:
        httpx.delete(
            f"{api_base_url}/api/profiles/{CUSTOM_INCOMPATIBLE_PROFILE['name']}",
            timeout=5.0,
        )


@pytest.fixture()
def connected_gps_unknown_hw(api_base_url: str) -> Iterator[None]:
    """Connect the fake driver with an unresolved hardware identity.

    Uses ``FakeGpsDriver.FAKE_UNKNOWN_HW_PORT`` — the sentinel that
    makes the fake driver report ``hardware_confidence=unknown``
    instead of its normal confirmed ``ZED-F9P`` — the only way to
    reach the GPS page's "unconfirmed hardware" banner without real
    hardware.
    """
    try:
        httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
    except Exception:
        pass

    payload = {"vendor": "fake", "port": FAKE_UNKNOWN_HW_PORT, "baud_rate": 115200}
    resp = httpx.post(f"{api_base_url}/api/device/connect", json=payload, timeout=10.0)
    if resp.status_code not in (200, 409):
        raise RuntimeError(
            f"Could not connect FakeGpsDriver (unknown hw): "
            f"HTTP {resp.status_code} — {resp.text}"
        )
    try:
        yield
    finally:
        try:
            httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
        except Exception:
            pass


def _goto_gps_config(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/gps-config")
    expect(page.locator("text=Advanced GPS Configuration").first).to_be_visible(
        timeout=15_000
    )


def _open_picker(page: Page) -> None:
    """Open the profile dropdown — its options only mount while open."""
    expect(page.locator(".profile-picker")).to_be_visible(timeout=10_000)
    page.locator(".profile-picker").click()


@pytest.mark.e2e
def test_form_seeds_matrix_and_gnss_from_live_receiver(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """The RTCM matrix and GNSS checkboxes reflect the fake driver's live
    state.

    ``FakeGpsDriver`` enables RTCM 1005/1077/1087/1097/1127/1230 on
    USB+UART1 (not UART2) and enables GPS/GLONASS/Galileo/BeiDou but
    not SBAS/QZSS — none of which matches the built-in profile's
    matrix, so a passing assertion here also proves the form isn't
    seeded from a profile.
    """
    _goto_gps_config(page, base_url)

    expect(page.locator(".rtcm-cell-1077-UART1")).to_have_text("✓", timeout=10_000)
    expect(page.locator(".rtcm-cell-1077-UART2")).to_have_text("-")
    expect(page.locator(".rtcm-cell-1077-USB")).to_have_text("✓")
    # A row with no live data at all stays off everywhere.
    expect(page.locator(".rtcm-cell-1074-UART1")).to_have_text("-")

    expect(page.locator(".gnss-checkbox-gps")).to_have_attribute("aria-checked", "true")
    expect(page.locator(".gnss-checkbox-sbas")).to_have_attribute(
        "aria-checked", "false"
    )


@pytest.mark.e2e
def test_matrix_tags_1005_required_and_highlights_data_link_columns(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """1005 carries a "Required" tag; UART1/UART2 headers are highlighted."""
    _goto_gps_config(page, base_url)

    row_1005 = page.locator(".rtcm-row-1005")
    expect(row_1005).to_be_visible(timeout=10_000)
    expect(row_1005.locator(".q-badge:text-is('Required')")).to_be_visible()

    expect(page.locator(".rtcm-col-header-UART1")).to_have_class(
        re.compile("text-primary")
    )
    expect(page.locator(".rtcm-col-header-UART2")).to_have_class(
        re.compile("text-primary")
    )
    expect(page.locator(".rtcm-col-header-USB")).not_to_have_class(
        re.compile("text-primary")
    )


@pytest.mark.e2e
def test_picker_is_a_dropdown_listing_builtin_before_custom(
    page: Page,
    base_url: str,
    connected_gps: None,
    custom_incompatible_profile: None,
) -> None:
    """AC: "The picker renders as a dropdown rather than a card list."

    The control itself is a ``q-select`` (queried via ``.profile-picker``,
    a ``role=combobox``), and its options — only mounted once the
    dropdown is opened — list the built-in before the custom.
    """
    _goto_gps_config(page, base_url)

    picker = page.locator(".profile-picker")
    expect(picker).to_be_visible(timeout=10_000)
    expect(picker.locator('[role="combobox"]')).to_have_count(1)

    _open_picker(page)
    custom_name = CUSTOM_INCOMPATIBLE_PROFILE["name"]
    builtin_option = page.locator(f".profile-option-{BUILTIN_NAME}")
    custom_option = page.locator(f".profile-option-{custom_name}")
    expect(builtin_option).to_be_visible(timeout=10_000)
    expect(custom_option).to_be_visible(timeout=10_000)

    builtin_box = builtin_option.bounding_box()
    custom_box = custom_option.bounding_box()
    assert builtin_box is not None and custom_box is not None
    assert builtin_box["y"] < custom_box["y"], (
        "built-in profile should render above the custom profile"
    )


@pytest.mark.e2e
def test_incompatible_option_is_disabled_with_tooltip(
    page: Page,
    base_url: str,
    connected_gps: None,
    custom_incompatible_profile: None,
) -> None:
    """AC: "Profiles incompatible with the connected hardware are disabled
    and carry a tooltip explaining why."

    Covers both halves: the option carries the disabled CSS hook and a
    hover tooltip naming the mismatched hardware, *and* clicking it is
    a no-op — nothing gets selected (the "modified from X" indicator,
    which only appears once a profile is picked, stays hidden).
    """
    _goto_gps_config(page, base_url)
    _open_picker(page)

    custom_name = CUSTOM_INCOMPATIBLE_PROFILE["name"]
    builtin_option = page.locator(f".profile-option-{BUILTIN_NAME}")
    custom_option = page.locator(f".profile-option-{custom_name}")
    expect(builtin_option).to_be_visible(timeout=10_000)
    expect(custom_option).to_be_visible(timeout=10_000)

    expect(custom_option).to_have_class(re.compile("profile-option-disabled"))
    expect(builtin_option).not_to_have_class(re.compile("profile-option-disabled"))

    custom_option.hover()
    expect(page.get_by_text("not for this hardware (ZED-F9P)")).to_be_visible(
        timeout=5_000
    )

    # Disabled options stay hoverable (that's how the tooltip above
    # works) but a click must still be a no-op — force the click since
    # the option isn't Quasar-"clickable" and Playwright's actionability
    # check would otherwise refuse it.
    custom_option.click(force=True)
    page.wait_for_timeout(300)
    expect(page.locator(".modified-badge")).to_be_hidden()


@pytest.mark.e2e
def test_suggested_profile_is_highlighted_but_not_written_to_form(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """AC: "The suggested profile for the detected hardware is highlighted."

    The confirmed-compatible built-in carries the suggested badge and
    highlight class in the dropdown; the form stays live-seeded until
    actually picked.
    """
    _goto_gps_config(page, base_url)
    _open_picker(page)

    builtin_option = page.locator(f".profile-option-{BUILTIN_NAME}")
    expect(builtin_option).to_be_visible(timeout=10_000)
    expect(builtin_option).to_have_class(re.compile("profile-option-suggested"))
    expect(builtin_option.locator(".profile-suggested-badge")).to_be_visible()

    page.keyboard.press("Escape")

    # The built-in profile's matrix has UART2 enabled for 1077; the
    # fake driver's live read-back does not (UART1+USB only). If the
    # form had been written from the "suggested" profile, this cell
    # would show "on". It must instead reflect the live receiver.
    expect(page.locator(".rtcm-cell-1077-UART2")).to_have_text("-", timeout=10_000)
    expect(page.locator(".modified-badge")).to_be_hidden()


@pytest.mark.e2e
def test_unconfirmed_hardware_banner_shown_with_no_suggestion(
    page: Page,
    base_url: str,
    connected_gps_unknown_hw: None,
) -> None:
    """AC: "A banner appears when the hardware has not been confirmed."

    Unresolved hardware identity shows the banner and highlights no
    option as suggested.
    """
    _goto_gps_config(page, base_url)

    profile_card = page.locator(".q-card:has(.profile-picker)").first
    expect(profile_card).to_be_visible(timeout=10_000)
    expect(profile_card.get_by_text("Unconfirmed hardware")).to_be_visible(
        timeout=10_000
    )

    _open_picker(page)
    expect(page.locator(".profile-suggested-badge")).to_have_count(0)
