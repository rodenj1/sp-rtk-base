"""End-to-end tests: a Corrected survey-in's warning and failure messages (#196).

The e2e server shortens the stall warning (60 s) to 2 s and the stall abort
(10 min) to 15 s. Corrections come from a scripted fake NTRIP caster in the
test process, on localhost.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.fixtures.scripted_ntrip_caster import FakeCaster, Script

ICY = b"ICY 200 OK\r\n"


@pytest.fixture
def caster() -> Iterator[FakeCaster]:
    fake = FakeCaster()
    yield fake
    fake.close()


def _start_corrected(page: Page, base_url: str, api_base_url: str, port: int) -> None:
    created = page.request.post(
        f"{api_base_url}/api/correction-sources",
        data={
            "name": "local",
            "caster": "127.0.0.1",
            "port": port,
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


@pytest.mark.e2e
def test_a_survey_without_corrections_warns_then_aborts(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
    clean_config: None,
    caster: FakeCaster,
) -> None:
    # Accepted, then dropped; every reconnect is refused.
    caster.scripts += [Script(reply=ICY, hold=False)] + [
        Script(reply=b"HTTP/1.0 401 Unauthorized\r\n\r\n", hold=False)
        for _ in range(20)
    ]

    _start_corrected(page, base_url, api_base_url, caster.port)

    expect(
        page.locator("text=/No RTK Fixed for .*no corrections are arriving/").first
    ).to_be_visible(timeout=10_000)
    expect(
        page.locator("text=/The survey aborts in \\d+s unless RTK Fixed returns/").first
    ).to_be_visible()
    expect(
        page.locator(
            "text=/Survey aborted: RTK Fixed never returned: no corrections "
            "arrived \\(the source's last error: .+\\)/"
        ).first
    ).to_be_visible(timeout=25_000)
    expect(
        page.locator(
            "text=/Nothing was committed; the receiver is in rover mode/"
        ).first
    ).to_be_visible()
    expect(page.get_by_role("button", name="Start Survey-In")).to_be_visible()


@pytest.mark.e2e
def test_a_refused_start_names_the_failing_stage(
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

    _start_corrected(page, base_url, api_base_url, caster.port)

    expect(
        page.locator(
            "text=Couldn't start: the Correction source failed at auth — "
            "the caster rejected the username or password."
        ).first
    ).to_be_visible(timeout=10_000)
    expect(page.get_by_role("button", name="Start Survey-In")).to_be_visible()
