"""Tests for running a Corrected survey-in end to end (issue #195).

Seam under test: the survey HTTP API
(``POST /api/device/configure/corrected-survey-in``,
``GET /api/device/survey-in``, ``POST /api/device/cancel-survey-in``,
``POST /api/device/disconnect``) via TestClient, over a real DeviceService,
a real SurveyService (injected clock, so 1 Hz sampling runs instantly), the
fake driver acting as a rover, and a scripted fake NTRIP caster on a real
localhost socket that the survey's own Relay engine pulls from.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import (
    CorrectionSourceProfile,
    NtripCorrectionConfig,
)
from sp_rtk_base.models.device_models import (
    BaseMode,
    ConsolePortReading,
    DeviceConnectionState,
    PortId,
    SurveyPosition,
)
from sp_rtk_base.services import (
    get_config_service,
    get_device_service,
    get_relay_service,
    get_survey_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.fake import FAKE_NO_SURVEY_IN_PORT, FakeGpsDriver
from sp_rtk_base.services.geodesy import ecef_to_llh, llh_to_ecef
from sp_rtk_base.services.relay_service import RelayService
from sp_rtk_base.services.survey_service import SurveyService
from tests.fixtures.scripted_ntrip_caster import FakeCaster, Script
from tests.unit.msm_frames import other_frame

START = "/api/device/configure/corrected-survey-in"
SURVEY = "/api/device/survey-in"
CANCEL = "/api/device/cancel-survey-in"
DISCONNECT = "/api/device/disconnect"

ICY = b"ICY 200 OK\r\n"
FRAMES = [other_frame(1005).data, other_frame(1077).data, other_frame(1087).data]
TRUE_ECEF = llh_to_ecef(32.7329015, -117.2362788, 27.94)


class FakeClock:
    """A monotonic clock that only moves when the survey sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0.001)  # let corrections and requests through

    def __call__(self) -> float:
        return self.now


class RecordingRover(FakeGpsDriver):
    """The fake rover, recording the calls a Corrected survey-in makes."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.console_ports: list[PortId | None] = []
        self.frames: list[bytes] = []
        self.hub_alive_at_commit: bool | None = None
        # Corrections bring a Fixed at once (the clock here is real time).
        self.float_after_s = 0.0
        self.fixed_after_s = 0.0

    def begin_correction_input(self, console_port: PortId | None) -> None:
        self.calls.append("begin_correction_input")
        self.console_ports.append(console_port)
        super().begin_correction_input(console_port)

    def write_corrections(self, frame: bytes) -> None:
        self.frames.append(frame)
        super().write_corrections(frame)

    def end_correction_input(self) -> None:
        self.calls.append("end_correction_input")
        super().end_correction_input()

    def configure_fixed_base(self, config: Any) -> None:
        self.calls.append("configure_fixed_base")
        self.hub_alive_at_commit = _hub_alive()
        super().configure_fixed_base(config)

    def save_to_flash(self) -> None:
        self.calls.append("save_to_flash")
        super().save_to_flash()

    def disable_base_mode(self) -> None:
        self.calls.append("disable_base_mode")
        super().disable_base_mode()

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        super().disconnect()


def _hub_alive() -> bool:
    """Whether any Relay engine's threads are still running."""
    return any(t.name.startswith("BroadcastHub") for t in threading.enumerate())


@pytest.fixture
def caster() -> Iterator[FakeCaster]:
    fake = FakeCaster()
    yield fake
    fake.close()


@pytest.fixture
def rover() -> RecordingRover:
    driver = RecordingRover()
    driver.connect(FAKE_NO_SURVEY_IN_PORT)
    return driver


@pytest.fixture
def relay() -> RelayService:
    return RelayService()


@pytest.fixture
def client(
    rover: RecordingRover,
    caster: FakeCaster,
    mock_config_service: ConfigService,
    relay: RelayService,
) -> Iterator[TestClient]:
    with _client_for(rover, caster, mock_config_service, relay) as test_client:
        yield test_client


@contextlib.contextmanager
def _client_for(
    rover: RecordingRover,
    caster: FakeCaster,
    mock_config_service: ConfigService,
    relay: RelayService,
) -> Iterator[TestClient]:
    device = DeviceService()
    device.set_driver(rover)
    device._state = DeviceConnectionState.CONNECTED  # pyright: ignore[reportPrivateUsage]
    device._console_port = ConsolePortReading.known(PortId.USB)  # pyright: ignore[reportPrivateUsage]
    clock = FakeClock()
    survey = SurveyService(device, clock=clock, sleep=clock.sleep)
    mock_config_service.create_correction_source(
        CorrectionSourceProfile(
            name="local",
            config=NtripCorrectionConfig(
                caster="127.0.0.1",
                port=caster.port,
                mountpoint="MP1",
                username="rover",
                password="pw",
                version="1.0",
            ),
        )
    )
    app = create_api_app()
    app.dependency_overrides[get_device_service] = lambda: device
    app.dependency_overrides[get_survey_service] = lambda: survey
    app.dependency_overrides[get_config_service] = lambda: mock_config_service
    app.dependency_overrides[get_relay_service] = lambda: relay
    with TestClient(app) as test_client:
        yield test_client
    asyncio.run(survey.shutdown())


