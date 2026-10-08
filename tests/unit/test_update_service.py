"""Starting an Update from the app (sp-rtk-base#240), as the host sees it.

The app writes the ``requested`` status, then the request file, into the
update directory, after its guards pass. The host (the updater) answers
in ``status.json``; these tests play the host by writing it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sp_rtk_base import __version__ as app_version
from sp_rtk_base.services.update_service import (
    UpdateRefusedError,
    UpdateService,
)
from sp_rtk_base.update.state import (
    REASON_DIDNT_START,
    UpdateFiles,
    UpdateStatus,
    Versions,
)

RUNNING = Versions(app="0.9.0", relay="4.1.0")
TARGET = Versions(app="0.10.1", relay="4.2.0")
T0 = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Guards:
    """What the guards report: a Survey-in running, a Console link connected."""

    def __init__(self) -> None:
        self.survey = False
        self.console = False

    async def survey_running(self) -> bool:
        return self.survey

    def console_connected(self) -> bool:
        return self.console


@pytest.fixture()
def files(tmp_path: Path) -> UpdateFiles:
    return UpdateFiles(tmp_path / "update")


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def guards() -> Guards:
    return Guards()


@pytest.fixture()
def service(files: UpdateFiles, clock: Clock, guards: Guards) -> UpdateService:
    svc = UpdateService(files, running=RUNNING, clock=clock)
    svc.set_survey_check(guards.survey_running)
    svc.set_console_check(guards.console_connected)
    return svc


def _host_writes(files: UpdateFiles, phase: str, at: datetime, **extra: object) -> None:
    files.write_status(
        UpdateStatus.model_validate(
            {"phase": phase, "from": RUNNING, "to": TARGET, "updated_at": at, **extra}
        )
    )


@pytest.mark.asyncio
class TestRequest:
    async def test_writes_the_requested_status_and_the_request(
        self, service: UpdateService, files: UpdateFiles
    ) -> None:
        await service.request(TARGET)

        status = json.loads(files.status_path.read_text())
        assert status["phase"] == "requested"
        assert status["from"] == {"app": "0.9.0", "relay": "4.1.0"}
        assert status["to"] == {"app": "0.10.1", "relay": "4.2.0"}
        request = json.loads(files.request_path.read_text())
        assert (request["app"], request["relay"]) == ("0.10.1", "4.2.0")

    async def test_the_status_is_written_before_the_request(
        self,
        service: UpdateService,
        files: UpdateFiles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The host may answer the moment the request appears; ``requested``
        must not overwrite its answer."""
        seen_at_request: list[bool] = []
        write_request = files.write_request

        def _spy(request: object) -> None:
            seen_at_request.append(files.status_path.exists())
            write_request(request)  # type: ignore[arg-type]

        monkeypatch.setattr(files, "write_request", _spy)

        await service.request(TARGET)

        assert seen_at_request == [True]

    async def test_refused_while_a_survey_in_runs(
        self, service: UpdateService, files: UpdateFiles, guards: Guards
    ) -> None:
        guards.survey = True

        with pytest.raises(UpdateRefusedError) as refused:
            await service.request(TARGET)

        assert refused.value.code == "survey_running"
        assert refused.value.message == (
            "A Survey-in is running. Update once it has finished."
        )
        assert not files.request_path.exists()
        assert not files.status_path.exists()

    async def test_refused_while_a_console_link_is_connected(
        self, service: UpdateService, files: UpdateFiles, guards: Guards
    ) -> None:
        guards.console = True

        with pytest.raises(UpdateRefusedError) as refused:
            await service.request(TARGET)

        assert refused.value.code == "console_connected"
        assert refused.value.message == (
            "A Console link is connected. Disconnect it to update."
        )
        assert not files.request_path.exists()

    async def test_refused_while_an_update_runs(
        self, service: UpdateService, files: UpdateFiles
    ) -> None:
        _host_writes(files, "installing", T0)

        with pytest.raises(UpdateRefusedError) as refused:
            await service.request(TARGET)

        assert refused.value.code == "updating"
        assert not files.request_path.exists()

    async def test_a_survey_that_cant_be_read_doesnt_block(
        self, service: UpdateService, files: UpdateFiles
    ) -> None:
        async def _unreadable() -> bool:
            raise RuntimeError("Device not connected")

        service.set_survey_check(_unreadable)

        await service.request(TARGET)

        assert files.request_path.exists()

    async def test_refusal_says_why_without_writing(
        self, service: UpdateService, files: UpdateFiles, guards: Guards
    ) -> None:
        assert await service.refusal() is None
        guards.console = True

        refusal = await service.refusal()

        assert refusal is not None
        assert refusal.code == "console_connected"
        assert not files.status_path.exists()

    async def test_after_a_finished_update_another_can_start(
        self, service: UpdateService, files: UpdateFiles
    ) -> None:
        _host_writes(files, "done", T0)

        await service.request(TARGET)

        assert files.request_path.exists()


