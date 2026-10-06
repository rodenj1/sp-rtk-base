"""The baud guard over a Bluetooth Console link (rtk_development#47).

Driven through ``/api/device`` with the real ``DeviceService`` and the real
u-blox driver behind it. Over Bluetooth the link is a fake RFCOMM socket in
front of the simulated receiver (sp-rtk-base#221); over serial the driver's
port opens onto the same receiver. The module is wired to UART2 at
115200, and can't follow a baud change there, so Apply refuses one under
``console_baud_over_bluetooth`` before writing anything.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterator
from functools import partial
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import InputProfile
from sp_rtk_base.services import get_device_service
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.bluetooth_link import BluetoothLinkOpener
from tests.fixtures.fake_rfcomm import (
    DEVICE_NAME,
    MAC,
    PIN,
    FakeBluetoothManager,
    FakeRfcommModule,
)
from tests.fixtures.simulated_ublox import (
    FLASH,
    UART2_PORT_ID,
    SimulatedSerial,
    SimulatedUblox,
)

BLUETOOTH = {"vendor": "ublox", "link": {"kind": "bluetooth"}}
SERIAL = {
    "vendor": "ublox",
    "link": {"kind": "serial", "port": "/dev/sim", "baud_rate": 38400},
}

#: The module's rate: what UART2 runs at on the bench.
MODULE_BAUD = 115200


@pytest.fixture()
def receiver() -> SimulatedUblox:
    """A base whose module is on UART2 at 115200, sending 1005 there."""
    sim = SimulatedUblox()
    sim.console_port_id = UART2_PORT_ID
    sim.store(
        FLASH,
        CFG_UART2_BAUDRATE=MODULE_BAUD,
        CFG_UART1INPROT_UBX=1,
        CFG_UART2INPROT_UBX=1,
        CFG_MSGOUT_RTCM_3X_TYPE1005_UART2=1,
    )
    sim.power_cycle()
    return sim


@pytest.fixture()
def module(receiver: SimulatedUblox) -> Iterator[FakeRfcommModule]:
    mod = FakeRfcommModule(receiver)
    yield mod
    mod.stop()


@pytest.fixture()
def service(
    receiver: SimulatedUblox, module: FakeRfcommModule
) -> Iterator[DeviceService]:
    manager = FakeBluetoothManager()
    opener = partial(
        BluetoothLinkOpener,
        manager_factory=lambda adapter: manager,
        socket_factory=module.new_socket,
        read_limit=1.0,
    )
    profile = InputProfile(
        source="bluetooth",
        config={"mac_address": MAC, "device_name": DEVICE_NAME, "pin": PIN},
    )
    svc = DeviceService(input_profile=lambda: profile, bluetooth_opener=opener)
    with (
        patch(
            "serial.Serial",
            side_effect=lambda **kw: SimulatedSerial(receiver, **kw),  # pyright: ignore[reportUnknownLambdaType]
        ),
        patch("fcntl.flock"),
        patch("sp_rtk_base.services.drivers.ublox.time.sleep"),
    ):
        yield svc
        if svc.driver is not None:
            svc.driver.disconnect()


@pytest.fixture()
def client(service: DeviceService) -> TestClient:
    app = create_api_app()
    app.dependency_overrides[get_device_service] = lambda: service
    return TestClient(app)


def _apply_body(service: DeviceService, **baud: int) -> dict[str, Any]:
    """What the receiver holds now, with the given UART rates changed."""
    read = asyncio.run(service.get_receiver_assertion())
    assertion = read.assertion.model_dump(mode="json", by_alias=True)
    assertion["baud"].update(baud)
    return {"assertion": assertion, "data_link_port": ["UART2"]}


def _connect(client: TestClient, body: dict[str, Any]) -> None:
    resp = client.post("/api/device/connect", json=body)
    assert resp.status_code == 200, resp.text


class TestOverBluetoothWithTheConsolePortKnown:
    def test_a_baud_change_on_the_console_port_is_refused_and_nothing_written(
        self, client: TestClient, service: DeviceService, receiver: SimulatedUblox
    ) -> None:
        _connect(client, BLUETOOTH)
        body = _apply_body(service, uart2=57600)
        before = copy.deepcopy(receiver.layers)

        resp = client.post("/api/device/apply-config", json=body)

        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail.startswith("console_baud_over_bluetooth")
        assert "UART2" in detail
        assert "115200" in detail
        assert "serial cable" in detail
        assert receiver.layers == before

    def test_a_baud_change_on_the_other_uart_goes_through(
        self, client: TestClient, service: DeviceService, receiver: SimulatedUblox
    ) -> None:
        _connect(client, BLUETOOTH)
        body = _apply_body(service, uart1=57600)

        resp = client.post("/api/device/apply-config", json=body)

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
        assert receiver.value("CFG_UART1_BAUDRATE") == 57600
        assert receiver.value("CFG_UART2_BAUDRATE") == MODULE_BAUD


class TestOverBluetoothWithTheConsolePortUnknown:
    @pytest.mark.parametrize("uart", ["uart1", "uart2"])
    def test_a_baud_change_on_either_uart_is_refused_naming_why(
        self,
        client: TestClient,
        service: DeviceService,
        receiver: SimulatedUblox,
        uart: str,
    ) -> None:
        receiver.console_port_id = None  # MON-COMMS is NAKed
        _connect(client, BLUETOOTH)
        status = client.get("/api/device/status").json()
        assert status["console_port"] is None
        reason = status["console_port_unknown_reason"]
        body = _apply_body(service, **{uart: 57600})
        before = copy.deepcopy(receiver.layers)

        resp = client.post("/api/device/apply-config", json=body)

        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail.startswith("console_baud_over_bluetooth")
        assert uart.upper() in detail
        assert f"unknown ({reason})" in detail
        assert receiver.layers == before


class TestOverSerial:
    def test_a_baud_change_on_the_console_port_goes_through(
        self, client: TestClient, service: DeviceService, receiver: SimulatedUblox
    ) -> None:
        _connect(client, SERIAL)
        body = _apply_body(service, uart2=57600)

        resp = client.post("/api/device/apply-config", json=body)

        assert resp.status_code == 200, resp.text
        assert receiver.value("CFG_UART2_BAUDRATE") == 57600
