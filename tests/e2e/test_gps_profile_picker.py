"""E2E tests for the GPS page's profile picker + live-seeded form (issue #64).

Extends the existing GPS page suite (``test_gps_config_buttons.py``,
``test_gps_data_flow.py``). This ticket is rendering-only — no Apply,
no Save-as — so these tests cover:

- The form (ports, GNSS, RTCM matrix) seeds from the live receiver via
  ``connected_gps``, not from any profile.
- The picker lists built-ins before customs, tagging compatibility and
  greying incompatible entries with a tooltip.
- The deterministic default is visibly suggested but never written
  into the form.
- The unconfirmed-hardware banner appears (and no default is
  suggested) when the receiver's identity can't be confirmed — driven
  through ``FakeGpsDriver``'s ``FAKE_UNKNOWN_HW_PORT`` sentinel.
- The matrix highlights the data-link columns and tags 1005 required.

Locators target the stable ``profile-*`` / ``rtcm-*`` CSS class hooks
the page renders (see ``ui/pages/gps_config.py``) rather than text
substrings — several page strings (e.g. the unconfirmed-hardware
banner's "...no profile is suggested") share words with the things
under test, which makes plain ``get_by_text`` matches ambiguous.
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


@pytest.mark.e2e
def test_form_seeds_matrix_and_gnss_from_live_receiver(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """The RTCM matrix and GNSS badges reflect the fake driver's live state.

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

    config_card = page.locator(".q-card:has(:text('Receiver Configuration'))").first
    expect(config_card.locator(".q-badge:text-is('GPS')")).to_be_visible()
    expect(config_card.locator(".q-badge:text-is('SBAS')")).to_be_visible()


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
def test_picker_lists_builtin_before_custom_and_greys_incompatible(
    page: Page,
    base_url: str,
    connected_gps: None,
    custom_incompatible_profile: None,
) -> None:
    """Built-in appears before the incompatible custom, which is greyed + tooltipped."""
    _goto_gps_config(page, base_url)

    builtin_row = page.locator(f".profile-row-{BUILTIN_NAME}")
    custom_name = CUSTOM_INCOMPATIBLE_PROFILE["name"]
    custom_row = page.locator(f".profile-row-{custom_name}")
    expect(builtin_row).to_be_visible(timeout=10_000)
    expect(custom_row).to_be_visible(timeout=10_000)

    builtin_box = builtin_row.bounding_box()
    custom_box = custom_row.bounding_box()
    assert builtin_box is not None and custom_box is not None
    assert builtin_box["y"] < custom_box["y"], (
        "built-in profile should render above the custom profile"
    )

    # Greyed + tooltip: the incompatible custom's row carries an info
    # icon whose hover tooltip names the mismatched hardware.
    info_icon = custom_row.locator(".profile-incompatible-icon")
    expect(info_icon).to_be_visible()
    info_icon.hover()
    expect(page.get_by_text("not for this hardware (ZED-F9P)")).to_be_visible(
        timeout=5_000
    )
    expect(builtin_row.locator(".profile-incompatible-icon")).to_have_count(0)


@pytest.mark.e2e
def test_default_is_suggested_but_not_written_to_form(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """The confirmed-compatible built-in is badged "Suggested"; the form stays live-seeded."""
    _goto_gps_config(page, base_url)

    builtin_row = page.locator(f".profile-row-{BUILTIN_NAME}")
    expect(builtin_row).to_be_visible(timeout=10_000)
    expect(builtin_row.locator(".profile-suggested-badge")).to_be_visible()

    # The built-in profile's matrix has UART2 enabled for 1077; the
    # fake driver's live read-back does not (UART1+USB only). If the
    # form had been written from the "suggested" profile, this cell
    # would show "on". It must instead reflect the live receiver.
    expect(page.locator(".rtcm-cell-1077-UART2")).to_have_text("-", timeout=10_000)


@pytest.mark.e2e
def test_unconfirmed_hardware_banner_shown_with_no_default(
    page: Page,
    base_url: str,
    connected_gps_unknown_hw: None,
) -> None:
    """Unresolved hardware identity shows the banner and suggests nothing."""
    _goto_gps_config(page, base_url)

    profile_card = page.locator(".q-card:has(.profile-row)").first
    expect(profile_card).to_be_visible(timeout=10_000)
    expect(profile_card.get_by_text("Unconfirmed hardware")).to_be_visible(
        timeout=10_000
    )
    expect(profile_card.locator(".profile-suggested-badge")).to_have_count(0)
