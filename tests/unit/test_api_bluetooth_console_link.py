"""The console over a Bluetooth link, through the device API (rtk_development#42).

Driven through ``/api/device`` with the real ``DeviceService`` and the real
u-blox driver behind it. The Bluetooth link is a fake RFCOMM socket in
front of the simulated receiver (sp-rtk-base#221), and the module it
reaches is wired to the receiver's UART2. Each test states what an API
client sees: the HTTP response, the status ``link`` and Stages, and what
the receiver ends up holding.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from functools import partial
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import InputProfile
from sp_rtk_base.services import get_device_service, get_survey_service
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.bluetooth_link import BluetoothLinkOpener
from sp_rtk_base.services.survey_service import SurveyService
from tests.fixtures.fake_rfcomm import (
    DEVICE_NAME,
    MAC,
    PIN,
    FakeBluetoothManager,
    FakeRfcommModule,
)
from tests.fixtures.simulated_ublox import (
    UART1_PORT_ID,
    UART2_PORT_ID,
    SimulatedUblox,
)

BLUETOOTH = {"vendor": "ublox", "link": {"kind": "bluetooth"}}

#: Short enough that a garbage stream ends the test quickly, long enough
#: for every real exchange with the simulated receiver.
READ_LIMIT_S = 1.0


@pytest.fixture()
def receiver() -> SimulatedUblox:
    sim = SimulatedUblox()
    sim.console_port_id = UART2_PORT_ID  # the module is wired to UART2
    return sim


@pytest.fixture()
def module(receiver: SimulatedUblox) -> Iterator[FakeRfcommModule]:
    mod = FakeRfcommModule(receiver)
    yield mod
    mod.stop()


@pytest.fixture()
def manager() -> FakeBluetoothManager:
    return FakeBluetoothManager()


@pytest.fixture()
def input_profile() -> list[InputProfile | None]:
    """The saved Input profile, swappable by a test."""
    return [
        InputProfile(
            source="bluetooth",
            config={"mac_address": MAC, "device_name": DEVICE_NAME, "pin": PIN},
        )
    ]


@pytest.fixture()
def service(
    module: FakeRfcommModule,
    manager: FakeBluetoothManager,
    input_profile: list[InputProfile | None],
) -> Iterator[DeviceService]:
    opener = partial(
        BluetoothLinkOpener,
        manager_factory=lambda adapter: manager,
        socket_factory=module.new_socket,
        read_limit=READ_LIMIT_S,
    )
    svc = DeviceService(input_profile=lambda: input_profile[0], bluetooth_opener=opener)
    with patch("sp_rtk_base.services.drivers.ublox.time.sleep"):
        yield svc
        if svc.driver is not None:
            svc.driver.disconnect()


@pytest.fixture()
def client(service: DeviceService) -> TestClient:
    app = create_api_app()
    survey = SurveyService(service)
    app.dependency_overrides[get_device_service] = lambda: service
    app.dependency_overrides[get_survey_service] = lambda: survey
    return TestClient(app)


def _stages(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["stage"]: s for s in status["connect_stages"]}


class TestConnectOverBluetooth:
    def test_connects_and_reports_the_link_the_console_port_and_the_stages(
        self, client: TestClient
    ) -> None:
        resp = client.post("/api/device/connect", json=BLUETOOTH)

        assert resp.status_code == 200, resp.text
        status = client.get("/api/device/status").json()
        assert status["state"] == "connected"
        assert status["link"] == {
            "kind": "bluetooth",
            "device_name": DEVICE_NAME,
            "mac": MAC,
        }
        assert status["port"] is None
        assert status["baud_rate"] is None
        assert status["console_port"] == "UART2"
        stages = _stages(status)
        assert [s["stage"] for s in status["connect_stages"]] == [
            "pair",
            "connect",
            "identify",
        ]
        assert stages["pair"]["status"] == "skipped"  # already Bonded
        assert stages["connect"]["status"] == "passed"
        assert stages["identify"]["status"] == "passed"

    def test_pairs_on_demand_when_there_is_no_bond(
        self, client: TestClient, manager: FakeBluetoothManager
    ) -> None:
        manager.bonded.clear()

        resp = client.post("/api/device/connect", json=BLUETOOTH)

        assert resp.status_code == 200, resp.text
        stages = _stages(client.get("/api/device/status").json())
        assert stages["pair"]["status"] == "passed"
        assert MAC in manager.bonded


class TestAFailedConnectIsRedOnItsStage:
    def test_a_refused_pin_is_red_at_pair(
        self,
        client: TestClient,
        manager: FakeBluetoothManager,
        input_profile: list[InputProfile | None],
    ) -> None:
        manager.bonded.clear()
        input_profile[0] = InputProfile(
            source="bluetooth", config={"mac_address": MAC, "pin": "9999"}
        )

        resp = client.post("/api/device/connect", json=BLUETOOTH)

        assert resp.status_code == 502
        status = client.get("/api/device/status").json()
        assert status["state"] == "error"
        stages = _stages(status)
        assert stages["pair"]["status"] == "failed"
        assert stages["pair"]["code"] == "pin_rejected"
        assert "PIN" in stages["pair"]["advice"]
        assert stages["connect"]["status"] == "pending"
        assert stages["identify"]["status"] == "pending"
        assert manager.closed == 1, "the session's manager was not closed"

    def test_a_module_that_does_not_answer_is_red_at_connect(
        self,
        client: TestClient,
        module: FakeRfcommModule,
        manager: FakeBluetoothManager,
    ) -> None:
        module.answers = False

        resp = client.post("/api/device/connect", json=BLUETOOTH)

        assert resp.status_code == 502
        stages = _stages(client.get("/api/device/status").json())
        assert stages["pair"]["status"] == "skipped"
        assert stages["connect"]["status"] == "failed"
        assert stages["connect"]["code"] == "socket_refused"
        assert "powered on" in stages["connect"]["advice"]
        assert stages["identify"]["status"] == "pending"
        assert manager.closed == 1

    def test_garbage_bytes_are_red_at_identify_and_end_on_the_read_limit(
        self,
        client: TestClient,
        module: FakeRfcommModule,
        manager: FakeBluetoothManager,
    ) -> None:
        module.garbage = True

        started = time.monotonic()
        resp = client.post("/api/device/connect", json=BLUETOOTH)
        took = time.monotonic() - started

        assert resp.status_code == 502
        # Well inside the connect's own 10 s MON-VER budget: the read
        # limit ended it.
        assert took < 5
        stages = _stages(client.get("/api/device/status").json())
        assert stages["connect"]["status"] == "passed"
        assert stages["identify"]["status"] == "failed"
        assert stages["identify"]["code"] == "no_ubx_answer"
        assert "baud rate" in stages["identify"]["advice"]
        assert manager.disconnects == [MAC]
        assert manager.closed == 1

    def test_connect_without_a_bluetooth_input_profile_is_refused(
        self, client: TestClient, input_profile: list[InputProfile | None]
    ) -> None:
        input_profile[0] = InputProfile(
            source="tcp", config={"host": "10.0.0.1", "port": 2101}
        )

        resp = client.post("/api/device/connect", json=BLUETOOTH)

        assert resp.status_code == 409
        assert "Input page" in resp.json()["detail"]


class TestRefusedWhileTheRelayRuns:
    def test_connect_is_refused_and_the_module_is_never_touched(
        self,
        client: TestClient,
        service: DeviceService,
        module: FakeRfcommModule,
        manager: FakeBluetoothManager,
    ) -> None:
        service.set_relay_check(lambda: True)

        resp = client.post("/api/device/connect", json=BLUETOOTH)

        assert resp.status_code == 409
        assert "relay" in resp.json()["detail"].lower()
        assert module.connects == 0
        assert manager.disconnects == []
        assert manager.closed == 0


class TestALinkClosedMidSessionIsALostDevice:
    def test_the_module_closing_the_link_loses_the_device(
        self,
        client: TestClient,
        module: FakeRfcommModule,
        manager: FakeBluetoothManager,
    ) -> None:
        client.post("/api/device/connect", json=BLUETOOTH)

        module.drop()

        status = client.get("/api/device/status").json()
        assert status["state"] == "disconnected"
        assert status["link"] is None
        assert "lost" in status["last_error"].lower()
        # Torn down in ADR 0002's order, once.
        assert manager.disconnects == [MAC]
        assert manager.closed == 1

    def test_a_poll_after_the_drop_is_refused_as_not_connected(
        self, client: TestClient, module: FakeRfcommModule
    ) -> None:
        client.post("/api/device/connect", json=BLUETOOTH)
        module.drop()

        resp = client.get("/api/device/survey-in")

        assert resp.status_code == 409
        assert "lost" in resp.json()["detail"].lower()


SURVEY = {"min_duration_seconds": 120, "accuracy_limit_mm": 2000}


class TestTheConsoleWorksOverTheLink:
    def test_survey_in_starts_and_cancel_returns_to_the_saved_base(
        self, client: TestClient, receiver: SimulatedUblox
    ) -> None:
        client.post("/api/device/connect", json=BLUETOOTH)

        started = client.post("/api/device/configure/survey-in", json=SURVEY)

        assert started.status_code == 200, started.text
        assert receiver.value("CFG_TMODE_MODE") == 1
        assert client.get("/api/device/survey-in").json()["active"] is True

        cancelled = client.post("/api/device/cancel-survey-in")

        assert cancelled.status_code == 200, cancelled.text
        assert receiver.value("CFG_TMODE_MODE") != 1
        assert client.get("/api/device/survey-in").json()["active"] is False
        assert client.get("/api/device/status").json()["state"] == "connected"

    def test_reset_gps_reopens_the_link_and_identifies_the_console_port_again(
        self,
        client: TestClient,
        receiver: SimulatedUblox,
        module: FakeRfcommModule,
        manager: FakeBluetoothManager,
    ) -> None:
        client.post("/api/device/connect", json=BLUETOOTH)
        # Only a fresh probe after the reset can see this: the port the
        # receiver now hears the console on.
        receiver.console_port_id = UART1_PORT_ID

        resp = client.post("/api/device/reset")

        assert resp.status_code == 200, resp.text
        assert 0x00 in receiver.resets
        assert module.connects == 2, "the link was not closed and reopened"
        assert manager.closed == 0, "the session's manager closed on a reopen"
        status = client.get("/api/device/status").json()
        assert status["state"] == "connected"
        assert status["link"]["kind"] == "bluetooth"
        assert status["console_port"] == "UART1"
