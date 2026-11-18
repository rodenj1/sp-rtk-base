"""``HealthCheck`` on its own, with a fake clock: what counts as healthy
after the hold (ADR 0005)."""

from __future__ import annotations

import json
from collections.abc import Callable

from sp_rtk_base.update.health import HEALTH_GRACE_S, HealthCheck
from sp_rtk_base.update.state import Versions

EXPECTED = Versions(app="0.11.0", relay="0.6.0")
GOOD = json.dumps({"version": "0.11.0", "relay_version": "0.6.0"}).encode()
WRONG = json.dumps({"version": "0.11.0", "relay_version": "0.5.0"}).encode()
TIMEOUT = TimeoutError("timed out")
TIMEOUT_S = 90.0
HOLD_S = 30.0


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def check(answers: Callable[[float], bytes | Exception]) -> tuple[str | None, float]:
    """Runs a ``HealthCheck`` whose app answers ``answers(now)`` to each
    request; returns its verdict and when it gave it."""
    time = FakeTime()

    def fetch(url: str) -> bytes:
        answer = answers(time.now)
        if isinstance(answer, Exception):
            raise answer
        return answer

    health = HealthCheck(
        url="http://127.0.0.1:8080/api/health",
        timeout_s=TIMEOUT_S,
        hold_s=HOLD_S,
        fetch=fetch,
        restarts=lambda: 0,
        sleep=time.sleep,
        clock=time.clock,
    )
    return health(EXPECTED), time.now


def test_answering_throughout_is_healthy() -> None:
    assert check(lambda now: GOOD) == (None, HOLD_S)


def test_one_late_answer_after_the_hold_is_healthy() -> None:
    answers = iter([GOOD, TIMEOUT])

    verdict, _ = check(lambda now: next(answers, GOOD))

    assert verdict is None


def test_wrong_versions_after_the_hold_then_right_ones_is_healthy() -> None:
    answers = iter([GOOD, WRONG, WRONG])

    verdict, _ = check(lambda now: next(answers, GOOD))

    assert verdict is None


def test_no_answer_after_the_hold_fails_once_the_grace_period_is_over() -> None:
    verdict, when = check(lambda now: GOOD if now < HOLD_S else TIMEOUT)

    assert verdict is not None
    assert verdict.startswith("SP-Base 0.11.0 started, then stopped answering: ")
    assert "no answer" in verdict
    assert when >= HOLD_S + HEALTH_GRACE_S


def test_wrong_versions_throughout_the_grace_period_fails() -> None:
    verdict, when = check(lambda now: GOOD if now < HOLD_S else WRONG)

    assert verdict == (
        "SP-Base 0.11.0 started, then stopped answering: "
        "it reports SP-Base 0.11.0, Relay 0.5.0"
    )
    assert when >= HOLD_S + HEALTH_GRACE_S


def test_the_grace_period_is_its_own() -> None:
    time = FakeTime()
    health = HealthCheck(
        fetch=lambda url: GOOD if time.now < HOLD_S else b"{}",
        hold_s=HOLD_S,
        grace_s=2.0,
        restarts=lambda: 0,
        sleep=time.sleep,
        clock=time.clock,
    )

    assert health(EXPECTED) is not None
    assert HOLD_S + 2.0 <= time.now < HOLD_S + 2.0 + 1.0 + 0.001
