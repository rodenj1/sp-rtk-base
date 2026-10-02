"""Tests for running a Corrected survey-in end to end (issues #195, #196).

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
import socket
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
    get_correction_verification_service,
    get_device_service,
    get_relay_service,
    get_survey_service,
    wire_corrected_survey,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.correction_verification import (
    CorrectionSourceVerificationService,
)
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
        self.written = b""  # every correction byte, in the order written
        self.hub_alive_at_commit: bool | None = None
        # Corrections bring a Fixed at once (the clock here is real time).
        self.float_after_s = 0.0
        self.fixed_after_s = 0.0

    def begin_correction_input(self, console_port: PortId | None) -> None:
        self.calls.append("begin_correction_input")
        self.console_ports.append(console_port)
        super().begin_correction_input(console_port)

    def write_corrections(self, frames: bytes) -> None:
        self.written += frames
        super().write_corrections(frames)

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
    verifier = CorrectionSourceVerificationService(data_window_seconds=1.0)
    # The same wiring the app does (services/__init__.py).
    wire_corrected_survey(survey, mock_config_service, verifier)
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
    app.dependency_overrides[get_correction_verification_service] = lambda: verifier
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

        assert rover.written
        assert b"".join(FRAMES * 50).startswith(rover.written)

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


# ---- Failure rules (issue #196) ----

SOURCES = "/api/correction-sources"
VERIFY = "/api/correction-sources/verify"
UNAUTHORIZED = b"HTTP/1.0 401 Unauthorized\r\n\r\n"


def _position(status: str, age: float | None = 1.0) -> SurveyPosition:
    x, y, z = TRUE_ECEF
    return SurveyPosition(
        ecef_x_m=x,
        ecef_y_m=y,
        ecef_z_m=z,
        accuracy_3d_m=0.01,
        rtk_status=status,
        fix_ok=True,
        correction_age_s=age,
    )


class TestStartRefusal:
    @pytest.mark.parametrize(
        ("script", "stage"),
        [
            (Script(reply=b"<html>Banned</html>\r\n", hold=False), "caster"),
            (Script(reply=UNAUTHORIZED, hold=False), "auth"),
            (
                Script(
                    reply=b"SOURCETABLE 200 OK\r\n\r\nENDSOURCETABLE\r\n", hold=False
                ),
                "mountpoint",
            ),
        ],
    )
    def test_an_unreachable_source_names_the_failing_stage(
        self,
        client: TestClient,
        caster: FakeCaster,
        rover: RecordingRover,
        script: Script,
        stage: str,
    ) -> None:
        caster.scripts.append(script)

        response = _start(client)

        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "source_unreachable"
        assert body["stage"] == stage
        assert stage in body["message"]
        assert rover.calls[-1] == "end_correction_input"
        assert client.get(SURVEY).json()["outcome"] is None  # nothing started

    def test_a_closed_port_names_connect(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        # Point the saved source at a port nothing listens on.
        unused = socket.create_server(("127.0.0.1", 0))
        port = unused.getsockname()[1]
        unused.close()
        client.put(f"{SOURCES}/local", json={"port": port})

        response = _start(client)

        assert response.json()["stage"] == "connect"


class GatedRover(RecordingRover):
    """A rover whose position reads wait, after ``gate_after`` reads, until released."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.gate_after: int | None = None
        self.release = threading.Event()

    def get_survey_position(self) -> SurveyPosition:
        self.reads += 1
        if self.gate_after is not None and self.reads > self.gate_after:
            self.release.wait(10)
        return super().get_survey_position()


class TestStall:
    def test_ten_minutes_with_only_float_aborts_as_no_fixed(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 2000))
        rover.fixed_after_s = 3600.0  # corrections in use, Float only

        _start(client)
        progress = _wait_for_outcome(client, "aborted")

        assert progress["abort_reason"] == "no_fixed"
        assert "configure_fixed_base" not in rover.calls
        assert rover.get_base_config().mode is BaseMode.DISABLED  # a rover
        assert "end_correction_input" in rover.calls

    def test_ten_minutes_without_corrections_aborts_as_no_corrections(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY))  # accepted, never sends

        _start(client)
        progress = _wait_for_outcome(client, "aborted")

        assert progress["abort_reason"] == "no_corrections"
        assert "configure_fixed_base" not in rover.calls

    def test_no_corrections_carries_the_relays_last_error(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        # Accepted, then dropped; every reconnect is refused.
        caster.scripts += [Script(reply=ICY, hold=False)] + [
            Script(reply=UNAUTHORIZED, hold=False) for _ in range(20)
        ]

        _start(client)
        progress = _wait_for_outcome(client, "aborted")

        assert progress["abort_reason"] == "no_corrections"
        assert progress["source_last_error"]

    def test_a_shorter_outage_only_pauses_the_survey(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 50))
        # 30 Fixed, nine and a half minutes without, then 30 Fixed.
        rover.script_survey_positions(
            [_position("fixed")] * 30
            + [_position("none", age=None)] * 570
            + [_position("fixed")] * 30
        )

        _start(client)
        progress = _wait_for_outcome(client, "completed")

        assert progress["observations"] == 60

    def test_a_failed_survey_never_falls_back_to_a_plain_one(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY))

        _start(client)
        _wait_for_outcome(client, "aborted")

        assert rover.calls.count("disable_base_mode") == 1  # the start only
        progress = client.get(SURVEY).json()
        assert progress["outcome"] == "aborted"  # not running again, any mode