class TestUpdating:
    @pytest.mark.parametrize(
        "phase", ["requested", "resolving", "installing", "restarting", "verifying"]
    )
    def test_every_phase_before_the_end_is_updating(
        self, service: UpdateService, files: UpdateFiles, phase: str
    ) -> None:
        _host_writes(files, phase, T0)

        assert service.updating() is True
        status = service.status()
        assert status is not None
        assert status.phase == phase

    @pytest.mark.parametrize("phase", ["done", "failed"])
    def test_a_finished_update_is_not_updating(
        self, service: UpdateService, files: UpdateFiles, phase: str
    ) -> None:
        _host_writes(files, phase, T0)

        assert service.updating() is False

    def test_no_update_yet(self, service: UpdateService) -> None:
        assert service.status() is None
        assert service.updating() is False

    @pytest.mark.asyncio
    async def test_resolving_names_the_requested_target(
        self, service: UpdateService, files: UpdateFiles
    ) -> None:
        """The host writes ``resolving`` before it names the target."""
        await service.request(TARGET)
        files.write_status(UpdateStatus(phase="resolving", from_=RUNNING))

        status = service.status()

        assert status is not None
        assert status.to == TARGET

    @pytest.mark.asyncio
    async def test_requests_from_the_versions_running_here(
        self, files: UpdateFiles
    ) -> None:
        await UpdateService(files).request(TARGET)

        status = files.read_status()
        assert status is not None
        assert status.from_ is not None
        assert status.from_.app == app_version


@pytest.mark.asyncio
class TestDidntStart:
    async def test_no_phase_after_requested_within_30_s(
        self, service: UpdateService, files: UpdateFiles, clock: Clock
    ) -> None:
        await service.request(TARGET)
        clock.now = T0 + timedelta(seconds=31)

        status = service.status()

        assert status is not None
        assert status.phase == "failed"
        assert status.reason == REASON_DIDNT_START
        assert status.to == TARGET
        assert service.updating() is False
        # Taken back: enabling the path unit later never starts it.
        assert not files.request_path.exists()
        on_disk = files.read_status()
        assert on_disk is not None
        assert on_disk.reason == REASON_DIDNT_START
        assert on_disk.finished_at == T0 + timedelta(seconds=31)

    async def test_still_waiting_at_29_s(
        self, service: UpdateService, files: UpdateFiles, clock: Clock
    ) -> None:
        await service.request(TARGET)
        clock.now = T0 + timedelta(seconds=29)

        assert service.updating() is True
        assert files.request_path.exists()

    async def test_a_host_that_answered_is_never_timed_out(
        self, service: UpdateService, files: UpdateFiles, clock: Clock
    ) -> None:
        await service.request(TARGET)
        files.take_request()
        _host_writes(files, "installing", T0 + timedelta(seconds=5))
        clock.now = T0 + timedelta(minutes=5)

        status = service.status()

        assert status is not None
        assert status.phase == "installing"

    async def test_a_request_the_host_took_at_the_last_moment_is_left_alone(
        self, service: UpdateService, files: UpdateFiles, clock: Clock
    ) -> None:
        """The host took the request but hasn't written a phase yet."""
        await service.request(TARGET)
        files.take_request()
        clock.now = T0 + timedelta(seconds=31)

        status = service.status()

        assert status is not None
        assert status.phase == "requested"


class TestAcknowledge:
    def test_a_finished_update_is_unacknowledged_until_dismissed(
        self, service: UpdateService, files: UpdateFiles
    ) -> None:
        _host_writes(files, "done", T0)
        status = service.status()
        assert status is not None
        assert service.acknowledged(status) is False

        service.acknowledge(status)

        assert service.acknowledged(status) is True
        # Kept beside the update state, so it outlives a restart.
        again = UpdateService(UpdateFiles(files.directory), running=RUNNING)
        assert again.acknowledged(status) is True

    def test_the_next_update_is_unacknowledged_again(
        self, service: UpdateService, files: UpdateFiles
    ) -> None:
        _host_writes(files, "done", T0)
        first = service.status()
        assert first is not None
        service.acknowledge(first)

        _host_writes(files, "done", T0 + timedelta(days=1))
        second = service.status()

        assert second is not None
        assert service.acknowledged(second) is False
