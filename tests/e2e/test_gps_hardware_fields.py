"""E2E tests for the Advanced GPS page's hardware-section controls
(issue #100).

Prior to this ticket, port protocols, GNSS constellations and the
"Hardware Section" extras (measurement rate, UART bauds, dynamics
model, base mode, elevation mask, BeiDou B2, SPI) rendered as
read-only badges/labels. This suite asserts each is now a real,
editable widget wired into the same whole-form Apply/out-of-sync
machinery the RTCM matrix and data-link picker already used (see
``test_gps_apply.py``), plus the base-mode survey_in lock and the
BeiDou B2 grey-out.

Locators target the stable CSS class hooks the page renders (see
``ui/pages/gps_config.py``), matching the existing suite's convention.
Every editable widget here is a Quasar ``q-checkbox``/``q-select``,
not a plain HTML form control, so checked/enabled state is asserted
via its ``aria-checked``/``aria-disabled`` attributes (Playwright's
``to_be_checked()``/``to_be_disabled()`` assume a native control and
don't see these) — see :func:`_expect_checked`/:func:`_expect_disabled`.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect


def _goto_gps_config(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/gps-config")
    expect(page.locator("text=Advanced GPS Configuration").first).to_be_visible(
        timeout=15_000
    )


def _expect_checked(checkbox: Locator, *, checked: bool) -> None:
    expect(checkbox).to_have_attribute("aria-checked", "true" if checked else "false")


def _expect_disabled(checkbox: Locator, *, disabled: bool) -> None:
    if disabled:
        expect(checkbox).to_have_attribute("aria-disabled", "true")
    else:
        expect(checkbox).not_to_have_attribute("aria-disabled", "true")


@pytest.mark.e2e
def test_gnss_checkbox_edits_the_form_and_applies(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """SBAS is off by default on the fake driver — check it, out of
    sync fires, Apply clears it and the checkbox stays checked."""
    _goto_gps_config(page, base_url)

    sync_badge = page.locator(".sync-badge")
    expect(sync_badge).to_have_text("In sync", timeout=10_000)

    sbas = page.locator(".gnss-checkbox-sbas")
    _expect_checked(sbas, checked=False)
    sbas.click()
    expect(sync_badge).to_contain_text("unapplied change")

    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )
    expect(sync_badge).to_have_text("In sync")
    _expect_checked(sbas, checked=True)


@pytest.mark.e2e
def test_port_protocol_checkbox_edits_the_form_and_applies(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """UART2 doesn't speak NMEA-in by default on the fake driver."""
    _goto_gps_config(page, base_url)

    sync_badge = page.locator(".sync-badge")
    expect(sync_badge).to_have_text("In sync", timeout=10_000)

    nmea_in = page.locator(".port-protocol-UART2-in-NMEA")
    _expect_checked(nmea_in, checked=False)
    nmea_in.click()
    expect(sync_badge).to_contain_text("unapplied change")

    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )
    expect(sync_badge).to_have_text("In sync")
    _expect_checked(nmea_in, checked=True)


