"""The Console link through the device API (sp-rtk-base, rtk_development#40).

Driven through ``/api/device`` with the real ``DeviceService`` and the real
u-blox driver behind it, the driver's serial port opening onto a simulated
receiver. Each test states what an API client sees: whether Connect worked,
and the ``link`` the status reports.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.services.device_service import DeviceService
from tests.fixtures.simulated_ublox import SimulatedSerial, SimulatedUblox


@pytest.fixture()
def receiver() -> SimulatedUblox:
    return SimulatedUblox()


@pytest.fixture()
def opened(receiver: SimulatedUblox) -> Iterator[list[dict[str, Any]]]:
    """Every host serial open, as the keyword arguments it was opened with."""
    opens: list[dict[str, Any]] = []

    def _open(**kwargs: Any) -> SimulatedSerial:
        opens.append(kwargs)
        return SimulatedSerial(receiver, **kwargs)

    with (
        patch("serial.Serial", side_effect=_open),
        patch("fcntl.flock"),
        patch("sp_rtk_base.services.drivers.ublox.time.sleep"),
    ):
        yield opens


@pytest.fixture()
def client(opened: list[dict[str, Any]]) -> Iterator[TestClient]:
    from sp_rtk_base.services import get_device_service

    svc = DeviceService()
    app = create_api_app()
    app.dependency_overrides[get_device_service] = lambda: svc
    yield TestClient(app)
    svc_driver = svc.driver
    if svc_driver is not None:
        svc_driver.disconnect()


SERIAL_LINK = {"kind": "serial", "port": "/dev/sim", "baud_rate": 57600}


class TestConnectWithALink:
    def test_a_serial_link_object_connects(self, client: TestClient) -> None:
        resp = client.post(
            "/api/device/connect", json={"vendor": "ublox", "link": SERIAL_LINK}
        )

        assert resp.status_code == 200, resp.text
        status = client.get("/api/device/status").json()
        assert status["state"] == "connected"
        assert status["link"] == SERIAL_LINK
        assert status["port"] == "/dev/sim"
        assert status["baud_rate"] == 57600

    def test_the_flat_port_and_baud_still_connect_as_a_serial_link(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/device/connect",
            json={"vendor": "ublox", "port": "/dev/sim", "baud_rate": 57600},
        )

        assert resp.status_code == 200, resp.text
        status = client.get("/api/device/status").json()
        assert status["link"] == SERIAL_LINK
        assert status["port"] == "/dev/sim"
        assert status["baud_rate"] == 57600

    def test_a_link_and_a_flat_port_together_are_refused(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/device/connect",
            json={"vendor": "ublox", "link": SERIAL_LINK, "port": "/dev/other"},
        )

        assert resp.status_code == 422
        assert client.get("/api/device/status").json()["state"] == "disconnected"

    def test_neither_a_link_nor_a_port_is_refused(self, client: TestClient) -> None:
        resp = client.post("/api/device/connect", json={"vendor": "ublox"})

        assert resp.status_code == 422

    def test_an_unknown_link_kind_is_refused(self, client: TestClient) -> None:
        resp = client.post(
            "/api/device/connect",
            json={"vendor": "ublox", "link": {"kind": "carrier-pigeon"}},
        )

        assert resp.status_code == 422


class TestStatusLink:
    def test_no_link_is_reported_while_disconnected(self, client: TestClient) -> None:
        status = client.get("/api/device/status").json()

        assert status["link"] is None

    def test_disconnect_clears_the_link(self, client: TestClient) -> None:
        client.post(
            "/api/device/connect", json={"vendor": "ublox", "link": SERIAL_LINK}
        )

        client.post("/api/device/disconnect")

        assert client.get("/api/device/status").json()["link"] is None


class TestResetReopensTheLink:
    def test_reset_gps_reopens_the_same_serial_link(
        self,
        client: TestClient,
        receiver: SimulatedUblox,
        opened: list[dict[str, Any]],
    ) -> None:
        client.post(
            "/api/device/connect", json={"vendor": "ublox", "link": SERIAL_LINK}
        )

        resp = client.post("/api/device/reset")

        assert resp.status_code == 200, resp.text
        assert receiver.resets, "the receiver was never reset"
        reopened = opened[-1]
        assert (reopened["port"], reopened["baudrate"]) == ("/dev/sim", 57600)
        status = client.get("/api/device/status").json()
        assert status["state"] == "connected"
        assert status["link"] == SERIAL_LINK
