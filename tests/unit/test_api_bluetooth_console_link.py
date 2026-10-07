"""The console over a Bluetooth link, through the device API (rtk_development#42).

Driven through ``/api/device`` with the real ``DeviceService`` and the real
u-blox driver behind it. The Bluetooth link is a fake RFCOMM socket in
front of the simulated receiver (sp-rtk-base#221), and the module it
reaches is wired to the receiver's UART2. Each test states what an API
client sees: the HTTP response, the status ``link`` and Stages, and what
the receiver ends up holding.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import (
    DestinationProfile,
    DeviceProfile,
    InputProfile,
)
from sp_rtk_base.models.device_models import BluetoothLink, DeviceConnectionState
from sp_rtk_base.services import (
    get_config_service,
    get_device_service,
    get_relay_service,
    get_survey_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers import create_driver
from sp_rtk_base.services.drivers.bluetooth_link import BluetoothLinkOpener
from sp_rtk_base.services.relay_service import RelayService
from sp_rtk_base.services.survey_service import SurveyService
from tests.fixtures.fake_rfcomm import (
    DEVICE_NAME,
    MAC,
    PIN,
    FakeBlueZ,
    FakeRfcommModule,
)
from tests.fixtures.saved_start import with_saved_start
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
def bluez() -> FakeBlueZ:
    return FakeBlueZ()


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
    bluez: FakeBlueZ,
    input_profile: list[InputProfile | None],
) -> Iterator[DeviceService]:
    opener = partial(
        BluetoothLinkOpener,
        manager_factory=bluez.manager,
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
        assert f"over Bluetooth {DEVICE_NAME}" in resp.json()["message"]
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
        self, client: TestClient, bluez: FakeBlueZ
    ) -> None:
        bluez.bonded.clear()

        resp = client.post("/api/device/connect", json=BLUETOOTH)

        assert resp.status_code == 200, resp.text
        stages = _stages(client.get("/api/device/status").json())
        assert stages["pair"]["status"] == "passed"
        assert MAC in bluez.bonded


class TestIdentifyIsShownWhileItRuns:
    def test_identify_runs_as_soon_as_the_link_connects(
        self, client: TestClient, module: FakeRfcommModule
    ) -> None:
        module.answering.clear()
        connecting = threading.Thread(
            target=client.post,
            args=("/api/device/connect",),
            kwargs={"json": BLUETOOTH},
        )
        connecting.start()
        try:
            assert module.heard.wait(timeout=5), "the receiver was never polled"

            status = client.get("/api/device/status").json()

            stages = _stages(status)
            assert stages["connect"]["status"] == "passed"
            assert stages["identify"]["status"] == "running"
        finally:
            module.answering.set()
            connecting.join(timeout=15)
        stages = _stages(client.get("/api/device/status").json())
        assert stages["identify"]["status"] == "passed"

    def test_the_status_is_still_connecting_while_the_console_port_is_asked(
        self, client: TestClient, receiver: SimulatedUblox
    ) -> None:
        # The bench (rtk_development#46): the status read "Connected ...
        # console port unknown" for ~2.6 s while Identify still ran.
        asked, answer = threading.Event(), threading.Event()

        def hold_mon_comms() -> None:
            asked.set()
            answer.wait(timeout=5)

        receiver.before_mon_comms = hold_mon_comms
        connecting = threading.Thread(
            target=client.post,
            args=("/api/device/connect",),
            kwargs={"json": BLUETOOTH},
        )
        connecting.start()
        try:
            assert asked.wait(timeout=10), "the console port was never asked"

            status = client.get("/api/device/status").json()

            assert status["state"] == "connecting"
            assert _stages(status)["identify"]["status"] == "running"
        finally:
            answer.set()
            connecting.join(timeout=15)
        status = client.get("/api/device/status").json()
        assert status["state"] == "connected"
        assert status["console_port"] == "UART2"


class TestAFailedConnectIsRedOnItsStage:
    def test_a_refused_pin_is_red_at_pair(
        self,
        client: TestClient,
        bluez: FakeBlueZ,
        input_profile: list[InputProfile | None],
    ) -> None:
        bluez.bonded.clear()
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
        assert bluez.closed == 1, "the manager was not closed"

    def test_a_module_that_does_not_answer_is_red_at_connect(
        self,
        client: TestClient,
        module: FakeRfcommModule,
        bluez: FakeBlueZ,
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
        assert bluez.closed == 1

    def test_garbage_bytes_are_red_at_identify_and_end_on_the_read_limit(
        self,
        client: TestClient,
        module: FakeRfcommModule,
        bluez: FakeBlueZ,
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
        assert bluez.log == [("disconnect", MAC), ("close", 0)]

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
        bluez: FakeBlueZ,
    ) -> None:
        service.set_relay_check(lambda: True)

        resp = client.post("/api/device/connect", json=BLUETOOTH)

        assert resp.status_code == 409
        assert "relay" in resp.json()["detail"].lower()
        assert module.connects == 0
        assert bluez.disconnects == []
        assert bluez.closed == 0


class TestALinkClosedMidSessionIsALostDevice:
    def test_the_module_closing_the_link_loses_the_device(
        self,
        client: TestClient,
        module: FakeRfcommModule,
        bluez: FakeBlueZ,
    ) -> None:
        client.post("/api/device/connect", json=BLUETOOTH)

        module.drop()

        status = client.get("/api/device/status").json()
        assert status["state"] == "disconnected"
        assert status["link"] is None
        assert "lost" in status["last_error"].lower()
        # Torn down in ADR 0002's order, once.
        assert bluez.log == [("disconnect", MAC), ("close", 0)]

    @pytest.mark.asyncio()
    async def test_the_teardown_runs_off_the_event_loop_after_the_disconnect_hooks(
        self, service: DeviceService, module: FakeRfcommModule, bluez: FakeBlueZ
    ) -> None:
        hooks: list[str] = []

        async def before_disconnect() -> None:
            hooks.append("ran")

        service.add_before_disconnect(before_disconnect)
        service.set_driver(create_driver("ublox"))
        await service.connect(BluetoothLink())
        bluez.dbus_free.clear()  # BlueZ is slow to answer Device1.Disconnect
        module.drop()

        started = time.monotonic()
        status = service.get_status()
        took = time.monotonic() - started

        assert took < 1, "the status read waited on BlueZ"
        assert status.state is DeviceConnectionState.DISCONNECTED
        assert status.last_error is not None and "lost" in status.last_error.lower()
        bluez.dbus_free.set()
        for _ in range(100):
            if bluez.closed:
                break
            await asyncio.sleep(0.05)
        assert hooks == ["ran"]
        assert bluez.log == [("disconnect", MAC), ("close", 0)]

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
        bluez: FakeBlueZ,
    ) -> None:
        client.post("/api/device/connect", json=BLUETOOTH)
        # Only a fresh probe after the reset can see this: the port the
        # receiver now hears the console on.
        receiver.console_port_id = UART1_PORT_ID

        resp = client.post("/api/device/reset")

        assert resp.status_code == 200, resp.text
        assert 0x00 in receiver.resets
        assert module.connects == 2, "the link was not closed and reopened"
        # The first link was released before the reopen, and the reopen
        # went through the session's one manager, still open.
        assert bluez.log == [("disconnect", MAC)]
        assert len(bluez.managers) == 1
        assert bluez.live == 1
        status = client.get("/api/device/status").json()
        assert status["state"] == "connected"
        assert status["link"]["kind"] == "bluetooth"
        assert status["console_port"] == "UART1"

    def test_cancel_resets_and_identifies_the_console_port_again(
        self, client: TestClient, receiver: SimulatedUblox
    ) -> None:
        client.post("/api/device/connect", json=BLUETOOTH)
        client.post("/api/device/configure/survey-in", json=SURVEY)
        receiver.console_port_id = UART1_PORT_ID

        resp = client.post("/api/device/cancel-survey-in")

        assert resp.status_code == 200, resp.text
        assert 0x00 in receiver.resets
        assert client.get("/api/device/status").json()["console_port"] == "UART1"

    def test_a_failed_start_resets_and_identifies_the_console_port_again(
        self, client: TestClient, receiver: SimulatedUblox
    ) -> None:
        client.post("/api/device/connect", json=BLUETOOTH)
        receiver.survey_engine_stalls = True
        receiver.console_port_id = UART1_PORT_ID

        resp = client.post("/api/device/configure/survey-in", json=SURVEY)

        assert resp.status_code >= 400
        assert 0x00 in receiver.resets
        assert client.get("/api/device/status").json()["console_port"] == "UART1"

    def test_a_start_that_resets_first_identifies_the_console_port_again(
        self, client: TestClient, receiver: SimulatedUblox
    ) -> None:
        client.post("/api/device/connect", json=BLUETOOTH)
        # A finished survey's accumulator: Start resets the receiver first.
        receiver.svin_dur = 120
        receiver.console_port_id = UART1_PORT_ID

        resp = client.post("/api/device/configure/survey-in", json=SURVEY)

        assert resp.status_code == 200, resp.text
        assert 0x00 in receiver.resets
        assert client.get("/api/device/status").json()["console_port"] == "UART1"

    def test_disconnect_after_a_reset_tears_down_the_reopened_link(
        self, client: TestClient, bluez: FakeBlueZ
    ) -> None:
        client.post("/api/device/connect", json=BLUETOOTH)
        client.post("/api/device/reset")

        resp = client.post("/api/device/disconnect")

        assert resp.status_code == 200, resp.text
        # One manager for the whole session, closed last.
        assert bluez.log == [
            ("disconnect", MAC),
            ("disconnect", MAC),
            ("close", 0),
        ]
        assert len(bluez.managers) == 1
        assert bluez.live == 0

    def test_a_status_read_while_disconnect_waits_on_bluez_leaves_the_teardown_alone(
        self, client: TestClient, bluez: FakeBlueZ
    ) -> None:
        # The bench (rtk_development#46): while Device1.Disconnect waited on
        # BlueZ, a status read saw the closing link as a lost device and
        # closed the session's manager under the call, which then never
        # returned (60 s D-Bus timeout).
        client.post("/api/device/connect", json=BLUETOOTH)
        client.post("/api/device/reset")
        bluez.dbus_free.clear()  # BlueZ is slow to answer Device1.Disconnect
        disconnecting = threading.Thread(
            target=client.post, args=("/api/device/disconnect",)
        )
        disconnecting.start()
        try:
            assert bluez.disconnect_waiting.wait(timeout=5), "Disconnect never ran"

            status = client.get("/api/device/status").json()

            assert "lost" not in (status["last_error"] or "").lower()
        finally:
            bluez.dbus_free.set()
            disconnecting.join(timeout=10)
        # The reset's link, then the last link, then the manager, last.
        assert bluez.log == [
            ("disconnect", MAC),
            ("disconnect", MAC),
            ("close", 0),
        ]
        status = client.get("/api/device/status").json()
        assert status["state"] == "disconnected"
        assert status["last_error"] is None


class TestHandOffOverBluetooth:
    """``POST /api/device/handoff`` with the console on Bluetooth (#44)."""

    @pytest.fixture()
    def config(
        self, tmp_path: Path, input_profile: list[InputProfile | None]
    ) -> ConfigService:
        cfg = ConfigService(config_path=tmp_path / "config.yaml")
        saved = input_profile[0]
        assert saved is not None
        cfg.save_input_config(saved)
        cfg.save_destination(
            DestinationProfile(name="out", type="tcp_server", config={"port": 9000})
        )
        return cfg

    @pytest.fixture()
    def relay(self, config: ConfigService) -> MagicMock:
        svc = MagicMock(spec=RelayService)
        svc.is_running = False
        svc.start_relay = AsyncMock()
        return with_saved_start(svc, config)

    @pytest.fixture()
    def handoff_client(
        self, client: TestClient, config: ConfigService, relay: MagicMock
    ) -> TestClient:
        app = client.app
        assert isinstance(app, FastAPI)
        app.dependency_overrides[get_config_service] = lambda: config
        app.dependency_overrides[get_relay_service] = lambda: relay
        return client

    def test_disconnects_the_console_and_starts_the_relay_on_the_bluetooth_input(
        self,
        handoff_client: TestClient,
        config: ConfigService,
        relay: MagicMock,
        bluez: FakeBlueZ,
    ) -> None:
        before = config.get_input_config()
        assert before is not None
        config.save_device_profile(DeviceProfile(port="/dev/ttyUSB1", baud_rate=38400))
        handoff_client.post("/api/device/connect", json=BLUETOOTH)

        resp = handoff_client.post("/api/device/handoff")

        assert resp.status_code == 200, resp.text
        status = handoff_client.get("/api/device/status").json()
        assert status["state"] == "disconnected"
        # ADR 0002's teardown, once.
        assert bluez.log == [("disconnect", MAC), ("close", 0)]
        relay.start_relay.assert_awaited_once()
        relay_input = relay.start_relay.call_args.args[0]
        assert relay_input == before.to_relay_config()
        assert config.get_input_config() == before
        # The Connect panel reopens on Bluetooth, and still remembers the
        # cable's port and baud for the serial side.
        remembered = config.get_device_profile()
        assert remembered is not None
        assert remembered.kind == "bluetooth"
        assert (remembered.port, remembered.baud_rate) == ("/dev/ttyUSB1", 38400)

    def test_remembers_the_driver_by_its_key_for_the_connect_panel(
        self, handoff_client: TestClient, config: ConfigService
    ) -> None:
        # The bench (rtk_development#46) saved "u-blox", the driver's
        # display name, and the panel's Driver select then loaded blank.
        handoff_client.post("/api/device/connect", json=BLUETOOTH)

        resp = handoff_client.post("/api/device/handoff")

        assert resp.status_code == 200, resp.text
        remembered = config.get_device_profile()
        assert remembered is not None
        assert remembered.vendor == "ublox"

    def test_destinations_that_cannot_run_are_refused_and_the_console_stays(
        self,
        handoff_client: TestClient,
        config: ConfigService,
        relay: MagicMock,
        bluez: FakeBlueZ,
    ) -> None:
        before = config.get_input_config()
        config.save_destination(
            DestinationProfile(
                name="rtk2go",
                type="ntrip",
                config={
                    "caster": "rtk2go.com",
                    "mountpoint": "MP1",
                    "password": "secret",
                    "version": "2.0",
                },
            )
        )
        handoff_client.post("/api/device/connect", json=BLUETOOTH)

        resp = handoff_client.post("/api/device/handoff")

        assert resp.status_code == 422
        assert "rtk2go" in resp.json()["detail"]
        status = handoff_client.get("/api/device/status").json()
        assert status["state"] == "connected"
        assert status["link"]["kind"] == "bluetooth"
        assert bluez.closed == 0
        relay.start_relay.assert_not_called()
        assert config.get_input_config() == before