def _start(client: TestClient, **overrides: Any) -> Any:
    body: dict[str, Any] = {
        "correction_source": "local",
        "min_duration_seconds": 60,
        "accuracy_limit_mm": 50,
    }
    body.update(overrides)
    return client.post(START, json=body)


def _wait_for(client: TestClient, done: Any, what: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10.0
    while True:
        progress: dict[str, Any] = client.get(SURVEY).json()
        if done(progress):
            return progress
        assert time.monotonic() < deadline, f"never {what}: {progress}"
        time.sleep(0.01)


def _wait_for_outcome(client: TestClient, outcome: str) -> dict[str, Any]:
    return _wait_for(client, lambda p: p.get("outcome") == outcome, outcome)


def _fixed(x: float, y: float, z: float, status: str = "fixed") -> SurveyPosition:
    return SurveyPosition(
        ecef_x_m=x,
        ecef_y_m=y,
        ecef_z_m=z,
        accuracy_3d_m=0.01,
        rtk_status=status,
        fix_ok=True,
        correction_age_s=1.0,
    )


class TestCompletion:
    def test_a_corrected_survey_completes_on_rtk_fixed_and_commits_the_base(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))

        response = _start(client)

        assert response.status_code == 200
        progress = _wait_for_outcome(client, "completed")
        assert progress["averaged_by"] == "application"
        assert progress["correction_source"] == "local"
        assert progress["duration_seconds"] == 60
        assert progress["mean_accuracy_mm"] <= 50
        base = rover.get_base_config()
        assert base.mode is BaseMode.FIXED
        lat, lon, _ = ecef_to_llh(*TRUE_ECEF)
        assert base.latitude == pytest.approx(lat, abs=1e-7)  # about 1 cm
        assert base.longitude == pytest.approx(lon, abs=1e-7)

    def test_the_receiver_is_a_rover_taking_rtcm3_on_its_console_port(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))

        _start(client)
        _wait_for_outcome(client, "completed")

        assert rover.calls[:2] == ["disable_base_mode", "begin_correction_input"]
        assert rover.console_ports == [PortId.USB]

    def test_every_frame_reaches_the_receiver_whole(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))

        _start(client)
        _wait_for_outcome(client, "completed")

        assert rover.frames
        assert all(frame in FRAMES for frame in rover.frames)
        assert rover.frames == (FRAMES * 50)[: len(rover.frames)]

    def test_the_engine_stops_and_input_is_restored_before_the_flash_save(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))

        _start(client)
        _wait_for_outcome(client, "completed")

        assert rover.calls[-3:] == [
            "end_correction_input",
            "configure_fixed_base",
            "save_to_flash",
        ]
        assert rover.hub_alive_at_commit is False

    def test_only_rtk_fixed_epochs_count_as_observations(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))
        x, y, z = TRUE_ECEF
        # Float a metre off, then 60 Fixed on the point: only the Fixed count.
        rover.script_survey_positions(
            [_fixed(x + 1.0, y, z, status="float")] * 20
            + [_fixed(x, y, z, status="none")] * 5
            + [_fixed(x, y, z)] * 60
        )

        _start(client)
        progress = _wait_for_outcome(client, "completed")

        assert progress["observations"] == 60
        lat, lon, alt = ecef_to_llh(x, y, z)
        assert progress["latitude"] == pytest.approx(lat, abs=1e-9)
        assert progress["altitude_m"] == pytest.approx(alt, abs=1e-4)


class TestProgress:
    def test_progress_reports_rtk_correction_age_and_the_source(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))
        rover.fixed_after_s = 3600.0  # Float only: the survey keeps running

        _start(client, min_duration_seconds=120, accuracy_limit_mm=20)
        progress = _wait_for(client, lambda p: p.get("rtk_status") == "float", "float")

        assert progress["outcome"] == "running"
        assert progress["correction_source"] == "local"
        assert progress["source_connected"] is True
        assert progress["source_last_error"] is None
        assert progress["correction_age_s"] is not None
        assert progress["min_duration_seconds"] == 120
        assert progress["accuracy_limit_mm"] == 20
        assert progress["duration_seconds"] == 0  # no Fixed yet

    def test_the_survey_engine_is_invisible_to_the_relay(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))
        rover.fixed_after_s = 3600.0

        _start(client)
        _wait_for(client, lambda p: p.get("source_connected") is True, "connected")

        assert client.get("/api/relay/status").json()["running"] is False
        # and the receiver can still be read: "relay running" doesn't block it
        assert client.get("/api/device/base-config").status_code == 200


