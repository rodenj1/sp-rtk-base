"""End-to-end tests: verifying a Correction source on the Survey page (#194).

A scripted fake NTRIP caster runs in the test process on localhost, where
the e2e server (a local subprocess) can reach it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.fixtures.scripted_ntrip_caster import FakeCaster, Script
from tests.unit.msm_frames import other_frame

ICY = b"ICY 200 OK\r\n"
REF_1005 = other_frame(1005).data


@pytest.fixture
def caster() -> Iterator[FakeCaster]:
    fake = FakeCaster()
    yield fake
    fake.close()


def _save_source(page: Page, api_base_url: str, caster: FakeCaster) -> None:
    created = page.request.post(
        f"{api_base_url}/api/correction-sources",
        data={
            "name": "local",
            "caster": "127.0.0.1",
            "port": caster.port,
            "mountpoint": "MP1",
            "username": "rover",
            "password": "roverpw",
            "version": "1.0",
        },
    )
    assert created.ok, created.text()


def _open_corrected(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)
    page.get_by_role("button", name="Corrected (cm-level)").click()
    page.get_by_test_id("correction-source-select").click()
    page.get_by_role("option", name="local", exact=True).click()


@pytest.mark.e2e
def test_a_green_shows_every_stage_passed_and_its_countdown(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    clean_config: None,
    caster: FakeCaster,
) -> None:
    caster.scripts.append(Script(reply=ICY, body=[REF_1005]))
    _save_source(page, api_base_url, caster)
    _open_corrected(page, base_url)

    page.get_by_role("button", name="Verify").first.click()

    for stage in ("connect", "caster", "auth", "mountpoint", "data"):
        expect(page.get_by_test_id(f"stage-{stage}").last).to_have_attribute(
            "data-status", "passed", timeout=20_000
        )
    expect(page.locator("text=Green for").first).to_be_visible()


@pytest.mark.e2e
def test_a_red_names_the_failing_stage_and_skips_the_rest(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    clean_config: None,
    caster: FakeCaster,
) -> None:
    caster.scripts.append(
        Script(reply=b"HTTP/1.0 401 Unauthorized\r\n\r\n", hold=False)
    )
    _save_source(page, api_base_url, caster)
    _open_corrected(page, base_url)

    page.get_by_role("button", name="Verify").first.click()

    expect(page.get_by_test_id("stage-auth").last).to_have_attribute(
        "data-status", "failed", timeout=20_000
    )
    expect(page.get_by_test_id("stage-caster").last).to_have_attribute(
        "data-status", "passed"
    )
    expect(page.get_by_test_id("stage-data").last).to_have_attribute(
        "data-status", "skipped"
    )
    expect(page.locator("text=Red at auth").first).to_be_visible()


@pytest.mark.e2e
def test_editing_the_form_after_a_green_voids_it(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    clean_config: None,
    caster: FakeCaster,
) -> None:
    caster.scripts.append(Script(reply=ICY, body=[REF_1005]))
    _save_source(page, api_base_url, caster)
    _open_corrected(page, base_url)

    page.get_by_role("button", name="Edit Correction source").click()
    dialog = page.locator(".q-dialog")
    dialog.get_by_role("button", name="Verify").click()
    expect(dialog.locator("text=Green for").first).to_be_visible(timeout=20_000)

    dialog.get_by_label("Port", exact=True).fill("2102")

    expect(
        dialog.locator("text=The form changed since it was verified").first
    ).to_be_visible(timeout=5_000)
