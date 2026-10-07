"""The update check: when it runs, and what it keeps when PyPI fails."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from sp_rtk_base.services.update_check import UpdateCheckService
from sp_rtk_base.update.release import NewerNeedsPython
from tests.fixtures.fake_pypi import APP_INDEX, FakePyPI

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 10, 7, 9, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def pypi() -> FakePyPI:
    return FakePyPI()


@pytest.fixture()
def clock() -> Clock:
    return Clock()


def _service(
    pypi: FakePyPI, clock: Clock, *, running_app: str = "0.9.0"
) -> UpdateCheckService:
    return UpdateCheckService(
        pypi,
        running_app=running_app,
        running_relay="4.1.0",
        python=(3, 11),
        clock=clock,
    )


class TestCheckNow:
    async def test_before_any_check_nothing_is_known(
        self, pypi: FakePyPI, clock: Clock
    ) -> None:
        status = _service(pypi, clock).status

        assert status.last_good is None
        assert not status.checking
        assert not status.last_check_failed

    async def test_an_available_update(self, pypi: FakePyPI, clock: Clock) -> None:
        pypi.publish_relay("4.2.0")
        pypi.publish_app("0.10.0", relay_pin="<5,>=4.2.0")

        status = await _service(pypi, clock).check_now()

        assert status.last_good is not None
        assert status.last_good.available
        assert status.last_good.running_app == "0.9.0"
        assert status.last_good.running_relay == "4.1.0"
        assert status.last_good.target.app == "0.10.0"
        assert status.last_good.target.relay == "4.2.0"
        assert status.last_good.checked_at == T0
        assert not status.last_check_failed

    async def test_up_to_date(self, pypi: FakePyPI, clock: Clock) -> None:
        status = await _service(pypi, clock).check_now()

        assert status.last_good is not None
        assert not status.last_good.available

    async def test_a_running_version_newer_than_pypi_is_not_an_update(
        self, pypi: FakePyPI, clock: Clock
    ) -> None:
        status = await _service(pypi, clock, running_app="0.10.0.dev1").check_now()

        assert status.last_good is not None
        assert not status.last_good.available

    async def test_the_python_note_comes_through(
        self, pypi: FakePyPI, clock: Clock
    ) -> None:
        pypi.publish_app("0.11.0", requires_python=">=3.12")

        status = await _service(pypi, clock).check_now()

        assert status.last_good is not None
        assert status.last_good.target.newer_needs_python == NewerNeedsPython(
            version="0.11.0", python="3.12"
        )


class TestFailedCheck:
    async def test_a_failed_check_keeps_the_last_good_result(
        self, pypi: FakePyPI, clock: Clock
    ) -> None:
        pypi.publish_app("0.10.0")
        service = _service(pypi, clock)
        await service.check_now()
        clock.now = T0 + timedelta(hours=24)
        pypi.down.add(APP_INDEX)

        status = await service.check_now()

        assert status.last_check_failed
        assert status.error
        assert status.last_good is not None
        assert status.last_good.target.app == "0.10.0"
        assert status.last_good.checked_at == T0

    async def test_a_failed_first_check_has_no_result(
        self, pypi: FakePyPI, clock: Clock
    ) -> None:
        pypi.down.add(APP_INDEX)

        status = await _service(pypi, clock).check_now()

        assert status.last_check_failed
        assert status.last_good is None

    async def test_a_good_check_clears_the_failure(
        self, pypi: FakePyPI, clock: Clock
    ) -> None:
        service = _service(pypi, clock)
        pypi.down.add(APP_INDEX)
        await service.check_now()
        pypi.down.clear()

        status = await service.check_now()

        assert not status.last_check_failed
        assert status.error is None

    async def test_a_bug_in_the_check_counts_as_a_failed_check(
        self, clock: Clock
    ) -> None:
        def broken(_url: str) -> bytes:
            raise AssertionError("bug")

        service = UpdateCheckService(
            broken, running_app="0.9.0", running_relay="4.1.0", python=(3, 11)
        )

        status = await service.check_now()

        assert status.last_check_failed


class BlockingPyPI(FakePyPI):
    """PyPI that holds every answer until the test lets it go."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, url: str) -> bytes:
        if url == APP_INDEX:
            self.calls += 1
        self.release.wait(5)
        return super().__call__(url)


class TestOneCheckAtATime:
    async def test_checking_shows_while_a_check_runs(self, clock: Clock) -> None:
        pypi = BlockingPyPI()
        service = _service(pypi, clock)

        first = asyncio.create_task(service.check_now())
        await asyncio.sleep(0.05)
        assert service.status.checking
        pypi.release.set()
        await first

        assert not service.status.checking

    async def test_check_now_during_a_check_joins_it(self, clock: Clock) -> None:
        pypi = BlockingPyPI()
        service = _service(pypi, clock)

        first = asyncio.create_task(service.check_now())
        await asyncio.sleep(0.05)
        second = asyncio.create_task(service.check_now())
        await asyncio.sleep(0.05)
        pypi.release.set()
        a, b = await asyncio.gather(first, second)

        assert pypi.calls == 1
        assert a == b


class TestSchedule:
    async def test_checks_at_startup_then_every_interval(
        self, pypi: FakePyPI, clock: Clock
    ) -> None:
        service = UpdateCheckService(
            pypi,
            running_app="0.9.0",
            running_relay="4.1.0",
            python=(3, 11),
            clock=clock,
            interval_s=0.05,
        )

        service.start()
        service.start()  # idempotent
        await asyncio.sleep(0.02)
        assert pypi.fetched.count(APP_INDEX) == 1
        await asyncio.sleep(0.1)
        await service.stop()

        assert pypi.fetched.count(APP_INDEX) >= 2
        assert service.status.last_good is not None

    async def test_stop_without_start(self, pypi: FakePyPI, clock: Clock) -> None:
        await _service(pypi, clock).stop()

    async def test_default_interval_is_a_day(self, pypi: FakePyPI) -> None:
        assert UpdateCheckService(pypi).interval_s == 24 * 60 * 60