class TestWarning:
    @pytest.fixture
    def gated_rover(self) -> GatedRover:
        driver = GatedRover()
        driver.connect(FAKE_NO_SURVEY_IN_PORT)
        return driver

    def test_progress_says_how_long_fixed_has_been_missing_and_why(
        self,
        caster: FakeCaster,
        gated_rover: GatedRover,
        mock_config_service: ConfigService,
        relay: RelayService,
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 2000))
        gated_rover.fixed_after_s = 3600.0  # Float only
        gated_rover.gate_after = 90
        with _client_for(gated_rover, caster, mock_config_service, relay) as c:
            _start(c)
            progress = _wait_for(
                c,
                lambda p: (p.get("seconds_without_fixed") or 0) >= 89,
                "90 s without Fixed",
            )
            gated_rover.release.set()

        assert progress["outcome"] == "running"
        assert progress["stall_warning"] is True  # over 60 s
        assert progress["stall_reason"] == "no_fixed"
        assert (
            progress["stall_abort_in_seconds"]
            == 600 - progress["seconds_without_fixed"]
        )


class TestWarningBoundary:
    @pytest.fixture
    def gated_rover(self) -> GatedRover:
        driver = GatedRover()
        driver.connect(FAKE_NO_SURVEY_IN_PORT)
        return driver

    @pytest.mark.parametrize(("reads", "warned"), [(60, False), (61, True)])
    def test_the_warning_starts_once_fixed_is_missing_for_over_60_s(
        self,
        caster: FakeCaster,
        gated_rover: GatedRover,
        mock_config_service: ConfigService,
        relay: RelayService,
        reads: int,
        warned: bool,
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 2000))
        gated_rover.fixed_after_s = 3600.0  # Float only
        gated_rover.gate_after = reads + 1
        with _client_for(gated_rover, caster, mock_config_service, relay) as c:
            _start(c)
            progress = _wait_for(
                c,
                lambda p: p.get("seconds_without_fixed") == reads,
                f"{reads} s without Fixed",
            )
            gated_rover.release.set()

        assert progress["stall_warning"] is warned


class TestStallRestore:
    def test_a_stall_whose_input_cant_be_restored_says_so(
        self,
        caster: FakeCaster,
        mock_config_service: ConfigService,
        relay: RelayService,
    ) -> None:
        rover = RestoreFailsRover()
        rover.connect(FAKE_NO_SURVEY_IN_PORT)
        rover.fixed_after_s = 3600.0
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 2000))
        with _client_for(rover, caster, mock_config_service, relay) as client:
            _start(client)
            progress = _wait_for_outcome(client, "aborted")

        assert progress["abort_reason"] == "input_not_restored"


