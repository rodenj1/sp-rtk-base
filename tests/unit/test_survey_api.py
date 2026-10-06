"""Tests for running a Survey-in through the survey API.

Seam under test: the survey HTTP API (``POST /api/device/configure/survey-in``,
``GET /api/device/survey-in``, ``POST /api/device/cancel-survey-in``) via
TestClient, over a real DeviceService, a real SurveyService and the fake
driver. The SurveyService takes an injected clock and sleep, so a survey
that samples once a second runs instantly.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.device_models import (
    BaseMode,
    DeviceConnectionState,
    FixedBaseConfig,
    SurveyInProgress,
    SurveyPosition,
)
from sp_rtk_base.services import get_device_service, get_survey_service
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.fake import FAKE_NO_SURVEY_IN_PORT, FakeGpsDriver
from sp_rtk_base.services.geodesy import ecef_to_llh, llh_to_ecef
from sp_rtk_base.services.survey_service import SurveyService

SURVEY = "/api/device/survey-in"
START = "/api/device/configure/survey-in"
CANCEL = "/api/device/cancel-survey-in"


class FakeClock:
    """A monotonic clock that only moves when the survey sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)  # let the API serve requests in between


class RecordingFake(FakeGpsDriver):
    """The fake driver, recording the calls that commit a fixed base."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def configure_fixed_base(self, config: Any) -> None:
        self.calls.append("configure_fixed_base")
        super().configure_fixed_base(config)

    def save_to_flash(self) -> None:
        self.calls.append("save_to_flash")
        super().save_to_flash()

    def disable_base_mode(self) -> None:
        self.calls.append("disable_base_mode")
        super().disable_base_mode()


@pytest.fixture
def fake() -> RecordingFake:
    driver = RecordingFake()
    driver.connect(FAKE_NO_SURVEY_IN_PORT)
    return driver


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(fake: RecordingFake, clock: FakeClock) -> Iterator[TestClient]:
    device = DeviceService()
    device.set_driver(fake)
    device._state = DeviceConnectionState.CONNECTED  # pyright: ignore[reportPrivateUsage]
    survey = SurveyService(device, clock=clock, sleep=clock.sleep)
    app = create_api_app()
    app.dependency_overrides[get_device_service] = lambda: device
    app.dependency_overrides[get_survey_service] = lambda: survey
    with TestClient(app) as test_client:
        yield test_client


def _wait_for_outcome(client: TestClient, outcome: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5.0
    while True:
        progress: dict[str, Any] = client.get(SURVEY).json()
        if progress.get("outcome") == outcome:
            return progress
        assert time.monotonic() < deadline, (
            f"survey never reached {outcome}: {progress}"
        )
        time.sleep(0.01)


def _fix(x: float, y: float, z: float, accuracy_m: float = 0.5) -> SurveyPosition:
    return SurveyPosition(
        ecef_x_m=x,
        ecef_y_m=y,
        ecef_z_m=z,
        accuracy_3d_m=accuracy_m,
        rtk_status="none",
        fix_ok=True,
        correction_age_s=None,
    )


TRUE_ECEF = llh_to_ecef(32.7329015, -117.2362788, 27.94)


class TestApplicationAveraging:
    def test_a_plain_survey_completes_and_commits_the_mean_as_the_fixed_base(
        self, client: TestClient, fake: RecordingFake
    ) -> None:
        x, y, z = TRUE_ECEF
        # 60 fixes 0.2 m either side of the true point along X: mean = the point
        fake.script_survey_positions(
            _fix(x + (0.2 if i % 2 else -0.2), y, z) for i in range(60)
        )

        response = client.post(
            START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 1000}
        )
        assert response.status_code == 200
        progress = _wait_for_outcome(client, "completed")

        assert progress["averaged_by"] == "application"
        assert progress["valid"] is True
        assert progress["active"] is False
        assert progress["duration_seconds"] == 60
        assert progress["observations"] == 60
        # accuracy = max(mean 3D accuracy 0.5 m, 3D std 0.2 m), no sqrt(N)
        assert progress["mean_accuracy_mm"] == pytest.approx(500.0)
        lat, lon, alt = ecef_to_llh(x, y, z)
        assert progress["latitude"] == pytest.approx(lat, abs=1e-9)
        assert progress["longitude"] == pytest.approx(lon, abs=1e-9)
        assert progress["altitude_m"] == pytest.approx(alt, abs=1e-4)

        base = fake.get_base_config()
        assert base.mode is BaseMode.FIXED
        assert math.isclose(base.latitude, lat, abs_tol=1e-9)
        assert base.accuracy_mm == 500
        assert fake.calls[-1] == "configure_fixed_base"
        assert "save_to_flash" not in fake.calls  # it persists itself (#221)

    def test_a_fix_drop_pauses_the_survey_without_losing_observations(
        self, client: TestClient, fake: RecordingFake
    ) -> None:
        x, y, z = TRUE_ECEF
        no_fix = SurveyPosition(
            ecef_x_m=0.0,
            ecef_y_m=0.0,
            ecef_z_m=0.0,
            accuracy_3d_m=99.0,
            rtk_status="none",
            fix_ok=False,
            correction_age_s=None,
        )
        # 30 fixes, a 20 s drop, then 30 more fixes
        fake.script_survey_positions(
            [_fix(x, y, z)] * 30 + [no_fix] * 20 + [_fix(x, y, z)] * 30
        )

        client.post(START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 1000})
        progress = _wait_for_outcome(client, "completed")

        assert progress["duration_seconds"] == 60  # observation time, not 80 s
        assert progress["observations"] == 60
        # the drop's bogus 0,0,0 positions were never averaged in
        assert progress["altitude_m"] == pytest.approx(27.94, abs=1e-4)

    def test_the_accuracy_is_the_3d_spread_when_it_is_larger(
        self, client: TestClient, fake: RecordingFake
    ) -> None:
        x, y, z = TRUE_ECEF
        # 2 m either side along X: std-dev 2 m > mean accuracy 0.5 m. More
        # than the survey can use before its hard cap, so it never runs dry.
        fake.script_survey_positions(
            _fix(x + (2.0 if i % 2 else -2.0), y, z) for i in range(4000)
        )

        client.post(START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 1000})
        deadline = time.monotonic() + 5.0
        while (progress := client.get(SURVEY).json())["observations"] < 100:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert progress["mean_accuracy_mm"] == pytest.approx(2000.0, rel=1e-3)
        assert progress["outcome"] == "running"  # 2 m is above the 1 m limit
        client.post(CANCEL)

    @pytest.mark.parametrize(
        ("min_duration_s", "hard_cap_s"), [(60, 3600), (1500, 4500)]
    )
    def test_a_survey_that_cannot_reach_its_accuracy_aborts_at_the_hard_cap(
        self,
        client: TestClient,
        fake: RecordingFake,
        clock: FakeClock,
        min_duration_s: int,
        hard_cap_s: int,
    ) -> None:
        x, y, z = TRUE_ECEF
        fake.script_survey_positions(
            _fix(x + (2.0 if i % 2 else -2.0), y, z) for i in range(hard_cap_s + 10)
        )
        started = clock.now

        client.post(
            START,
            json={"min_duration_seconds": min_duration_s, "accuracy_limit_mm": 1000},
        )
        progress = _wait_for_outcome(client, "aborted")

        assert progress["abort_reason"] == "accuracy_not_reached"
        assert progress["active"] is False
        assert progress["valid"] is False
        assert progress["mean_accuracy_mm"] == pytest.approx(2000.0, rel=1e-3)
        assert clock.now - started == pytest.approx(hard_cap_s, abs=1.0)
        assert "configure_fixed_base" not in fake.calls

    def test_cancel_stops_sampling_and_leaves_the_receiver_in_rover_mode(
        self, client: TestClient, fake: RecordingFake
    ) -> None:
        client.post(
            START, json={"min_duration_seconds": 600, "accuracy_limit_mm": 1000}
        )
        deadline = time.monotonic() + 5.0
        while client.get(SURVEY).json()["observations"] < 5:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert client.post(CANCEL).status_code == 200
        cancelled = client.get(SURVEY).json()
        time.sleep(0.1)

        assert cancelled["outcome"] == "cancelled"
        assert cancelled["active"] is False
        assert client.get(SURVEY).json()["observations"] == cancelled["observations"]
        assert fake.get_base_config().mode is BaseMode.DISABLED
        assert "configure_fixed_base" not in fake.calls

    def test_a_receiver_that_stops_answering_aborts_the_survey(
        self, client: TestClient, fake: RecordingFake
    ) -> None:
        x, y, z = TRUE_ECEF
        fake.script_survey_positions([_fix(x, y, z)] * 10)
        client.post(START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 1000})
        deadline = time.monotonic() + 5.0
        while client.get(SURVEY).json()["observations"] < 10:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        fake.disconnect()
        progress = _wait_for_outcome(client, "aborted")

        assert progress["abort_reason"] == "device_disconnected"

    def test_a_second_start_is_refused_while_a_survey_runs(
        self, client: TestClient
    ) -> None:
        body = {"min_duration_seconds": 600, "accuracy_limit_mm": 1000}
        assert client.post(START, json=body).status_code == 200

        response = client.post(START, json=body)

        assert response.status_code == 409
        assert "already running" in response.json()["detail"]
        client.post(CANCEL)

    def test_before_any_survey_there_is_nothing_running(
        self, client: TestClient
    ) -> None:
        progress = client.get(SURVEY).json()

        assert progress["outcome"] is None
        assert progress["active"] is False


class ScriptedReceiverFake(RecordingFake):
    """A fake with a Receiver survey-in whose NAV-SVIN replies are scripted."""

    def __init__(self) -> None:
        super().__init__()
        self.statuses: list[SurveyInProgress] = []

    def get_survey_in_status(self) -> SurveyInProgress:
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


@pytest.fixture
def receiver_fake() -> ScriptedReceiverFake:
    driver = ScriptedReceiverFake()
    driver.connect("FAKE")
    return driver


@pytest.fixture
def receiver_client(
    receiver_fake: ScriptedReceiverFake, clock: FakeClock
) -> Iterator[TestClient]:
    device = DeviceService()
    device.set_driver(receiver_fake)
    device._state = DeviceConnectionState.CONNECTED  # pyright: ignore[reportPrivateUsage]
    survey = SurveyService(device, clock=clock, sleep=clock.sleep)
    app = create_api_app()
    app.dependency_overrides[get_device_service] = lambda: device
    app.dependency_overrides[get_survey_service] = lambda: survey
    with TestClient(app) as test_client:
        yield test_client


class TestAnUnfinishedSurveyFallsBackToTheSavedBase:
    """Nothing persists from a survey that never commits (issue #221)."""

    SAVED = FixedBaseConfig(
        latitude=32.7328957, longitude=-117.2362787, altitude_m=29.09, accuracy_mm=17
    )

    def test_cancel_returns_to_the_saved_fixed_base(
        self, client: TestClient, fake: RecordingFake
    ) -> None:
        fake.configure_fixed_base(self.SAVED)
        client.post(
            START, json={"min_duration_seconds": 600, "accuracy_limit_mm": 1000}
        )
        deadline = time.monotonic() + 5.0
        while client.get(SURVEY).json()["observations"] < 5:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert client.post(CANCEL).status_code == 200

        base = fake.get_base_config()
        assert base.mode is BaseMode.FIXED
        assert base.latitude == pytest.approx(32.7328957)

    def test_an_abort_returns_to_the_saved_fixed_base(
        self, client: TestClient, fake: RecordingFake
    ) -> None:
        fake.configure_fixed_base(self.SAVED)
        x, y, z = TRUE_ECEF
        fake.script_survey_positions(
            _fix(x + (2.0 if i % 2 else -2.0), y, z) for i in range(3700)
        )

        client.post(START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 1000})
        _wait_for_outcome(client, "aborted")

        base = fake.get_base_config()
        assert base.mode is BaseMode.FIXED
        assert base.latitude == pytest.approx(32.7328957)


