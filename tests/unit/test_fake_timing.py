"""A timing the e2e tests may shorten, honoured only on a fake host
(rtk_development#189)."""

from __future__ import annotations

import pytest

from sp_rtk_base.fake_timing import fake_timing

GATE = "SP_RTK_BASE_TEST_FAKE_HOST"
TIMING = "SP_RTK_BASE_TEST_FAKE_TIMING_S"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (GATE, TIMING):
        monkeypatch.delenv(var, raising=False)


def _gps_timing() -> float:
    """As the fake GPS gates it: only when the gate is exactly "1"."""
    return fake_timing(TIMING, 1.0, gate_env=GATE, gate_value="1")


def test_the_fake_shortens_the_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GATE, "1")
    monkeypatch.setenv(TIMING, "0.1")
    assert _gps_timing() == 0.1


def test_without_the_fake_the_default_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TIMING, "0.1")
    assert _gps_timing() == 1.0


def test_a_gate_with_another_value_keeps_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GATE, "true")
    monkeypatch.setenv(TIMING, "0.1")
    assert _gps_timing() == 1.0


def test_any_gate_value_will_do_when_none_is_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GATE, "/tmp/links.json")
    monkeypatch.setenv(TIMING, "0.5")
    assert fake_timing(TIMING, 2.0, gate_env=GATE) == 0.5


def test_the_fake_alone_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GATE, "1")
    assert _gps_timing() == 1.0


@pytest.mark.parametrize("value", ["soon", "0", "-1", "nan"])
def test_a_bad_override_is_logged_and_keeps_the_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    monkeypatch.setenv(GATE, "1")
    monkeypatch.setenv(TIMING, value)
    assert _gps_timing() == 1.0
    assert TIMING in caplog.text


def test_zero_stands_where_it_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A settling time of 0 s means "no settling", not a spinning loop."""
    monkeypatch.setenv(GATE, "1")
    monkeypatch.setenv(TIMING, "0")
    assert (
        fake_timing(TIMING, 30.0, gate_env=GATE, gate_value="1", allow_zero=True) == 0.0
    )


def test_a_negative_override_is_refused_even_where_zero_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GATE, "1")
    monkeypatch.setenv(TIMING, "-1")
    assert (
        fake_timing(TIMING, 30.0, gate_env=GATE, gate_value="1", allow_zero=True)
        == 30.0
    )
