"""E2E tests for Apply + the receiver-out-of-sync indicator (issue #65).

Extends the GPS page suite (``test_gps_profile_picker.py`` covers the
read-only shell from #64). This ticket makes the RTCM matrix and
data-link port(s) editable and wires them to
``POST /api/device/apply-config``:

- Toggling a matrix cell flips the "In sync" badge to "Receiver out of
  sync"; a successful Apply clears it back to "In sync" (usage path 2
  — tweak one cell, apply, iterate).
- A factory-fresh receiver (no RTCM anywhere) can't infer a data-link
  port — Apply is disabled with a clear prompt until the operator
  checks at least one UART box.
- Breaking a context-free rule (1005 must stay on the chosen data-link
  port) surfaces a named pre-write refusal and writes nothing.

Locators target the stable CSS class hooks the page renders (see
``ui/pages/gps_config.py``), matching the existing suite's convention.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from sp_rtk_base.services.drivers.fake import FAKE_FACTORY_PORT


@pytest.fixture()
def connected_gps_factory(api_base_url: str) -> Iterator[None]:
    """Connect the fake driver in its factory-fresh state (no RTCM anywhere).

    Uses ``FakeGpsDriver.FAKE_FACTORY_PORT`` — the sentinel that resets
    the fake driver's RTCM read-back to empty, the only way to reach
    the GPS page's "data-link port cannot be inferred" prompt without
    real hardware.
    """
    try:
        httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
    except Exception:
        pass

    payload = {"vendor": "fake", "port": FAKE_FACTORY_PORT, "baud_rate": 115200}
    resp = httpx.post(f"{api_base_url}/api/device/connect", json=payload, timeout=10.0)
    if resp.status_code not in (200, 409):
        raise RuntimeError(
            f"Could not connect FakeGpsDriver (factory): "
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
def test_toggle_cell_then_apply_clears_out_of_sync(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Usage path 2: connect to a configured base, tweak one cell, Apply."""
    _goto_gps_config(page, base_url)

    sync_badge = page.locator(".sync-badge")
    expect(sync_badge).to_have_text("In sync", timeout=10_000)

    # 1077 is off on UART2 by default (see FakeGpsDriver) — toggle it on.
    cell = page.locator(".rtcm-cell-1077-UART2")
    expect(cell).to_have_text("-")
    cell.click()
    expect(cell).to_have_text("✓")
    expect(sync_badge).to_have_text("Receiver out of sync")

    apply_btn = page.get_by_role("button", name="Apply")
    expect(apply_btn).to_be_enabled()
    apply_btn.click()

    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )
    expect(sync_badge).to_have_text("In sync")
    # The applied cell stays on after a successful, verified apply.
    expect(cell).to_have_text("✓")


@pytest.mark.e2e
def test_factory_receiver_blocks_apply_until_data_link_port_chosen(
    page: Page,
    base_url: str,
    connected_gps_factory: None,
) -> None:
    """No RTCM anywhere -> nothing inferred -> Apply is blocked with a prompt."""
    _goto_gps_config(page, base_url)

    expect(page.locator(".data-link-blocked")).to_be_visible(timeout=10_000)
    apply_btn = page.get_by_role("button", name="Apply")
    expect(apply_btn).to_be_disabled()

    # Turn on 1005 for UART1 so the chosen data-link port has a row on,
    # then explicitly pick UART1 as the data-link port.
    page.locator(".rtcm-cell-1005-UART1").click()
    page.locator(".data-link-checkbox-UART1").click()

    expect(page.locator(".data-link-blocked")).not_to_be_visible(timeout=10_000)
    expect(apply_btn).to_be_enabled(timeout=10_000)

    apply_btn.click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )


@pytest.mark.e2e
def test_breaking_1005_rule_shows_named_refusal_and_writes_nothing(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Turning off 1005 on the only data-link port is refused pre-write."""
    _goto_gps_config(page, base_url)

    # FakeGpsDriver's default state has 1005 enabled on UART1, which is
    # inferred as the sole data-link port. Turn it off.
    cell = page.locator(".rtcm-cell-1005-UART1")
    expect(cell).to_have_text("✓", timeout=10_000)
    cell.click()
    expect(cell).to_have_text("-")

    page.get_by_role("button", name="Apply").click()

    result = page.locator(".apply-result")
    expect(result).to_contain_text("Apply refused", timeout=10_000)
    expect(result).to_contain_text("1005")
    expect(result).to_contain_text("nothing was written")

    # Nothing was written — the receiver-out-of-sync badge is untouched
    # by the refusal (still reflects the pending, unwritten edit).
    expect(page.locator(".sync-badge")).to_have_text("Receiver out of sync")
