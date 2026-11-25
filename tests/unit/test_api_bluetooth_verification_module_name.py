"""A Bluetooth Verification reports the Module name it heard (rtk_development#226).

Driven through ``POST /api/input/bluetooth/test`` with the real
Verification service over the fake BlueZ and the fake RFCOMM module, so
nothing here needs an adapter. The saved profile already holds a Proven
PIN for the module, so the Verification takes the relay's own connect
path without a force-repair.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sp_rtk_base_relay.core.bluetooth_manager import BluetoothError

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.config_models import InputProfile
from sp_rtk_base.services import get_bluetooth_verification_service
from sp_rtk_base.services.bluetooth_service import BluetoothVerificationService
from sp_rtk_base.services.config_service import ConfigService
from tests.fixtures.fake_rfcomm import (
    DEVICE_NAME,
    MAC,
    PIN,
    FakeBlueZ,
    FakeRfcommModule,
)
from tests.fixtures.simulated_ublox import UART2_PORT_ID, SimulatedUblox


class _StoppedRelay:
    is_running = False


@pytest.fixture()
def bluez() -> FakeBlueZ:
    return FakeBlueZ()


@pytest.fixture()
def module() -> Iterator[FakeRfcommModule]:
    receiver = SimulatedUblox()
    receiver.console_port_id = UART2_PORT_ID
    mod = FakeRfcommModule(receiver)
    yield mod
    mod.stop()


@pytest.fixture()
def config(tmp_path: Path) -> ConfigService:
    """A saved Bluetooth Input profile for the module, with no Module name.

    Its PIN is Proven, so a Verification of the same MAC and PIN skips
    the force-repair.
    """
    cfg = ConfigService(config_path=tmp_path / "config.yaml")
    profile = InputProfile(
        source="bluetooth",
        config={"mac_address": MAC, "pin": PIN},
        proven_pin=PIN,
    )
    cfg.save_config(cfg.get_config().model_copy(update={"input": profile}))
    return cfg


@pytest.fixture()
def client(
    bluez: FakeBlueZ, module: FakeRfcommModule, config: ConfigService
) -> TestClient:
    service = BluetoothVerificationService(
        relay_service=_StoppedRelay(),  # type: ignore[arg-type]
        config_service=config,
        manager_factory=bluez.manager,
        socket_factory=module.new_socket,
        data_window_seconds=0.1,
    )
    app = create_api_app()
    app.dependency_overrides[get_bluetooth_verification_service] = lambda: service
    return TestClient(app)


def _verify(client: TestClient) -> dict[str, object]:
    resp = client.post(
        "/api/input/bluetooth/test", json={"mac_address": MAC, "pin": PIN}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestTheVerificationReportsTheModuleName:
    def test_a_green_carries_the_module_name_bluez_reports(
        self, client: TestClient
    ) -> None:
        body = _verify(client)

        assert body["verdict"] == "green"
        assert body["module_name"] == DEVICE_NAME

    def test_the_module_name_is_null_when_bluez_heard_none(
        self, client: TestClient, bluez: FakeBlueZ
    ) -> None:
        bluez.module_names.clear()

        body = _verify(client)

        assert body["verdict"] == "green"
        assert body["module_name"] is None

    def test_failing_to_read_the_name_leaves_the_verdict_alone(
        self, client: TestClient, bluez: FakeBlueZ
    ) -> None:
        heard = _verify(client)
        bluez.module_name_error = BluetoothError(
            "Failed to list devices: org.bluez.Error.Failed"
        )

        unheard = _verify(client)

        assert unheard["module_name"] is None
        assert unheard["verdict"] == heard["verdict"] == "green"
        assert unheard["stages"] == heard["stages"]


class TestTheVerificationLeavesTheSavedProfileAlone:
    def test_a_heard_module_name_is_not_written_to_the_saved_profile(
        self, client: TestClient, config: ConfigService, tmp_path: Path
    ) -> None:
        before = (tmp_path / "config.yaml").read_text()

        assert _verify(client)["module_name"] == DEVICE_NAME

        assert (tmp_path / "config.yaml").read_text() == before
        reloaded = ConfigService(config_path=tmp_path / "config.yaml")
        saved = reloaded.get_input_config()
        assert saved is not None
        assert "device_name" not in saved.config
