"""The Module name written back to the saved profile on connect (rtk_development#227).

When the base reaches the module, through a Console link connect or a
Relay start of a Bluetooth Input profile, it reads the **Module name**
BlueZ holds for the MAC and, if it heard one that differs from the saved
profile's and the saved profile's MAC is the one connected, updates the
saved profile's ``device_name``. Nothing else in the profile changes, and
a name that can't be read never fails the connect.

Driven through the HTTP API: ``/api/device/connect`` over a fake RFCOMM
module in front of the simulated receiver, and ``/api/relay/start`` with a
stub engine. BlueZ is the injected fake manager. What a client sees is the
saved Input profile, read back through ``GET /api/input``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sp_rtk_base_relay.core.bluetooth_manager import BluetoothError

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import DestinationProfile, InputProfile
from sp_rtk_base.services import (
    get_config_service,
    get_device_service,
    get_relay_service,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers import create_driver
from sp_rtk_base.services.drivers.bluetooth_link import BluetoothLinkOpener
from sp_rtk_base.services.relay_service import RelayService
from tests.fixtures.fake_rfcomm import (
    DEVICE_NAME,
    MAC,
    PIN,
    FakeBlueZ,
    FakeRfcommModule,
)
from tests.fixtures.simulated_ublox import UART2_PORT_ID, SimulatedUblox

relay_mod = import_module("sp_rtk_base.services.relay_service")
bt_mod = import_module("sp_rtk_base.services.bluetooth_service")

BLUETOOTH = {"vendor": "ublox", "link": {"kind": "bluetooth"}}
OTHER_MAC = "00:1A:7D:DA:71:13"
PROVEN_AT = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def receiver() -> SimulatedUblox:
    sim = SimulatedUblox()
    sim.console_port_id = UART2_PORT_ID
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
def config(tmp_path: Path) -> ConfigService:
    svc = ConfigService(config_path=tmp_path / "config.yaml")
    svc.load_config()
    svc.save_destination(
        DestinationProfile(name="out", type="tcp_server", config={"port": 9000})
    )
    return svc


def _save_profile(
    config: ConfigService, *, mac: str = MAC, device_name: str | None = None
) -> None:
    """Save a Bluetooth Input profile with a Proven PIN record, as on disk."""
    settings: dict[str, Any] = {"mac_address": mac, "pin": PIN, "scan_timeout": 45}
    if device_name is not None:
        settings["device_name"] = device_name
    profile = InputProfile(
        source="bluetooth",
        config=settings,
        proven_pin=PIN,
        pin_proven_at=PROVEN_AT,
    )
    config.save_config(config.get_config().model_copy(update={"input": profile}))


@pytest.fixture()
def device(
    module: FakeRfcommModule, bluez: FakeBlueZ, config: ConfigService
) -> Iterator[DeviceService]:
    opener = partial(
        BluetoothLinkOpener,
        manager_factory=bluez.manager,
        socket_factory=module.new_socket,
        read_limit=1.0,
    )
    svc = DeviceService(
        input_profile=config.get_input_config,
        bluetooth_opener=opener,
        record_module_name=config.record_module_name,
    )
    svc.set_driver(create_driver("ublox"))
    with patch("sp_rtk_base.services.drivers.ublox.time.sleep"):
        yield svc
        if svc.driver is not None:
            svc.driver.disconnect()


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The Relay's engine, a stub that starts and stops."""
    stub = MagicMock()
    stub.is_running = False

    def _start(destinations: Any = None) -> None:
        stub.is_running = True

    def _stop() -> None:
        stub.is_running = False

    stub.start = _start
    stub.stop = _stop

    def _engine(cfg: object) -> MagicMock:
        return stub

    monkeypatch.setattr(relay_mod, "RelayEngine", _engine)

    async def _no_release(mac: str, manager: Any = None) -> None:
        return None

    monkeypatch.setattr(bt_mod, "release_stale_bluetooth_handle", _no_release)
    return stub


@pytest.fixture()
def relay(engine: MagicMock, config: ConfigService, bluez: FakeBlueZ) -> RelayService:
    return RelayService(
        config.get_config,
        bluetooth_manager_factory=bluez.manager,
        record_module_name=config.record_module_name,
    )


@pytest.fixture()
def client(
    device: DeviceService, relay: RelayService, config: ConfigService
) -> TestClient:
    app = create_api_app()
    app.dependency_overrides[get_device_service] = lambda: device
    app.dependency_overrides[get_relay_service] = lambda: relay
    app.dependency_overrides[get_config_service] = lambda: config
    return TestClient(app)


def _connect(client: TestClient) -> None:
    resp = client.post("/api/device/connect", json=BLUETOOTH)
    assert resp.status_code == 200, resp.text


def _start(client: TestClient) -> None:
    resp = client.post("/api/relay/start")
    assert resp.status_code == 200, resp.text


def _saved(client: TestClient) -> dict[str, Any]:
    body = client.get("/api/input").json()
    assert body["configured"], body
    return body["config"]


#: Each way the base reaches the module.
REACH = [pytest.param(_connect, id="console-link"), pytest.param(_start, id="relay")]


