"""End-to-end tests for a Survey-in averaged by the station (issue #191).

The fake receiver is connected without a Receiver survey-in (sentinel port
``FAKE-NO-SVIN``), so a plain Survey-in started on the Survey page is
averaged by the station itself, which commits the fixed base on its own.

The completion test runs a real 60 s survey (the shortest allowed), so it
takes about a minute.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from sp_rtk_base.services.drivers.fake import FAKE_NO_SURVEY_IN_PORT


@pytest.fixture()
def connected_gps_without_survey_in(api_base_url: str) -> Iterator[None]:
    """Connect the server to the fake receiver without a Receiver survey-in."""
    try:
        httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
    except Exception:
        pass
    resp = httpx.post(
        f"{api_base_url}/api/device/connect",
        json={"vendor": "fake", "port": FAKE_NO_SURVEY_IN_PORT, "baud_rate": 115200},
        timeout=10.0,
    )
    if resp.status_code not in (200, 409):
        raise RuntimeError(
            f"Could not connect the fake: {resp.status_code} {resp.text}"
        )
    try:
        yield
    finally:
        try:
            httpx.post(f"{api_base_url}/api/device/disconnect", timeout=5.0)
        except Exception:
            pass


def _start_survey(page: Page, base_url: str, min_duration_s: int) -> None:
    page.goto(f"{base_url}/survey")
    expect(page.locator("text=Survey-In").first).to_be_visible(timeout=15_000)
    page.get_by_label("Min Duration (seconds)").fill(str(min_duration_s))
    page.get_by_role("button", name="Start Survey-In").click()
    expect(page.locator("text=Start Survey-In?").first).to_be_visible(timeout=5_000)
    page.get_by_role("button", name="Start Survey", exact=True).click()
    expect(page.locator("text=Survey-In Progress").first).to_be_visible(timeout=5_000)


def _survey(page: Page, api_base_url: str) -> dict[str, Any]:
    response = page.request.get(f"{api_base_url}/api/device/survey-in")
    assert response.ok, response.text()
    payload: dict[str, Any] = response.json()
    return payload


def _base_mode(page: Page, api_base_url: str) -> str:
    response = page.request.get(f"{api_base_url}/api/device/base-config")
    assert response.ok, response.text()
    mode: str = response.json()["mode"]
    return mode


@pytest.mark.e2e
def test_a_station_averaged_survey_completes_and_commits_the_fixed_base(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps_without_survey_in: None,
) -> None:
    _start_survey(page, base_url, min_duration_s=60)

    expect(page.locator("text=Averaged by the station").first).to_be_visible(
        timeout=10_000
    )
    expect(page.locator("text=Active — collecting").first).to_be_visible(timeout=10_000)
    # The station counts the minimum duration in observation time: ~60 s
    expect(
        page.locator("text=Survey complete — position committed!").first
    ).to_be_visible(timeout=100_000)

    progress = _survey(page, api_base_url)
    assert progress["outcome"] == "completed", progress
    assert progress["averaged_by"] == "application"
    assert progress["duration_seconds"] >= 60
    assert _base_mode(page, api_base_url) == "fixed"


@pytest.mark.e2e
def test_cancelling_a_station_averaged_survey_leaves_the_receiver_in_rover_mode(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps_without_survey_in: None,
) -> None:
    _start_survey(page, base_url, min_duration_s=600)
    expect(page.locator("text=Averaged by the station").first).to_be_visible(
        timeout=10_000
    )

    page.get_by_role("button", name="Cancel Survey").first.click()
    expect(page.locator("text=Cancel Survey-In?").first).to_be_visible(timeout=5_000)
    page.get_by_role("button", name="Cancel Survey", exact=True).last.click()

    expect(page.locator("text=Cancelled by operator").first).to_be_visible(
        timeout=10_000
    )
    expect(page.get_by_role("button", name="Start Survey-In")).to_be_visible()
    assert _survey(page, api_base_url)["outcome"] == "cancelled"
    assert _base_mode(page, api_base_url) == "disabled"


@pytest.mark.e2e
def test_a_receiver_with_its_own_survey_in_averages_it_itself(
    page: Page,
    base_url: str,
    api_base_url: str,
    connected_gps: None,
) -> None:
    _start_survey(page, base_url, min_duration_s=60)

    expect(page.locator("text=Averaged by the receiver").first).to_be_visible(
        timeout=10_000
    )
    assert _survey(page, api_base_url)["averaged_by"] == "receiver"
