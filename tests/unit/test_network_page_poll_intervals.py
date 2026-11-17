"""The Network page's poll intervals: 2 s / 5 s, shortened only on the fake
Network link host (rtk_development#192)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sp_rtk_base.services.network_link.reader import FAKE_NETWORK_LINKS_ENV
from sp_rtk_base.ui.pages.network import (
    SAVED_POLL_ENV,
    STATUS_POLL_ENV,
    NetworkPollIntervals,
    network_poll_intervals,
)

PRODUCTION = NetworkPollIntervals(status_s=2.0, saved_s=5.0)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (STATUS_POLL_ENV, SAVED_POLL_ENV, FAKE_NETWORK_LINKS_ENV):
        monkeypatch.delenv(var, raising=False)


def test_production_polls_every_2_and_5_seconds() -> None:
    assert network_poll_intervals() == PRODUCTION


def test_the_fake_host_shortens_both_polls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(FAKE_NETWORK_LINKS_ENV, str(tmp_path / "links.json"))
    monkeypatch.setenv(STATUS_POLL_ENV, "0.5")
    monkeypatch.setenv(SAVED_POLL_ENV, "1.0")
    assert network_poll_intervals() == NetworkPollIntervals(status_s=0.5, saved_s=1.0)


def test_the_overrides_are_ignored_without_the_fake_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STATUS_POLL_ENV, "0.5")
    monkeypatch.setenv(SAVED_POLL_ENV, "1.0")
    assert network_poll_intervals() == PRODUCTION


def test_the_fake_host_alone_keeps_the_production_polls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(FAKE_NETWORK_LINKS_ENV, str(tmp_path / "links.json"))
    assert network_poll_intervals() == PRODUCTION


@pytest.mark.parametrize("value", ["soon", "0", "-1"])
def test_a_bad_override_keeps_the_production_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.setenv(FAKE_NETWORK_LINKS_ENV, str(tmp_path / "links.json"))
    monkeypatch.setenv(STATUS_POLL_ENV, value)
    monkeypatch.setenv(SAVED_POLL_ENV, "1.0")
    assert network_poll_intervals() == NetworkPollIntervals(status_s=2.0, saved_s=1.0)