class TestFillsInTheModuleName:
    @pytest.mark.parametrize("reach", REACH)
    def test_a_profile_without_a_name_gets_the_name_heard(
        self, client: TestClient, config: ConfigService, reach: Any
    ) -> None:
        _save_profile(config)

        reach(client)

        assert _saved(client)["device_name"] == DEVICE_NAME

    @pytest.mark.parametrize("reach", REACH)
    def test_a_stale_name_is_replaced_by_the_name_heard(
        self, client: TestClient, config: ConfigService, reach: Any
    ) -> None:
        _save_profile(config, device_name="HC-05")

        reach(client)

        assert _saved(client)["device_name"] == DEVICE_NAME


OTHER_MODULE = {
    "source": "bluetooth",
    "config": {"mac_address": OTHER_MAC, "device_name": "OTHER_MODULE", "pin": PIN},
}


class TestNeverWritesOntoAnotherMac:
    def test_a_console_link_connect_leaves_a_profile_saved_for_another_mac_alone(
        self, client: TestClient, config: ConfigService, module: FakeRfcommModule
    ) -> None:
        # The operator saves a different module while the connect runs.
        _save_profile(config)
        module.answering.clear()
        connecting = threading.Thread(target=_connect, args=(client,))
        connecting.start()
        try:
            assert module.heard.wait(timeout=5), "the receiver was never polled"
            resp = client.put("/api/input", json=OTHER_MODULE)
            assert resp.status_code == 200, resp.text
        finally:
            module.answering.set()
            connecting.join(timeout=15)

        saved = _saved(client)
        assert saved["mac_address"] == OTHER_MAC
        assert saved["device_name"] == "OTHER_MODULE"

    def test_a_relay_start_leaves_a_profile_saved_for_another_mac_alone(
        self, client: TestClient, config: ConfigService, engine: MagicMock
    ) -> None:
        _save_profile(config)

        def _start_while_the_operator_saves(destinations: Any = None) -> None:
            resp = client.put("/api/input", json=OTHER_MODULE)
            assert resp.status_code == 200, resp.text
            engine.is_running = True

        engine.start = _start_while_the_operator_saves

        _start(client)

        saved = _saved(client)
        assert saved["mac_address"] == OTHER_MAC
        assert saved["device_name"] == "OTHER_MODULE"


class TestANameThatCantBeReadChangesNothing:
    @pytest.mark.parametrize("reach", REACH)
    def test_a_failed_read_still_connects_and_keeps_the_saved_name(
        self, client: TestClient, config: ConfigService, bluez: FakeBlueZ, reach: Any
    ) -> None:
        _save_profile(config, device_name="HC-05")
        bluez.module_name_error = BluetoothError("org.bluez.Error.Failed")

        reach(client)

        assert _saved(client)["device_name"] == "HC-05"

    @pytest.mark.parametrize("reach", REACH)
    def test_no_name_heard_still_connects_and_keeps_the_saved_name(
        self, client: TestClient, config: ConfigService, bluez: FakeBlueZ, reach: Any
    ) -> None:
        _save_profile(config, device_name="HC-05")
        bluez.module_names.clear()

        reach(client)

        assert _saved(client)["device_name"] == "HC-05"

    def test_the_console_is_connected_after_a_failed_read(
        self, client: TestClient, config: ConfigService, bluez: FakeBlueZ
    ) -> None:
        _save_profile(config)
        bluez.module_name_error = BluetoothError("org.bluez.Error.Failed")

        _connect(client)

        assert client.get("/api/device/status").json()["state"] == "connected"

    def test_the_relay_is_running_after_a_failed_read(
        self,
        client: TestClient,
        config: ConfigService,
        bluez: FakeBlueZ,
        relay: RelayService,
    ) -> None:
        _save_profile(config)
        bluez.module_name_error = BluetoothError("org.bluez.Error.Failed")

        _start(client)

        assert relay.is_running


class TestOnlyTheModuleNameChanges:
    @pytest.mark.parametrize("reach", REACH)
    def test_the_pin_proven_pin_and_other_settings_stay_as_saved(
        self, client: TestClient, config: ConfigService, reach: Any
    ) -> None:
        _save_profile(config, device_name="HC-05")

        reach(client)

        saved = config.get_input_config()
        assert saved is not None
        assert saved.config == {
            "mac_address": MAC,
            "pin": PIN,
            "scan_timeout": 45,
            "device_name": DEVICE_NAME,
        }
        assert saved.proven_pin == PIN
        assert saved.pin_proven_at == PROVEN_AT


class TestTheRelayStartsReadLeavesNoManagerOpen:
    @pytest.mark.parametrize("fails", [False, True], ids=["read", "failed-read"])
    def test_the_manager_built_to_read_the_name_is_closed(
        self, client: TestClient, config: ConfigService, bluez: FakeBlueZ, fails: bool
    ) -> None:
        # While it lives, a manager holds a pairing agent on BlueZ.
        _save_profile(config)
        if fails:
            bluez.module_name_error = BluetoothError("org.bluez.Error.Failed")

        _start(client)

        assert bluez.managers, "no manager was built to read the name"
        assert bluez.live == 0