class TestSaveRefusedDuringASurvey:
    """A whole-RAM save mid-survey would make the survey permanent (#221)."""

    def test_save_to_flash_is_refused_while_a_survey_runs(
        self, client: TestClient, fake: RecordingFake
    ) -> None:
        client.post(
            START, json={"min_duration_seconds": 600, "accuracy_limit_mm": 1000}
        )

        resp = client.post("/api/device/save")

        assert resp.status_code == 409
        assert "survey" in resp.json()["detail"].lower()
        assert "save_to_flash" not in fake.calls
        client.post(CANCEL)

    def test_save_to_flash_works_when_no_survey_runs(
        self, client: TestClient, fake: RecordingFake
    ) -> None:
        resp = client.post("/api/device/save")

        assert resp.status_code == 200
        assert "save_to_flash" in fake.calls


class TestReceiverSurveyIn:
    def test_elapsed_time_counts_from_the_receivers_counter_at_the_start(
        self, receiver_client: TestClient, receiver_fake: ScriptedReceiverFake
    ) -> None:
        # The receiver's counter still stands at 25 s from an earlier session
        receiver_fake.statuses = [
            SurveyInProgress(active=True, duration_seconds=25),  # read at the start
            SurveyInProgress(active=False, duration_seconds=40, observations=15),
        ]

        client = receiver_client
        assert (
            client.post(
                START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 50000}
            ).status_code
            == 200
        )
        progress = client.get(SURVEY).json()

        assert progress["averaged_by"] == "receiver"
        assert progress["outcome"] == "running"
        assert progress["duration_seconds"] == 15
        assert progress["active"] is True  # despite HPG 1.12's active=False

    def test_a_valid_receiver_survey_is_reported_completed(
        self, receiver_client: TestClient, receiver_fake: ScriptedReceiverFake
    ) -> None:
        receiver_fake.statuses = [
            SurveyInProgress(active=True, duration_seconds=0),
            SurveyInProgress(
                active=False,
                valid=True,
                duration_seconds=61,
                mean_accuracy_mm=900.0,
                observations=61,
                latitude=32.7,
                longitude=-117.2,
                altitude_m=28.0,
            ),
        ]

        receiver_client.post(
            START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 50000}
        )
        progress = receiver_client.get(SURVEY).json()

        assert progress["outcome"] == "completed"
        assert progress["valid"] is True
        assert progress["averaged_by"] == "receiver"
        assert progress["latitude"] == 32.7
        # the receiver path keeps its page-driven promote: nothing committed here
        assert "configure_fixed_base" not in receiver_fake.calls

    def test_cancel_ends_the_receivers_survey_in(
        self, receiver_client: TestClient, receiver_fake: ScriptedReceiverFake
    ) -> None:
        receiver_fake.statuses = [SurveyInProgress(active=True, duration_seconds=0)]
        receiver_client.post(
            START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 50000}
        )

        assert receiver_client.post(CANCEL).status_code == 200

        assert receiver_client.get(SURVEY).json()["outcome"] == "cancelled"
        # Nothing saved: back to base mode off.
        assert receiver_fake.get_base_config().mode is BaseMode.DISABLED

    def test_cancel_returns_the_receiver_to_its_saved_fixed_base(
        self, receiver_client: TestClient, receiver_fake: ScriptedReceiverFake
    ) -> None:
        receiver_fake.configure_fixed_base(
            FixedBaseConfig(
                latitude=32.7328957,
                longitude=-117.2362787,
                altitude_m=29.09,
                accuracy_mm=17,
            )
        )
        receiver_fake.statuses = [SurveyInProgress(active=True, duration_seconds=0)]
        receiver_client.post(
            START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 50000}
        )

        assert receiver_client.post(CANCEL).status_code == 200

        base = receiver_fake.get_base_config()
        assert base.mode is BaseMode.FIXED
        assert base.latitude == pytest.approx(32.7328957)

    def test_save_is_refused_after_a_survey_completes_until_it_is_promoted(
        self, receiver_client: TestClient, receiver_fake: ScriptedReceiverFake
    ) -> None:
        receiver_fake.statuses = [
            SurveyInProgress(active=True, duration_seconds=0),
            SurveyInProgress(active=False, valid=True, duration_seconds=61),
        ]
        receiver_client.post(
            START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 50000}
        )
        assert receiver_client.get(SURVEY).json()["outcome"] == "completed"

        resp = receiver_client.post("/api/device/save")

        # RAM still holds the survey's base mode until it is promoted.
        assert resp.status_code == 409
        assert "save_to_flash" not in receiver_fake.calls


