"""Start refuses while the console is connected (rtk_development#41).

The console and the Relay exclude each other in both directions: Connect
already refuses while the Relay runs, and Start refuses while the console
is connected, whatever the Console link kind. Over Bluetooth a Start would
otherwise cut the live console link (``ConnectionAbortedError 103`` on the
bench), and it does that in the stale-handle release, so a refused Start
must refuse before that release runs.

Driven through the REST API with a real ``DeviceService`` connected to the
simulated receiver over a (simulated) serial cable, and a real
``RelayService`` whose engine is a stub.
"""

from __future__ import annotations

from collections.abc import Iterator
from importlib import import_module
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import DestinationProfile, InputProfile
from sp_rtk_base.services import (
    get_config_service,
    get_device_service,
    get_relay_service,
    wire_console_relay_exclusion,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.relay_service import RelayService
from tests.fixtures.simulated_ublox import SimulatedSerial, SimulatedUblox

relay_mod = import_module("sp_rtk_base.services.relay_service")
bt_mod = import_module("sp_rtk_base.services.bluetooth_service")

MAC = "AA:BB:CC:DD:EE:FF"


@pytest.fixture()
def released(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """MACs the stale-handle release was run against."""
    seen: list[str] = []

    async def _spy(mac: str, **kwargs: Any) -> None:
        seen.append(mac)

    monkeypatch.setattr(bt_mod, "release_stale_bluetooth_handle", _spy)
    return seen


@pytest.fixture()
def relay(monkeypatch: pytest.MonkeyPatch) -> RelayService:
    svc = RelayService()
    engine = MagicMock()
    engine.is_running = False

    def _start(destinations: Any = None) -> None:
        engine.is_running = True

    engine.start = _start

    def _engine(cfg: object) -> MagicMock:
        return engine

    monkeypatch.setattr(relay_mod, "RelayEngine", _engine)
    return svc


@pytest.fixture()
def config(tmp_path: Any) -> ConfigService:
    cfg = ConfigService(config_path=tmp_path / "config.yaml")
    cfg.save_input_config(
        InputProfile(
            source="bluetooth",
            config={"mac_address": MAC, "pin": "1234"},
        )
    )
    cfg.save_destination(
        DestinationProfile(name="out", type="tcp_server", config={"port": 9000})
    )
    return cfg


@pytest.fixture()
def client(
    relay: RelayService, config: ConfigService
) -> Iterator[tuple[TestClient, DeviceService]]:
    receiver = SimulatedUblox()
    device = DeviceService()
    wire_console_relay_exclusion(device, relay)
    app = create_api_app()
    app.dependency_overrides[get_device_service] = lambda: device
    app.dependency_overrides[get_relay_service] = lambda: relay
    app.dependency_overrides[get_config_service] = lambda: config
    with (
        patch(
            "sp_rtk_base.services.drivers.ublox.serial.Serial",
            side_effect=lambda **kw: SimulatedSerial(receiver, **kw),  # pyright: ignore[reportUnknownLambdaType]
        ),
        patch("sp_rtk_base.services.drivers.ublox.fcntl.flock"),
        patch("sp_rtk_base.services.drivers.ublox.time.sleep"),
    ):
        yield TestClient(app), device
        if device.driver is not None and device.driver.is_connected:
            device.driver.disconnect()


def _connect_console(api: TestClient) -> None:
    resp = api.post(
        "/api/device/connect",
        json={"vendor": "ublox", "port": "/dev/sim", "baud_rate": 38400},
    )
    assert resp.status_code == 200, resp.text


class TestStartWhileConsoleConnected:
    def test_start_is_refused_with_console_connected(
        self,
        client: tuple[TestClient, DeviceService],
        relay: RelayService,
        released: list[str],
    ) -> None:
        api, _ = client
        _connect_console(api)

        resp = api.post("/api/relay/start")

        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "console_connected"
        assert "disconnect the console" in body["message"].lower()
        assert "hand off" in body["message"].lower()
        assert relay.is_running is False
        assert api.get("/api/relay/status").json()["running"] is False

    def test_a_refused_start_does_not_release_the_stale_handle(
        self,
        client: tuple[TestClient, DeviceService],
        released: list[str],
    ) -> None:
        api, _ = client
        _connect_console(api)

        api.post("/api/relay/start")

        assert released == []

    def test_the_console_stays_connected_after_a_refused_start(
        self,
        client: tuple[TestClient, DeviceService],
        released: list[str],
    ) -> None:
        api, _ = client
        _connect_console(api)

        api.post("/api/relay/start")

        assert api.get("/api/device/status").json()["state"] == "connected"

    def test_start_runs_once_the_console_disconnects(
        self,
        client: tuple[TestClient, DeviceService],
        relay: RelayService,
        released: list[str],
    ) -> None:
        api, _ = client
        _connect_console(api)
        assert api.post("/api/device/disconnect").status_code == 200

        resp = api.post("/api/relay/start")

        assert resp.status_code == 200, resp.text
        assert relay.is_running is True
        assert released == [MAC]

    def test_start_without_a_console_behaves_as_before(
        self,
        client: tuple[TestClient, DeviceService],
        relay: RelayService,
        released: list[str],
    ) -> None:
        api, _ = client

        resp = api.post("/api/relay/start")

        assert resp.status_code == 200, resp.text
        assert relay.is_running is True
