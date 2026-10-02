"""End-to-end tests: a Corrected survey-in's live progress panel (#195).

The server runs the fake driver, which behaves like a rover: Float a few
seconds after corrections start, then Fixed. Corrections come from a
scripted fake NTRIP caster in the test process, on localhost.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.fixtures.scripted_ntrip_caster import FakeCaster, Script
from tests.unit.msm_frames import other_frame

ICY = b"ICY 200 OK\r\n"
# 20 ms apart: about 40 s of corrections, longer than the test runs.
STREAM = [other_frame(1005).data, other_frame(1077).data] * 1000


@pytest.fixture
def caster() -> Iterator[FakeCaster]:
    fake = FakeCaster()
    yield fake
    fake.close()


@pytest.mark.e2e
def test_the_progress_panel_shows_rtk_corrections_and_fixed_time(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    clean_config: None,
    caster: FakeCaster,
) -> None:
    caster.scripts.append(Script(reply=ICY, body=STREAM))
    created = page.request.post(
        f"{api_base_url}/api/correction-sources",
        data={
            "name": "local",
            "caster": "127.0.0.1",
            "port": caster.port,
            "mountpoint": "MP1",
            "version": "1.0",
        },
    )
    assert created.ok, created.text()

    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)
    page.get_by_role("button", name="Corrected (cm-level)").click()
    page.get_by_test_id("correction-source-select").click()
    page.get_by_role("option", name="local", exact=True).click()

    page.get_by_role("button", name="Start Survey-In").click()
    expect(page.locator("text=Start Survey-In?").first).to_be_visible(timeout=5_000)
    page.get_by_role("button", name="Start Survey", exact=True).click()

    expect(page.locator("text=Corrected against local").first).to_be_visible(
        timeout=10_000
    )
    expect(page.get_by_test_id("survey-source-state")).to_have_text(
        "Source: connected", timeout=10_000
    )
    expect(page.get_by_test_id("survey-rtk-status")).to_have_text(
        "RTK: Fixed", timeout=20_000
    )
    expect(page.get_by_test_id("survey-correction-age")).to_have_text(
        re.compile(r"Correction age: \d+\.\d s")
    )
    expect(page.locator("text=/Fixed time: [1-9]\\d*s \\/ 300s/").first).to_be_visible(
        timeout=10_000
    )
    expect(page.locator("text=/Accuracy: \\d+ mm \\/ 50 mm/").first).to_be_visible()

    page.get_by_role("button", name="Cancel Survey").first.click()
    expect(page.locator("text=Cancel Survey-In?").first).to_be_visible(timeout=5_000)
    page.get_by_role("button", name="Cancel Survey", exact=True).last.click()
    expect(page.locator("text=Cancelled by operator").first).to_be_visible(
        timeout=10_000
    )