class TestSurveyBelongsToItsReceiver:
    def test_a_finished_survey_is_forgotten_when_another_receiver_connects(
        self, fake: RecordingFake, clock: FakeClock
    ) -> None:
        device = DeviceService()
        device.set_driver(fake)
        device._state = DeviceConnectionState.CONNECTED  # pyright: ignore[reportPrivateUsage]
        survey = SurveyService(device, clock=clock, sleep=clock.sleep)
        app = create_api_app()
        app.dependency_overrides[get_device_service] = lambda: device
        app.dependency_overrides[get_survey_service] = lambda: survey
        x, y, z = TRUE_ECEF
        fake.script_survey_positions([_fix(x, y, z)] * 60)
        with TestClient(app) as client:
            client.post(
                START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 1000}
            )
            _wait_for_outcome(client, "completed")

            # The operator disconnects and connects another receiver
            device._state = DeviceConnectionState.DISCONNECTED  # pyright: ignore[reportPrivateUsage]
            other = RecordingFake()
            other.connect(FAKE_NO_SURVEY_IN_PORT)
            device.set_driver(other)
            device._state = DeviceConnectionState.CONNECTED  # pyright: ignore[reportPrivateUsage]

            progress = client.get(SURVEY).json()

        assert progress["outcome"] is None
        assert progress["averaged_by"] is None