@pytest.mark.e2e
def test_hardware_extras_are_editable_and_apply(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Measurement rate, dynamics model, elevation mask and SPI are
    real controls, not static text, and Apply asserts every edit."""
    _goto_gps_config(page, base_url)

    sync_badge = page.locator(".sync-badge")
    expect(sync_badge).to_have_text("In sync", timeout=10_000)

    meas_rate = page.locator(".hw-field-meas-rate-input input")
    expect(meas_rate).to_have_value("1000")
    meas_rate.fill("500")
    meas_rate.blur()

    dyn_model = page.locator(".hw-field-dyn-model-select")
    dyn_model.click()
    page.get_by_role("option", name="stationary", exact=True).click()

    elevation = page.locator(".hw-field-elevation-mask-input input")
    elevation.fill("10")
    elevation.blur()

    spi = page.locator(".hw-field-spi-checkbox")
    _expect_checked(spi, checked=True)  # on by default on the fake driver
    spi.click()

    expect(sync_badge).to_contain_text("unapplied change")

    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )
    expect(sync_badge).to_have_text("In sync")
    expect(meas_rate).to_have_value("500")
    _expect_checked(spi, checked=False)


@pytest.mark.e2e
def test_uart_baud_selects_are_editable_and_apply(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """UART1/UART2 baud are real selects seeded from the receiver
    (57600 / 115200 on the fake driver), not static text.

    Only UART2 is actually changed here — a changed UART1 baud
    reopens this console's own serial link (see
    ``DeviceService.apply_receiver_config``), which is exercised by
    its own suite rather than risked here. UART1's select is still
    proven to be a real, distinct combobox control (not static text)
    via its role — opening it too would risk racing UART2's identical
    option labels in the same dropdown-menu layer.
    """
    _goto_gps_config(page, base_url)

    sync_badge = page.locator(".sync-badge")
    expect(sync_badge).to_have_text("In sync", timeout=10_000)

    uart1 = page.locator(".hw-field-baud-uart1")
    uart2 = page.locator(".hw-field-baud-uart2")
    expect(uart1).to_contain_text("57600")
    expect(uart2).to_contain_text("115200")
    expect(uart1.locator('[role="combobox"]')).to_have_count(1)

    uart2.click()
    page.get_by_role("option", name="230400", exact=True).click()
    expect(sync_badge).to_contain_text("unapplied change")

    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )
    expect(sync_badge).to_have_text("In sync")
    expect(uart2).to_contain_text("230400")


@pytest.mark.e2e
def test_fixed_without_position_shows_named_refusal(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """The fake driver starts ``disabled`` with no fixed position on
    record — picking ``fixed`` from the new base-mode select and
    applying surfaces the existing ``tmode_fixed_requires_coordinates``
    refusal rather than a silent or generic failure."""
    _goto_gps_config(page, base_url)

    tmode_select = page.locator(".hw-field-tmode-select")
    expect(tmode_select).to_be_visible(timeout=10_000)
    tmode_select.click()
    page.get_by_role("option", name="fixed", exact=True).click()

    page.get_by_role("button", name="Apply").click()

    result = page.locator(".apply-result")
    expect(result).to_contain_text("Apply refused", timeout=10_000)
    expect(result).to_contain_text("tmode_fixed_requires_coordinates")
    expect(result).to_contain_text("nothing was written")


@pytest.mark.e2e
def test_bds_b2_greys_out_when_beidou_disabled(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """BeiDou is on by default on the fake driver, so B2 starts
    enabled; unchecking BeiDou greys B2 out and Apply sends it off."""
    _goto_gps_config(page, base_url)

    beidou = page.locator(".gnss-checkbox-beidou")
    bds_b2 = page.locator(".hw-field-bds-b2-checkbox")
    _expect_checked(beidou, checked=True)
    _expect_disabled(bds_b2, disabled=False)

    beidou.click()
    _expect_checked(beidou, checked=False)
    _expect_disabled(bds_b2, disabled=True)

    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )
    _expect_checked(bds_b2, checked=False)


@pytest.mark.e2e
def test_base_mode_offers_disabled_and_fixed(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """The fake driver starts in ``disabled`` — the select offers
    ``fixed`` as the other choice, and picking it is a normal edit."""
    _goto_gps_config(page, base_url)

    tmode_select = page.locator(".hw-field-tmode-select")
    expect(tmode_select).to_be_visible(timeout=10_000)
    expect(page.locator(".hw-field-tmode-note")).to_have_count(0)

    sync_badge = page.locator(".sync-badge")
    expect(sync_badge).to_have_text("In sync")

    tmode_select.click()
    page.get_by_role("option", name="fixed", exact=True).click()
    expect(sync_badge).to_contain_text("unapplied change")


@pytest.mark.e2e
def test_survey_in_shows_as_locked_current_value(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    """A receiver mid-survey shows survey_in as the current value —
    not selectable, no phantom unapplied change — with a pointer to
    the Survey page. Overriding it with ``disabled``/``fixed`` is a
    legitimate edit."""
    resp = httpx.post(
        f"{api_base_url}/api/device/configure/survey-in",
        json={},
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.text

    _goto_gps_config(page, base_url)

    expect(page.locator(".hw-field-tmode-note")).to_contain_text(
        "survey_in", timeout=10_000
    )
    expect(page.locator(".hw-field-tmode-survey-link")).to_be_visible()
    # No selectable option ever reads "survey_in" — seedable, not pickable.
    expect(page.locator(".hw-field-tmode-select")).to_be_visible()

    sync_badge = page.locator(".sync-badge")
    expect(sync_badge).to_have_text("In sync")

    page.locator(".hw-field-tmode-select").click()
    page.get_by_role("option", name="disabled", exact=True).click()
    expect(sync_badge).to_contain_text("unapplied change")