class TestProtection:
    def _running(self, client: TestClient, caster: FakeCaster, rover: Any) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 2000))
        # Fixed throughout (no stall), and a minimum it won't reach.
        _start(client, min_duration_seconds=86400)
        _wait_for(client, lambda p: p.get("outcome") == "running", "running")

    def test_renaming_the_source_in_use_is_refused(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        self._running(client, caster, rover)

        response = client.put(f"{SOURCES}/local", json={"name": "renamed"})

        assert response.status_code == 409
        assert response.json()["code"] == "in_use"

    def test_deleting_the_source_in_use_is_refused(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        self._running(client, caster, rover)

        response = client.delete(f"{SOURCES}/local")

        assert response.status_code == 409
        assert response.json()["code"] == "in_use"
        assert client.get(f"{SOURCES}/local").status_code == 200

    def test_after_the_survey_the_source_can_be_deleted(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        self._running(client, caster, rover)
        client.post("/api/device/cancel-survey-in")

        assert client.delete(f"{SOURCES}/local").status_code == 200

    def test_the_source_is_in_use_while_the_survey_connects(
        self, client: TestClient, caster: FakeCaster
    ) -> None:
        caster.scripts.append(Script(delay=1.5, reply=ICY, body=FRAMES * 2000))
        starting = threading.Thread(target=lambda: _start(client))
        starting.start()
        _wait_for_request(caster)

        response = client.delete(f"{SOURCES}/local")
        starting.join(10)

        assert response.status_code == 409
        assert response.json()["code"] == "in_use"

    def test_importing_a_config_without_the_source_in_use_is_refused(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        self._running(client, caster, rover)
        exported = client.get("/api/config/export").text
        without = exported.split("correction_sources:")[0]

        response = client.post(
            "/api/config/import",
            files={"file": ("config.yaml", without, "application/x-yaml")},
        )

        assert response.status_code == 409
        assert client.get(f"{SOURCES}/local").status_code == 200

    def test_a_verification_is_refused_while_the_survey_runs(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        self._running(client, caster, rover)

        response = client.post(
            VERIFY,
            json={"caster": "127.0.0.1", "port": caster.port, "mountpoint": "MP1"},
        )

        assert response.status_code == 409
        assert response.json()["code"] == "survey_running"
        assert len(caster.requests) == 1  # the survey's own connection only


def _wait_for_request(caster: FakeCaster) -> None:
    deadline = time.monotonic() + 5.0
    while not caster.requests:
        assert time.monotonic() < deadline, "the survey never connected"
        time.sleep(0.01)


# ---- Correction delivery counters (bench diagnosis, #197) ----


class FailingWriteRover(RecordingRover):
    """A rover whose port rejects every correction write."""

    def write_corrections(self, frames: bytes) -> None:
        raise OSError("write failed")


class TestDeliveryCounters:
    def test_progress_counts_the_frames_written_and_the_receivers_rtcm(
        self, client: TestClient, caster: FakeCaster, rover: RecordingRover
    ) -> None:
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 2000))
        rover.fixed_after_s = 3600.0  # keep it running

        _start(client, min_duration_seconds=86400)
        progress = _wait_for(
            client,
            lambda p: (p.get("receiver_rtcm3_messages") or 0) >= 10,
            "the receiver to count RTCM",
        )

        assert progress["corrections_written"] >= 10
        assert progress["correction_write_failures"] == 0
        assert progress["corrections_dropped"] == 0
        assert progress["receiver_rx_bytes"] > 0
        assert progress["correction_bytes_written"] > 0
        assert progress["receiver_overrun_errors"] == 0

    def test_failed_writes_are_counted(
        self,
        caster: FakeCaster,
        mock_config_service: ConfigService,
        relay: RelayService,
    ) -> None:
        rover = FailingWriteRover()
        rover.connect(FAKE_NO_SURVEY_IN_PORT)
        caster.scripts.append(Script(reply=ICY, body=FRAMES * 2000))
        with _client_for(rover, caster, mock_config_service, relay) as client:
            _start(client, min_duration_seconds=86400)
            progress = _wait_for(
                client,
                lambda p: (p.get("correction_write_failures") or 0) >= 5,
                "failed writes",
            )

        assert progress["corrections_written"] == 0
        assert progress["receiver_rtcm3_messages"] == 0


# ---- Correction writes keep up on a busy receiver link (#197) ----


class BusyLinkRover(RecordingRover):
    """A rover whose position reads hold the driver lock for a while.

    Like a ZED-F9P on a busy 57 600-baud UART, where each UBX poll waits
    out the receiver's own RTCM output. Correction writes take the same lock.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()

    def get_survey_position(self) -> SurveyPosition:
        with self.lock:
            time.sleep(0.4)
            return super().get_survey_position()

    def write_corrections(self, frames: bytes) -> None:
        with self.lock:
            super().write_corrections(frames)


class TestBusyLink:
    def test_every_frame_is_written_while_reads_hold_the_lock(
        self,
        caster: FakeCaster,
        mock_config_service: ConfigService,
        relay: RelayService,
    ) -> None:
        rover = BusyLinkRover()
        rover.connect(FAKE_NO_SURVEY_IN_PORT)
        rover.fixed_after_s = 3600.0  # keep it running
        stream = FRAMES * 100  # 300 Frames over about 6 s
        caster.scripts.append(Script(reply=ICY, body=stream))
        with _client_for(rover, caster, mock_config_service, relay) as client:
            _start(client, min_duration_seconds=86400)
            progress = _wait_for(
                client,
                lambda p: (p.get("corrections_written") or 0) >= len(stream),
                "every Frame written",
            )

        assert progress["corrections_dropped"] == 0
        assert rover.written == b"".join(stream)  # whole, in order, unchanged