class TestStopping:
    def test_cancel_stops_the_engine_restores_input_and_stays_a_rover(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))
        rover.fixed_after_s = 3600.0
        _start(client)
        _wait_for(client, lambda p: p.get("source_connected") is True, "connected")

        response = client.post(CANCEL)

        assert response.status_code == 200
        assert client.get(SURVEY).json()["outcome"] == "cancelled"
        assert "end_correction_input" in rover.calls
        assert "configure_fixed_base" not in rover.calls
        assert rover.get_base_config().mode is BaseMode.DISABLED
        assert not _hub_alive()

    def test_disconnect_stops_the_engine_and_restores_input_first(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))
        rover.fixed_after_s = 3600.0
        _start(client)
        _wait_for(client, lambda p: p.get("source_connected") is True, "connected")

        client.post(DISCONNECT)

        assert rover.calls[-2:] == ["end_correction_input", "disconnect"]
        progress = client.get(SURVEY).json()
        assert progress["outcome"] == "aborted"
        assert progress["abort_reason"] == "device_disconnected"
        assert not _hub_alive()


class TestStart:
    def test_needs_a_saved_correction_source(self, client: TestClient) -> None:
        response = _start(client, correction_source="nope")

        assert response.status_code == 404

    def test_needs_no_verification_or_first_frame(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY))  # accepted, sends nothing yet

        response = _start(client)

        assert response.status_code == 200
        assert client.get(SURVEY).json()["outcome"] == "running"

    def test_a_second_start_is_refused_while_one_runs(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))
        rover.fixed_after_s = 3600.0
        _start(client)

        assert _start(client).status_code == 409

    def test_an_unreachable_source_refuses_the_start_and_restores_input(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(
            Script(reply=b"HTTP/1.0 401 Unauthorized\r\n\r\n", hold=False)
        )

        response = _start(client)

        assert response.status_code == 409
        assert rover.calls[-1] == "end_correction_input"
        assert not _hub_alive()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("accuracy_limit_mm", 9),
            ("accuracy_limit_mm", 1001),
            ("min_duration_seconds", 59),
        ],
    )
    def test_limits_outside_the_corrected_bounds_are_rejected(
        self, client: TestClient, field: str, value: int
    ) -> None:
        assert _start(client, **{field: value}).status_code == 422


class RestoreFailsRover(RecordingRover):
    """A rover whose input settings can't be restored."""

    def end_correction_input(self) -> None:
        self.calls.append("end_correction_input")
        raise RuntimeError("Correction input restore did not take effect")


class TestReviewFixes:
    def test_a_failed_restore_commits_and_saves_nothing(
        self,
        caster: FakeCaster,
        mock_config_service: ConfigService,
        relay: RelayService,
    ) -> None:
        rover = RestoreFailsRover()
        rover.connect(FAKE_NO_SURVEY_IN_PORT)
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))
        with _client_for(rover, caster, mock_config_service, relay) as client:
            _start(client)
            progress = _wait_for_outcome(client, "aborted")

        assert progress["abort_reason"] == "input_not_restored"
        assert "configure_fixed_base" not in rover.calls
        assert "save_to_flash" not in rover.calls

    def test_the_sources_last_error_reaches_progress(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        rover.fixed_after_s = 3600.0
        # Accepted, a few Frames, then the caster drops the stream and
        # refuses the Relay's reconnect.
        caster.scripts += [
            Script(reply=ICY, body=FRAMES, hold=False),
            Script(reply=b"HTTP/1.0 401 Unauthorized\r\n\r\n", hold=False),
        ]

        # A long minimum keeps the hard cap far off on the fast test clock.
        _start(client, min_duration_seconds=86400)
        progress = _wait_for(
            client, lambda p: p.get("source_last_error") is not None, "an error"
        )

        assert progress["source_connected"] is False
        assert progress["outcome"] == "running"  # an outage only pauses it

    def test_the_operators_relay_is_never_touched(
        self,
        client: TestClient,
        caster: FakeCaster,
        rover: RecordingRover,
        relay: RelayService,
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))

        _start(client)
        _wait_for_outcome(client, "completed")

        # Signal Quality and metrics attach through the operator's Relay only.
        assert relay.engine is None
        assert relay.is_running is False