class SlowReadFake(RecordingFake):
    """Each position read takes 0.4 s of the survey's clock (two UBX polls)."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self._clock = clock

    def get_survey_position(self) -> SurveyPosition:
        self._clock.now += 0.4
        return super().get_survey_position()


class TestReviewFixes:
    def test_a_cancel_after_completion_keeps_the_committed_base(
        self, client: TestClient, fake: RecordingFake
    ) -> None:
        x, y, z = TRUE_ECEF
        fake.script_survey_positions([_fix(x, y, z)] * 60)
        client.post(START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 1000})
        _wait_for_outcome(client, "completed")

        client.post(CANCEL)

        assert fake.get_base_config().mode is BaseMode.FIXED
        assert client.get(SURVEY).json()["outcome"] == "completed"

    def test_sampling_stays_at_once_a_second_when_reads_are_slow(
        self, clock: FakeClock
    ) -> None:
        slow = SlowReadFake(clock)
        slow.connect(FAKE_NO_SURVEY_IN_PORT)
        x, y, z = TRUE_ECEF
        slow.script_survey_positions([_fix(x, y, z)] * 60)
        device = DeviceService()
        device.set_driver(slow)
        device._state = DeviceConnectionState.CONNECTED  # pyright: ignore[reportPrivateUsage]
        survey = SurveyService(device, clock=clock, sleep=clock.sleep)
        app = create_api_app()
        app.dependency_overrides[get_device_service] = lambda: device
        app.dependency_overrides[get_survey_service] = lambda: survey
        started = clock.now
        with TestClient(app) as client:
            client.post(
                START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 1000}
            )
            _wait_for_outcome(client, "completed")

        # 60 one-second observations take about 60 s, not 60 x 1.4 s
        assert clock.now - started == pytest.approx(60.0, abs=1.0)

    def test_the_hard_cap_also_stops_a_survey_short_of_observations(
        self, client: TestClient, fake: RecordingFake, clock: FakeClock
    ) -> None:
        x, y, z = TRUE_ECEF
        no_fix = SurveyPosition(
            ecef_x_m=0.0,
            ecef_y_m=0.0,
            ecef_z_m=0.0,
            accuracy_3d_m=99.0,
            rtk_status="none",
            fix_ok=False,
            correction_age_s=None,
        )
        # Accurate fixes, but only one in a hundred seconds
        fake.script_survey_positions(([_fix(x, y, z)] + [no_fix] * 99) * 40)
        started = clock.now

        client.post(START, json={"min_duration_seconds": 60, "accuracy_limit_mm": 1000})
        progress = _wait_for_outcome(client, "aborted")

        assert progress["abort_reason"] == "accuracy_not_reached"
        assert progress["observations"] < 60
        assert clock.now - started == pytest.approx(3600.0, abs=1.0)


class TestReceiverReviewFixes:
    def test_an_unfinished_receiver_survey_does_not_block_a_new_start(
        self, receiver_client: TestClient, receiver_fake: ScriptedReceiverFake
    ) -> None:
        receiver_fake.statuses = [SurveyInProgress(active=True, duration_seconds=0)]
        body = {"min_duration_seconds": 60, "accuracy_limit_mm": 50000}
        receiver_client.post(START, json=body)

        # e.g. the operator set a fixed base by hand and starts again
        assert receiver_client.post(START, json=body).status_code == 200


class TestRestart:
    def test_after_a_restart_no_survey_runs_and_the_receiver_stays_a_rover(
        self, client: TestClient, fake: RecordingFake, clock: FakeClock
    ) -> None:
        client.post(
            START, json={"min_duration_seconds": 600, "accuracy_limit_mm": 1000}
        )
        deadline = time.monotonic() + 5.0
        while client.get(SURVEY).json()["observations"] < 3:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        # The app restarts: a new SurveyService, the receiver as it was left
        device = DeviceService()
        device.set_driver(fake)
        device._state = DeviceConnectionState.CONNECTED  # pyright: ignore[reportPrivateUsage]
        app = create_api_app()
        app.dependency_overrides[get_device_service] = lambda: device
        app.dependency_overrides[get_survey_service] = lambda: SurveyService(device)
        with TestClient(app) as restarted:
            progress = restarted.get(SURVEY).json()
            base = restarted.get("/api/device/base-config").json()

        assert progress["outcome"] is None
        assert base["mode"] == "disabled"
        client.post(CANCEL)
