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
        # configure_survey_in now performs:
        #   1. CFG-VALSET TMODE=0 (layer=7: RAM+BBR+Flash)  -> ACK
        #   2. CFG-VALSET TMODE=1 + SVIN params (layer=1)   -> ACK
        #   3. NAV-SVIN poll                                -> dur=0
        #   4. (wait ~2s)
        #   5. NAV-SVIN poll                                -> dur=2 (incremented)
        reader.read.side_effect = [
            (b"", _make_mon_ver()),
            (b"", _make_ack()),  # full-layer disable
            (b"", _make_ack()),  # enable
            (
                b"",
                SimpleNamespace(
                    identity="NAV-SVIN",
                    valid=0,
                    active=0,
                    dur=0,
                    meanAcc=0,
                    obs=0,
                ),
            ),
            (
                b"",
                SimpleNamespace(
                    identity="NAV-SVIN",
                    valid=0,
                    active=0,  # HPG 1.12: stays False even while surveying
                    dur=2,
                    meanAcc=99999,
                    obs=1,
                ),
            ),
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        drv = UbloxDriver()
        drv.connect("/dev/ttyUSB0")
        with patch("sp_rtk_base.services.drivers.ublox.time.sleep"):
            drv.configure_survey_in(
                SurveyInConfig(min_duration_seconds=120, accuracy_limit_mm=50000)
            )

        # Two config_sets: full-layer disable, then RAM-only enable.
        assert mock_ubx_msg.config_set.call_count == 2
        disable = mock_ubx_msg.config_set.call_args_list[0]
        enable = mock_ubx_msg.config_set.call_args_list[1]
        assert disable[0][2] == [("CFG_TMODE_MODE", 0)]
        # layer=7 = RAM | BBR | Flash, per u-blox C099 reference script.
        # BBR coverage is the critical bit — RAM+Flash alone leaves
        # ``dur`` ticking from a stale prior session.
        assert disable[0][0] == 7
        # The enable call writes to RAM only (survey-in is intentionally
        # not persisted) and contains the new survey params.
        assert enable[0][0] == 1
        keys = {k: v for k, v in enable[0][2]}
        assert keys.get("CFG_TMODE_MODE") == 1
        assert keys.get("CFG_TMODE_SVIN_MIN_DUR") == 120
        assert keys.get("CFG_TMODE_SVIN_ACC_LIMIT") == 50000

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_configure_survey_in_raises_and_rolls_back_when_dur_doesnt_advance(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """If NAV-SVIN ``dur`` doesn't increment between the two
        verify polls, configure_survey_in must raise *and* roll the
        receiver back to TMODE=0 across all layers so a failed start
        doesn't leave the receiver pinned in survey-in mode (the
        v0.3.3 regression observed on larson-base.lan)."""
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        # Both NAV-SVIN polls return the same dur — the survey
        # didn't actually start, mirroring the HPG 1.12 + stuck-BBR
        # symptom we hit on the real device.
        nav_svin_idle = SimpleNamespace(
            identity="NAV-SVIN",
            valid=0,
            active=0,
            dur=0,
            meanAcc=0,
            obs=0,
        )
        reader = MagicMock()
        # disable ACK → enable ACK → NAV-SVIN(dur=0) → NAV-SVIN(dur=0)
        # → rollback disable ACK
        reader.read.side_effect = [
            (b"", _make_mon_ver()),
            (b"", _make_ack()),  # full-layer disable
            (b"", _make_ack()),  # enable
            (b"", nav_svin_idle),  # before-snapshot
            (b"", nav_svin_idle),  # after-snapshot, dur unchanged
            (b"", _make_ack()),  # rollback layer=7 disable
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        drv = UbloxDriver()
        drv.connect("/dev/ttyUSB0")
        with patch("sp_rtk_base.services.drivers.ublox.time.sleep"):
            with pytest.raises(RuntimeError, match="dur did not advance"):
                drv.configure_survey_in(
                    SurveyInConfig(min_duration_seconds=60, accuracy_limit_mm=50000)
                )

        # Three CFG-VALSETs: initial layer=7 disable, layer=1 enable,
        # then layer=7 rollback after dur failed to advance.
        assert mock_ubx_msg.config_set.call_count == 3
        layers = [c[0][0] for c in mock_ubx_msg.config_set.call_args_list]
        assert layers == [7, 1, 7]
        # First and last (rollback) payloads must be the disable key.
        assert mock_ubx_msg.config_set.call_args_list[0][0][2] == [
            ("CFG_TMODE_MODE", 0)
        ]
        assert mock_ubx_msg.config_set.call_args_list[2][0][2] == [
            ("CFG_TMODE_MODE", 0)
        ]

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_configure_survey_in_raises_when_dur_floor_exceeded(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """If the first NAV-SVIN poll reports dur >= 30 s, the CFG-RST
        didn't actually clear the BBR-backed accumulator and we are
        not running a fresh survey.  configure_survey_in must surface
        a clear actionable error mentioning the physical power-cycle
        workaround (the symptom we observed on larson-base before
        v0.3.5)."""
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        # First poll already reports 17 hours of accumulator — same
        # number we saw on the real device.  Second poll is +2 s
        # later, so dur is still increasing (would pass the
        # progression-only check from v0.3.4) but the floor catches
        # it.
        nav_svin_stale_before = SimpleNamespace(
            identity="NAV-SVIN",
            valid=0,
            active=0,
            dur=61955,
            meanAcc=6622,
            obs=45805,
        )
        nav_svin_stale_after = SimpleNamespace(
            identity="NAV-SVIN",
            valid=0,
            active=0,
            dur=61957,
            meanAcc=6622,
            obs=45806,
        )
        reader = MagicMock()
        reader.read.side_effect = [
            (b"", _make_mon_ver()),
            (b"", _make_ack()),  # layer=7 disable
            (b"", _make_ack()),  # enable
            (b"", nav_svin_stale_before),
            (b"", nav_svin_stale_after),
            (b"", _make_ack()),  # rollback layer=7
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        drv = UbloxDriver()
        drv.connect("/dev/ttyUSB0")
        with patch("sp_rtk_base.services.drivers.ublox.time.sleep"):
            with pytest.raises(
                RuntimeError, match="accumulator did not reset"
            ) as exc_info:
                drv.configure_survey_in(
                    SurveyInConfig(min_duration_seconds=60, accuracy_limit_mm=50000)
                )
        # Error message must include the operator-actionable bit
        # (physical power cycle) so the UI surfaces it directly.
        assert "power cycle" in str(exc_info.value).lower()
        # Rollback must still fire — receiver was just put into
        # TMODE=1 by the enable VALSET, even if dur shows stale.
        assert mock_ubx_msg.config_set.call_count == 3
        layers = [c[0][0] for c in mock_ubx_msg.config_set.call_args_list]
        assert layers == [7, 1, 7]

    def test_disable_base_mode_when_disconnected(self) -> None:
        drv = UbloxDriver()
        with pytest.raises(ConnectionError, match="Not connected"):
            drv.disable_base_mode()

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_disable_base_mode_drains_rx_buffer(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """The driver must drain the serial RX buffer before writing
        CFG-VALSET so the ACK isn't buried behind RTCM/NAV-PVT traffic
        that a busy base station receiver continuously streams.

        Regression: without this drain, ``_wait_for_ack`` could exhaust
        its 50-iteration cap before the ACK arrived, raising
        ``RuntimeError("No ACK/NAK response …")`` while the receiver
        had actually applied the config.  See memory-bank/progress.md
        2026-05-27 "Cancel Survey-In doesn't cancel" entry.
        """
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        reader = MagicMock()
        # MON-VER then ACK then NAV-SVIN(active=False, valid=False)
        reader.read.side_effect = [
            (b"", _make_mon_ver()),
            (b"", _make_ack()),
            (
                b"",
                SimpleNamespace(
                    identity="NAV-SVIN",
                    valid=0,
                    active=0,
                    dur=0,
                    meanAcc=0,
                    obs=0,
                ),
            ),
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        drv = UbloxDriver()
        drv.connect("/dev/ttyUSB0")
        # Reset counters so we only see calls from disable_base_mode
        ser.reset_input_buffer.reset_mock()
        ser.write.reset_mock()

        drv.disable_base_mode()

        # reset_input_buffer must be called at least once *before* the
        # CFG-VALSET write, and again before the NAV-SVIN verify poll.
        assert ser.reset_input_buffer.call_count >= 1
        assert ser.write.call_count >= 1

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_disable_base_mode_retries_when_still_active(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """If the first CFG-VALSET ACK is acknowledged but the receiver
        is still reporting ``active=True`` on NAV-SVIN, the driver must
        retry the VALSET once.  Second attempt succeeds → no error.
        """
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        reader = MagicMock()
        # Sequence:
        #   MON-VER → connect
        #   ACK     → first VALSET
        #   NAV-SVIN active=True → trigger retry
        #   ACK     → second VALSET
        #   NAV-SVIN active=False → success
        reader.read.side_effect = [
            (b"", _make_mon_ver()),
            (b"", _make_ack()),
            (
                b"",
                SimpleNamespace(
                    identity="NAV-SVIN",
                    valid=0,
                    active=1,
                    dur=5,
                    meanAcc=0,
                    obs=0,
                ),
            ),
            (b"", _make_ack()),
            (
                b"",
                SimpleNamespace(
                    identity="NAV-SVIN",
                    valid=0,
                    active=0,
                    dur=5,
                    meanAcc=0,
                    obs=0,
                ),
            ),
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        drv = UbloxDriver()
        drv.connect("/dev/ttyUSB0")
        drv.disable_base_mode()  # should not raise

        # Two CFG-VALSET writes (initial + retry)
        assert mock_ubx_msg.config_set.call_count == 2
        for call in mock_ubx_msg.config_set.call_args_list:
            assert call[0][2] == [("CFG_TMODE_MODE", 0)]

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_disable_base_mode_raises_when_retry_also_fails(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """If both VALSET attempts ACK but NAV-SVIN still reports
        ``active=True``, the driver must raise so the UI can keep the
        Cancel button visible and surface a clear error to the
        operator instead of silently leaving the survey running.
        """
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        # NAV-SVIN always reports active=True even after the second ACK
        reader = MagicMock()
        reader.read.side_effect = [
            (b"", _make_mon_ver()),
            (b"", _make_ack()),
            (
                b"",
                SimpleNamespace(
                    identity="NAV-SVIN",
                    valid=0,
                    active=1,
                    dur=10,
                    meanAcc=0,
                    obs=0,
                ),
            ),
            (b"", _make_ack()),
            (
                b"",
                SimpleNamespace(
                    identity="NAV-SVIN",
                    valid=0,
                    active=1,
                    dur=12,
                    meanAcc=0,
                    obs=0,
                ),
            ),
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        drv = UbloxDriver()
        drv.connect("/dev/ttyUSB0")
        with pytest.raises(RuntimeError, match="Cancel did not take effect"):
            drv.disable_base_mode()

        # Both attempts were made before giving up
        assert mock_ubx_msg.config_set.call_count == 2


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


class TestCfgRstDiagnosticEndpoint:
    """Smoke tests for POST /api/device/debug/cfg-rst.

    Uses an in-memory stub driver that records the args without
    talking to a real receiver — the endpoint's value is in the
    request validation + before/after capture, both of which we can
    exercise without UBX traffic.
    """

    def _make_client(self, svc: DeviceService) -> TestClient:
        app = create_api_app()
        app.dependency_overrides[get_device_service] = lambda: svc
        return TestClient(app)

    def test_endpoint_rejects_disallowed_reset_mode(self) -> None:
        svc = DeviceService()
        fake = FakeGpsDriver()
        fake.connect("FAKE")
        svc.set_driver(fake)
        svc._state = DeviceConnectionState.CONNECTED  # type: ignore[attr-defined]

        client = self._make_client(svc)
        # 0x00 = hardware reset — rejected because it drops the USB.
        resp = client.post(
            "/api/device/debug/cfg-rst",
            json={"reset_mode": 0, "bbr_bits": ["pos"]},
        )
        assert resp.status_code == 400
        assert "reset_mode" in resp.json()["detail"].lower()

    def test_endpoint_rejects_unknown_bbr_bit(self) -> None:
        svc = DeviceService()
        fake = FakeGpsDriver()
        fake.connect("FAKE")
        svc.set_driver(fake)
        svc._state = DeviceConnectionState.CONNECTED  # type: ignore[attr-defined]

        client = self._make_client(svc)
        resp = client.post(
            "/api/device/debug/cfg-rst",
            json={"reset_mode": 2, "bbr_bits": ["nonsense"]},
        )
        assert resp.status_code == 400
        assert "nonsense" in resp.json()["detail"]

    def test_endpoint_rejects_non_ublox_driver(self) -> None:
        """Fake driver doesn't expose send_cfg_rst_diagnostic — the
        service must surface a clear 409 rather than crash."""
        svc = DeviceService()
        fake = FakeGpsDriver()
        fake.connect("FAKE")
        svc.set_driver(fake)
        svc._state = DeviceConnectionState.CONNECTED  # type: ignore[attr-defined]

        client = self._make_client(svc)
        resp = client.post(
            "/api/device/debug/cfg-rst",
            json={"reset_mode": 2, "bbr_bits": ["pos"], "wait_seconds": 0.0},
        )
        assert resp.status_code == 409
        assert "u-blox" in resp.json()["detail"].lower()

    def test_endpoint_returns_before_and_after_on_ublox(self) -> None:
        """When a u-blox driver is present, the endpoint must call
        through and return both NAV-SVIN snapshots plus the UBX hex."""
        from sp_rtk_base.models.device_models import SurveyInProgress
        from sp_rtk_base.services.drivers.ublox import UbloxDriver

        # Build a UbloxDriver instance with send_cfg_rst_diagnostic
        # mocked so we don't open serial.
        drv = UbloxDriver()
        # Mark connected so _require_connected() passes.
        drv._connected = True  # type: ignore[attr-defined]
        before_state = SurveyInProgress(duration_seconds=65000, observations=40000)
        after_state = SurveyInProgress(duration_seconds=0, observations=0)
        drv.send_cfg_rst_diagnostic = MagicMock(  # type: ignore[method-assign]
            return_value=(before_state, after_state, b"\xb5\x62\x06\x04\x04\x00")
        )

        svc = DeviceService()
        svc._driver = drv  # type: ignore[attr-defined]
        svc._state = DeviceConnectionState.CONNECTED  # type: ignore[attr-defined]

        client = self._make_client(svc)
        resp = client.post(
            "/api/device/debug/cfg-rst",
            json={"reset_mode": 2, "bbr_bits": ["pos", "eph"], "wait_seconds": 1.5},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["before"]["duration_seconds"] == 65000
        assert body["after"]["duration_seconds"] == 0
        assert body["wait_seconds"] == 1.5
        assert body["ubx_sent_hex"] == "b56206040400"

        # And the driver was called with the right kwargs.
        drv.send_cfg_rst_diagnostic.assert_called_once_with(  # type: ignore[attr-defined]
            2, 1.5, {"pos": 1, "eph": 1}
        )
