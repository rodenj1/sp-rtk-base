"""The Network page's poll intervals: 2 s / 5 s, shortened only on the fake
Network link host (rtk_development#192)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sp_rtk_base.ui.pages.network import network_poll_intervals

STATUS = "SP_RTK_BASE_FAKE_NETWORK_STATUS_POLL_S"
SAVED = "SP_RTK_BASE_FAKE_NETWORK_SAVED_POLL_S"
FAKE_HOST = "SP_RTK_BASE_FAKE_NETWORK_LINKS"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (STATUS, SAVED, FAKE_HOST):
        monkeypatch.delenv(var, raising=False)


def test_production_polls_every_2_and_5_seconds() -> None:
    assert network_poll_intervals() == (2.0, 5.0)


def test_the_fake_host_shortens_both_polls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(FAKE_HOST, str(tmp_path / "links.json"))
    monkeypatch.setenv(STATUS, "0.5")
    monkeypatch.setenv(SAVED, "1.0")
    assert network_poll_intervals() == (0.5, 1.0)


def test_the_overrides_are_ignored_without_the_fake_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STATUS, "0.5")
    monkeypatch.setenv(SAVED, "1.0")
    assert network_poll_intervals() == (2.0, 5.0)


def test_the_fake_host_alone_keeps_the_production_polls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(FAKE_HOST, str(tmp_path / "links.json"))
    assert network_poll_intervals() == (2.0, 5.0)


@pytest.mark.parametrize("value", ["soon", "0", "-1"])
def test_a_bad_override_keeps_the_production_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.setenv(FAKE_HOST, str(tmp_path / "links.json"))
    monkeypatch.setenv(STATUS, value)
    monkeypatch.setenv(SAVED, "1.0")
    assert network_poll_intervals() == (2.0, 1.0)
