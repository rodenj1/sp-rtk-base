"""Tests for the cancel-survey-in feature.

Covers the full vertical slice added on 2026-05-27:

- ``UbloxDriver.disable_base_mode()`` writes ``CFG_TMODE_MODE=0``.
- ``UbloxDriver.configure_survey_in()`` now writes ``CFG_TMODE_MODE=0``
  *first*, then the new survey params, so the receiver actually
  restarts the survey when the previous TMODE was non-zero.
- ``FakeGpsDriver.disable_base_mode()`` resets the in-memory state.
- ``DeviceService.cancel_survey_in()`` delegates to the driver.
- ``POST /api/device/cancel-survey-in`` returns ``ok`` on success and
  ``409`` when the device is disconnected or the relay is running.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sp_rtk_base.app import create_api_app
from sp_rtk_base.models.device_models import (
    BaseMode,
    DeviceConnectionState,
    SurveyInConfig,
)
from sp_rtk_base.services import get_device_service
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.drivers.fake import FakeGpsDriver
from sp_rtk_base.services.drivers.ublox import UbloxDriver

# ---------------------------------------------------------------------------
# Auto-mock fcntl.flock — mock serial objects don't have real file descriptors
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_fcntl() -> object:  # type: ignore[misc]
    """Prevent fcntl.flock from running on mock file descriptors."""
    with patch("sp_rtk_base.services.drivers.ublox.fcntl.flock"):
        yield


def _make_mon_ver() -> SimpleNamespace:
    return SimpleNamespace(
        identity="MON-VER",
        swVersion="EXT CORE 1.00",
        hwVersion="00190000",
        extension_00="FWVER=HPG 1.32",
        extension_01="PROTVER=27.31",
        extension_02="MOD=ZED-F9P",
    )


def _make_ack() -> SimpleNamespace:
    return SimpleNamespace(identity="ACK-ACK")


# ---------------------------------------------------------------------------
# FakeGpsDriver.disable_base_mode
# ---------------------------------------------------------------------------


class TestFakeDriverDisableBaseMode:
    """The fake driver must support disabling base mode for e2e flows."""

    def test_disable_base_mode_resets_state(self) -> None:
        """disable_base_mode() switches mode to DISABLED and clears survey clock."""
        drv = FakeGpsDriver()
        drv.connect("FAKE")
        drv.configure_survey_in(
            SurveyInConfig(min_duration_seconds=60, accuracy_limit_mm=50000)
        )
        # Sanity: survey is active
        assert drv.get_base_config().mode == BaseMode.SURVEY_IN
        progress = drv.get_survey_in_status()
        assert progress.active is True

        # Act
        drv.disable_base_mode()

        # State is now disabled and the next survey-in poll reports inactive.
        bc = drv.get_base_config()
        assert bc.mode == BaseMode.DISABLED
        assert bc.latitude == 0.0
        assert bc.longitude == 0.0
        progress = drv.get_survey_in_status()
        assert progress.active is False
        assert progress.valid is False

    def test_disable_base_mode_requires_connection(self) -> None:
        drv = FakeGpsDriver()
        with pytest.raises(ConnectionError):
            drv.disable_base_mode()


# ---------------------------------------------------------------------------
# UbloxDriver.disable_base_mode + the configure_survey_in TMODE-reset fix
# ---------------------------------------------------------------------------


class TestUbloxDisableBaseMode:
    """Verify the ublox driver writes CFG_TMODE_MODE=0 on disable + reset."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_disable_base_mode_sends_tmode_zero(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        reader = MagicMock()
        reader.read.side_effect = [
            (b"", _make_mon_ver()),
            (b"", _make_ack()),
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        drv = UbloxDriver()
        drv.connect("/dev/ttyUSB0")
        drv.disable_base_mode()

        mock_ubx_msg.config_set.assert_called_once()
        cfg_data = mock_ubx_msg.config_set.call_args[0][2]
        assert cfg_data == [("CFG_TMODE_MODE", 0)]

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_configure_survey_in_writes_disable_first(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """Regression: configure_survey_in must reset TMODE before writing
        new params.  Without this, a receiver previously flashed with
        TMODE=2 (fixed) silently ignores the new survey-in request and
        the operator sees the UI stuck on "Idle / 0 mm" forever."""
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        reader = MagicMock()
        reader.read.side_effect = [
            (b"", _make_mon_ver()),
            (b"", _make_ack()),  # disable step
            (b"", _make_ack()),  # enable step
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        drv = UbloxDriver()
        drv.connect("/dev/ttyUSB0")
        drv.configure_survey_in(
            SurveyInConfig(min_duration_seconds=120, accuracy_limit_mm=50000)
        )

        # The first config_set must be the disable, the second must enable
        assert mock_ubx_msg.config_set.call_count == 2
        first = mock_ubx_msg.config_set.call_args_list[0][0][2]
        second = mock_ubx_msg.config_set.call_args_list[1][0][2]
        assert first == [("CFG_TMODE_MODE", 0)]
        # The second call sets the new survey params + re-enables TMODE
        keys = {k: v for k, v in second}
        assert keys.get("CFG_TMODE_MODE") == 1
        assert keys.get("CFG_TMODE_SVIN_MIN_DUR") == 120
        assert keys.get("CFG_TMODE_SVIN_ACC_LIMIT") == 50000

    def test_disable_base_mode_when_disconnected(self) -> None:
        drv = UbloxDriver()
        with pytest.raises(ConnectionError, match="Not connected"):
            drv.disable_base_mode()


# ---------------------------------------------------------------------------
# DeviceService.cancel_survey_in
# ---------------------------------------------------------------------------


class TestDeviceServiceCancelSurveyIn:
    """The service delegates to the driver and handles errors cleanly."""

    @pytest.mark.asyncio
    async def test_cancel_survey_in_delegates_to_driver(self) -> None:
        svc = DeviceService()
        mock_driver = MagicMock()
        mock_driver.vendor_name = "mock"
        mock_driver.is_connected = True
        mock_driver.disable_base_mode = MagicMock()
        svc.set_driver(mock_driver)
        # Manually mark connected (avoid having to mock the full connect path)
        svc._state = DeviceConnectionState.CONNECTED  # type: ignore[attr-defined]

        await svc.cancel_survey_in()

        mock_driver.disable_base_mode.assert_called_once()
        # State must be CONNECTED again after a successful cancel
        assert svc.state == DeviceConnectionState.CONNECTED

    @pytest.mark.asyncio
    async def test_cancel_survey_in_when_not_connected(self) -> None:
        svc = DeviceService()
        with pytest.raises(RuntimeError, match="No GPS driver loaded|not connected"):
            await svc.cancel_survey_in()

    @pytest.mark.asyncio
    async def test_cancel_survey_in_propagates_driver_error(self) -> None:
        svc = DeviceService()
        mock_driver = MagicMock()
        mock_driver.vendor_name = "mock"
        mock_driver.is_connected = True
        mock_driver.disable_base_mode = MagicMock(
            side_effect=RuntimeError("device rejected")
        )
        svc.set_driver(mock_driver)
        svc._state = DeviceConnectionState.CONNECTED  # type: ignore[attr-defined]

        with pytest.raises(RuntimeError, match="device rejected"):
            await svc.cancel_survey_in()
        # State must be restored to CONNECTED so the UI doesn't get stuck
        # in CONFIGURING after a failed cancel.
        assert svc.state == DeviceConnectionState.CONNECTED

    @pytest.mark.asyncio
    async def test_cancel_survey_in_blocked_by_relay(self) -> None:
        svc = DeviceService()
        mock_driver = MagicMock()
        mock_driver.vendor_name = "mock"
        mock_driver.is_connected = True
        svc.set_driver(mock_driver)
        svc._state = DeviceConnectionState.CONNECTED  # type: ignore[attr-defined]
        svc.set_relay_check(lambda: True)

        with pytest.raises(RuntimeError, match="relay is running"):
            await svc.cancel_survey_in()


# ---------------------------------------------------------------------------
# POST /api/device/cancel-survey-in
# ---------------------------------------------------------------------------


class TestCancelSurveyInEndpoint:
    """Smoke tests for the new POST /api/device/cancel-survey-in endpoint.

    We use FastAPI's ``dependency_overrides`` so we never touch the
    real singleton-bound services in ``sp_rtk_base.services``.  This
    matches the pattern used in ``tests/unit/test_api_device.py``.
    """

    def _make_client(self, svc: DeviceService) -> TestClient:
        app = create_api_app()
        app.dependency_overrides[get_device_service] = lambda: svc
        return TestClient(app)

    def test_endpoint_returns_409_when_no_driver(self) -> None:
        svc = DeviceService()
        client = self._make_client(svc)
        resp = client.post("/api/device/cancel-survey-in")
        assert resp.status_code == 409
        detail = resp.json()["detail"].lower()
        assert "no gps" in detail or "not connected" in detail

    def test_endpoint_returns_409_when_relay_running(self) -> None:
        svc = DeviceService()
        fake = FakeGpsDriver()
        fake.connect("FAKE")
        svc.set_driver(fake)
        svc._state = DeviceConnectionState.CONNECTED  # type: ignore[attr-defined]
        svc.set_relay_check(lambda: True)
        client = self._make_client(svc)

        resp = client.post("/api/device/cancel-survey-in")
        assert resp.status_code == 409
        assert "relay" in resp.json()["detail"].lower()

    def test_endpoint_returns_ok_when_connected(self) -> None:
        svc = DeviceService()
        fake = FakeGpsDriver()
        fake.connect("FAKE")
        svc.set_driver(fake)
        svc._state = DeviceConnectionState.CONNECTED  # type: ignore[attr-defined]
        fake.configure_survey_in(
            SurveyInConfig(min_duration_seconds=60, accuracy_limit_mm=50000)
        )
        assert fake.get_base_config().mode == BaseMode.SURVEY_IN

        client = self._make_client(svc)
        resp = client.post("/api/device/cancel-survey-in")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "cancel" in body["message"].lower()
        # The fake driver should now be in DISABLED mode
        assert fake.get_base_config().mode == BaseMode.DISABLED
