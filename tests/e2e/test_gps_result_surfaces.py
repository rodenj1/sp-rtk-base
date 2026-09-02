"""E2E tests for the Advanced GPS page's result surfaces (issue #101).

Covers what's reachable without hardware:

- The warning strip renders one line per warning (never a joined blob),
  is replaced by the next Apply, and clears on disconnect — driven by
  ``FakeGpsDriver.FAKE_FLASH_DIVERGENCE_PORT``, the sentinel that
  queues a step warning after every RTCM matrix write.
- The headline verdict line carries a neutral warning count alongside
  the "Applied and verified" verdict when warnings are present.
- The step log is append-only across successive Apply presses within
  one connection, and clears on disconnect.
- The three-state badge's neutral "pending" wording (the "failed
  verification" state itself needs a genuinely dishonest read-back,
  which the fake driver doesn't simulate — that logic is covered at
  the unit level in ``test_gps_config_helpers.py``).

Locators target the stable CSS class hooks the page renders (see
``ui/pages/gps_config.py``), matching the existing suite's convention.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from sp_rtk_base.services.drivers.fake import FAKE_FLASH_DIVERGENCE_PORT


@pytest.fixture()
def connected_gps_flash_divergence(api_base_url: str) -> Iterator[None]:
    """Connect the fake driver in its flash-divergence state.

    Uses ``FakeGpsDriver.FAKE_FLASH_DIVERGENCE_PORT`` — the sentinel
    that makes every RTCM matrix write queue a step warning, the only
    way to reach the GPS page's warning strip without a real producer.
    """
    try:
        httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
    except Exception:
        pass

    payload = {
        "vendor": "fake",
        "port": FAKE_FLASH_DIVERGENCE_PORT,
        "baud_rate": 115200,
    }
    resp = httpx.post(f"{api_base_url}/api/device/connect", json=payload, timeout=10.0)
    if resp.status_code not in (200, 409):
        raise RuntimeError(
            f"Could not connect FakeGpsDriver (flash-divergence): "
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
def test_warning_strip_shows_one_line_per_warning_and_headline_counts_it(
    page: Page,
    base_url: str,
    connected_gps_flash_divergence: None,
) -> None:
    _goto_gps_config(page, base_url)

    # Toggle a matrix cell so the rtcm_matrix step actually runs.
    page.locator(".rtcm-cell-1077-UART2").click()
    page.get_by_role("button", name="Apply").click()

    expect(page.locator(".warning-strip")).to_be_visible(timeout=10_000)
    lines = page.locator(".warning-strip-line")
    expect(lines).to_have_count(1)
    expect(lines.first).to_contain_text("flash")

    headline = page.locator(".apply-result")
    expect(headline).to_contain_text("Applied and verified", timeout=10_000)
    expect(headline).to_contain_text("1 warning")


@pytest.mark.e2e
def test_warning_strip_is_replaced_not_accumulated(
    page: Page,
    base_url: str,
    connected_gps_flash_divergence: None,
) -> None:
    """A step that warned on the previous Apply retries (rather than
    being skipped) and so warns again — the strip still shows exactly
    one line, proving it was replaced rather than appended to."""
    _goto_gps_config(page, base_url)

    page.locator(".rtcm-cell-1077-UART2").click()
    page.get_by_role("button", name="Apply").click()
    lines = page.locator(".warning-strip-line")
    expect(lines).to_have_count(1, timeout=10_000)

    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )
    expect(lines).to_have_count(1)


@pytest.mark.e2e
def test_warning_strip_clears_on_disconnect(
    page: Page,
    base_url: str,
    connected_gps_flash_divergence: None,
) -> None:
    _goto_gps_config(page, base_url)

    page.locator(".rtcm-cell-1077-UART2").click()
    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".warning-strip-line")).to_have_count(1, timeout=10_000)

    # Disconnect alone proves the clear — the elements are actually
    # removed from the DOM (``.clear()``), not merely hidden behind
    # ``config_card``'s visibility toggle. Reconnecting through the UI
    # port dropdown is deliberately avoided elsewhere in this suite
    # (see ``test_gps_config_buttons.py``) as brittle and no extra
    # coverage over what the REST-driven fixtures already exercise.
    page.get_by_role("button", name="Disconnect").click()
    expect(page.locator("text=Disconnected").first).to_be_visible(timeout=10_000)
    expect(page.locator(".warning-strip-line")).to_have_count(0)


@pytest.mark.e2e
def test_step_log_is_append_only_across_applies(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    _goto_gps_config(page, base_url)

    page.locator(".rtcm-cell-1077-UART2").click()
    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )

    # ``APPLY_STEPS`` is a fixed 8-step sequence — every Apply appends
    # exactly one ``ApplyStepResult`` per step, whatever its outcome.
    steps_per_apply = 8
    log = page.locator(".step-log")
    expect(log.locator(".step-log-entry")).to_have_count(steps_per_apply)

    page.locator(".rtcm-cell-1077-UART1").click()
    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )

    # Append-only: the second Apply's steps land on top of the first's,
    # never replacing them.
    expect(log.locator(".step-log-entry")).to_have_count(steps_per_apply * 2)


@pytest.mark.e2e
def test_step_log_clears_on_disconnect(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    _goto_gps_config(page, base_url)

    page.locator(".rtcm-cell-1077-UART2").click()
    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".apply-result")).to_contain_text(
        "Applied and verified", timeout=10_000
    )
    expect(page.locator(".step-log-entry").first).to_be_visible(timeout=10_000)

    page.get_by_role("button", name="Disconnect").click()
    expect(page.locator("text=Disconnected").first).to_be_visible(timeout=10_000)
    expect(page.locator(".step-log-entry")).to_have_count(0)


@pytest.mark.e2e
def test_pending_badge_pluralises_with_multiple_edits(
    page: Page,
    base_url: str,
    connected_gps: None,
) -> None:
    """Two independent edits -> "2 unapplied changes", neutral colour."""
    _goto_gps_config(page, base_url)

    sync_badge = page.locator(".sync-badge")
    expect(sync_badge).to_have_text("In sync", timeout=10_000)

    page.locator(".rtcm-cell-1077-UART2").click()
    page.locator(".gnss-checkbox-sbas").click()

    expect(sync_badge).to_have_text("2 unapplied changes")
