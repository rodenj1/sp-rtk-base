"""Tests for u-blox GPS receiver driver.

Covers:
- Connection lifecycle (connect, disconnect, reconnect)
- MON-VER polling & parsing
- Survey-in, fixed base, RTCM message configuration
- Save-to-flash
- NAV-SVIN status polling
- ACK/NAK handling
- Serial port discovery
- Registry integration
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sp_rtk_base.models.device_models import (
    BaseMode,
    DeviceCapability,
    DynModel,
    FixedBaseConfig,
    PortId,
    RtcmRowId,
    SurveyInConfig,
    UbxProtocol,
)
from sp_rtk_base.services.drivers.ublox import UbloxDriver

# ---------------------------------------------------------------------------
# Auto-mock fcntl.flock — mock serial objects don't have real file descriptors
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_fcntl() -> object:  # type: ignore[misc]
    """Prevent fcntl.flock from running on mock file descriptors."""
    with patch("sp_rtk_base.services.drivers.ublox.fcntl.flock"):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mon_ver_response() -> SimpleNamespace:
    """Create a mock MON-VER parsed response."""
    return SimpleNamespace(
        identity="MON-VER",
        swVersion="EXT CORE 1.00 (f4c834)",
        hwVersion="00190000",
        extension_00="FWVER=HPG 1.32",
        extension_01="PROTVER=27.31",
        extension_02="MOD=ZED-F9P",
        extension_03="GPS;GLO;GAL;BDS",
        extension_04=None,
    )


def _make_ack_response() -> SimpleNamespace:
    """Create a mock ACK-ACK response."""
    return SimpleNamespace(identity="ACK-ACK")


def _make_nak_response() -> SimpleNamespace:
    """Create a mock ACK-NAK response."""
    return SimpleNamespace(identity="ACK-NAK")


def _make_nav_svin_response(
    active: int = 1,
    valid: int = 0,
    dur: int = 45,
    mean_acc: int = 25000,
    obs: int = 45,
) -> SimpleNamespace:
    """Create a mock NAV-SVIN response."""
    return SimpleNamespace(
        identity="NAV-SVIN",
        active=active,
        valid=valid,
        dur=dur,
        meanAcc=mean_acc,
        obs=obs,
    )


def _make_dyn_model_valget(value: int = 2) -> SimpleNamespace:
    """Create a mock CFG-VALGET response for CFG_NAVSPG_DYNMODEL (issue #38).

    Defaults to 2 (stationary); pass ``value=0`` to simulate a mismatch.
    """
    return SimpleNamespace(identity="CFG-VALGET", CFG_NAVSPG_DYNMODEL=value)


@pytest.fixture()
def mock_serial() -> MagicMock:
    """Create a mock serial.Serial instance."""
    ser = MagicMock()
    ser.is_open = True
    ser.write = MagicMock(return_value=10)
    ser.close = MagicMock()
    return ser


@pytest.fixture()
def mock_reader_factory() -> type:
    """Return a factory that creates mock UBXReader with configurable responses."""

    class MockReaderFactory:
        @staticmethod
        def create(responses: list[SimpleNamespace]) -> MagicMock:
            reader = MagicMock()
            idx = 0

            def read_side_effect() -> tuple[bytes, SimpleNamespace | None]:
                nonlocal idx
                if idx < len(responses):
                    resp = responses[idx]
                    idx += 1
                    return (b"", resp)
                raise StopIteration("No more responses")

            reader.read = MagicMock(side_effect=read_side_effect)
            return reader

    return MockReaderFactory


# ---------------------------------------------------------------------------
# Identity / capabilities
# ---------------------------------------------------------------------------


class TestUbloxDriverIdentity:
    """Test driver identity and capabilities."""

    def test_vendor_name(self) -> None:
        driver = UbloxDriver()
        assert driver.vendor_name == "u-blox"

    def test_capabilities(self) -> None:
        driver = UbloxDriver()
        caps = driver.get_capabilities()
        assert DeviceCapability.SURVEY_IN in caps
        assert DeviceCapability.FIXED_BASE in caps
        assert DeviceCapability.RTCM_MESSAGE_SELECT in caps
        assert DeviceCapability.SAVE_TO_FLASH in caps
        assert DeviceCapability.POSITION_STREAM in caps
        assert DeviceCapability.SATELLITE_INFO in caps
        assert len(caps) == 7

    def test_not_connected_initially(self) -> None:
        driver = UbloxDriver()
        assert driver.is_connected is False


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class TestUbloxDriverConnect:
    """Test connect / disconnect."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_connect_success(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
    ) -> None:
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        reader = MagicMock()
        reader.read.return_value = (b"", _make_mon_ver_response())
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        info = driver.connect("/dev/ttyUSB0", 57600)

        assert info.vendor == "u-blox"
        assert info.model == "ZED-F9P"
        # FWVER=HPG 1.32 → firmware_version = "HPG 1.32"
        assert info.firmware_version == "HPG 1.32"
        assert info.protocol_version == "27.31"
        assert driver.is_connected is True
        mock_serial_cls.assert_called_once_with(
            port="/dev/ttyUSB0",
            baudrate=57600,
            timeout=3.0,
            exclusive=True,
        )

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_connect_reports_confirmed_hardware_identity(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
    ) -> None:
        """MOD=ZED-F9P (tier 1) is a real read, not a guess."""
        from sp_rtk_base.models.hardware_identity import HardwareConfidence

        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        reader = MagicMock()
        reader.read.return_value = (b"", _make_mon_ver_response())
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        info = driver.connect("/dev/ttyUSB0")

        assert info.hardware_target == "ZED-F9P"
        assert info.hardware_confidence == HardwareConfidence.CONFIRMED

    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_connect_serial_exception(self, mock_serial_cls: MagicMock) -> None:
        import serial  # type: ignore[import-untyped]

        mock_serial_cls.side_effect = serial.SerialException("Port busy")

        driver = UbloxDriver()
        with pytest.raises(ConnectionError, match="Failed to open"):
            driver.connect("/dev/ttyUSB0")
        assert driver.is_connected is False

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_connect_already_connected(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
    ) -> None:
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        reader = MagicMock()
        reader.read.return_value = (b"", _make_mon_ver_response())
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")

        with pytest.raises(ConnectionError, match="Already connected"):
            driver.connect("/dev/ttyUSB0")

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_disconnect(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
    ) -> None:
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        reader = MagicMock()
        reader.read.return_value = (b"", _make_mon_ver_response())
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")
        assert driver.is_connected is True

        driver.disconnect()
        assert driver.is_connected is False
        ser.close.assert_called()

    def test_disconnect_when_not_connected(self) -> None:
        driver = UbloxDriver()
        driver.disconnect()  # Should not raise
        assert driver.is_connected is False

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_connect_mon_ver_timeout(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
    ) -> None:
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        reader = MagicMock()
        reader.read.side_effect = Exception("timeout")
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        with pytest.raises(ConnectionError, match="Connection failed"):
            driver.connect("/dev/ttyUSB0")
        assert driver.is_connected is False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestUbloxDriverConfiguration:
    """Test base station configuration methods."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_configure_survey_in(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        # Setup connection
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        reader = MagicMock()
        # configure_survey_in now performs (issue #63 retired the
        # trailing base-output-profile / dyn-model force-applies):
        #   0. NAV-SVIN baseline poll                       -> dur=0 (no pre-reset)
        #   1. CFG-VALSET TMODE=0 (layer=7: RAM+BBR+Flash)  -> ACK
        #   2. CFG-VALSET TMODE=1 + SVIN params (layer=5)   -> ACK
        #   3. CFG-VALGET read-back                         -> matches (issue #42)
        #   4. NAV-SVIN poll                                -> dur=0
        #   5. (~2 s gap)
        #   6. NAV-SVIN poll                                -> dur=3 (incremented)
        reader.read.side_effect = [
            (b"", _make_mon_ver_response()),
            (b"", _make_nav_svin_response(active=0, valid=0, dur=0, obs=0)),  # baseline
            (b"", _make_ack_response()),  # full-layer disable
            (b"", _make_ack_response()),  # enable
            (
                b"",
                SimpleNamespace(
                    identity="CFG-VALGET",
                    CFG_TMODE_SVIN_MIN_DUR=300,
                    CFG_TMODE_SVIN_ACC_LIMIT=400000,
                    CFG_TMODE_MODE=1,
                ),
            ),  # enable read-back — matches
            (b"", _make_nav_svin_response(active=0, valid=0, dur=0, obs=0)),
            (b"", _make_nav_svin_response(active=0, valid=0, dur=3, obs=1)),
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")

        config = SurveyInConfig(min_duration_seconds=300, accuracy_limit_mm=40000)
        with patch("sp_rtk_base.services.drivers.ublox.time.sleep"):
            driver.configure_survey_in(config)

        # Issue #63: only two CFG-VALSET calls now — layer=7 disable and
        # layer=5 enable (issue #42). The base output profile and dyn
        # model force-applies (issues #40/#38) were retired: they used
        # to overwrite an operator-applied profile on every transition.
        assert mock_ubx_msg.config_set.call_count == 2
        disable_layer = mock_ubx_msg.config_set.call_args_list[0][0][0]
        disable_cfg = mock_ubx_msg.config_set.call_args_list[0][0][2]
        enable_layer = mock_ubx_msg.config_set.call_args_list[1][0][0]
        enable_cfg = mock_ubx_msg.config_set.call_args_list[1][0][2]

        # Disable must hit RAM|BBR|Flash (7), per u-blox C099 reference
        # script — RAM-only leaves BBR pinned and the ``dur`` counter
        # accumulating from prior sessions.
        assert disable_layer == 7
        assert disable_cfg == [("CFG_TMODE_MODE", 0)]
        # Enable is RAM+Flash (issue #42) — a RAM-only write reverted
        # to the last-flashed selection on reboot / port reopen.
        assert enable_layer == 5
        keys = [k for k, _ in enable_cfg]
        assert "CFG_TMODE_MODE" in keys
        assert "CFG_TMODE_SVIN_MIN_DUR" in keys
        assert "CFG_TMODE_SVIN_ACC_LIMIT" in keys
        mode_vals = [v for k, v in enable_cfg if k == "CFG_TMODE_MODE"]
        assert mode_vals == [1]
        # CFG_TMODE_SVIN_ACC_LIMIT is in 0.1 mm wire units; the
        # Python API takes mm so we multiply by 10 when sending.
        # accuracy_limit_mm=40000 -> 400000 on the wire.
        acc_vals = [v for k, v in enable_cfg if k == "CFG_TMODE_SVIN_ACC_LIMIT"]
        assert acc_vals == [400000]
        # MIN_DUR is in seconds (no unit conversion).
        dur_vals = [v for k, v in enable_cfg if k == "CFG_TMODE_SVIN_MIN_DUR"]
        assert dur_vals == [300]

        # Issue #63: a survey-in start must not write the base output
        # profile or dynamics model keys at all — those are exactly the
        # keys an operator-applied ``ReceiverConfig`` profile owns, and
        # this used to silently overwrite it on every transition.
        all_keys = {
            k for call in mock_ubx_msg.config_set.call_args_list for k, _ in call[0][2]
        }
        assert "CFG_UART1OUTPROT_NMEA" not in all_keys
        assert "CFG_UART1OUTPROT_RTCM3X" not in all_keys
        assert "CFG_UART2OUTPROT_NMEA" not in all_keys
        assert "CFG_UART2OUTPROT_RTCM3X" not in all_keys
        assert "CFG_NAVSPG_DYNMODEL" not in all_keys
        # UBX input protocols must never be touched — the app manages
        # the receiver over the same link.
        assert "CFG_UART1INPROT_UBX" not in all_keys
        assert "CFG_UART2INPROT_UBX" not in all_keys

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_configure_fixed_base(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        reader = MagicMock()
        # configure_fixed_base now performs (mirroring
        # configure_survey_in's edge-triggered TMODE pattern; issue #63
        # retired the trailing base-output-profile / dyn-model
        # force-applies):
        #   1. UBX-CFG-RST (no ACK read needed)
        #   2. CFG-VALSET TMODE=0 (layer=7)  -> ACK
        #   3. CFG-VALSET TMODE=2 + coords + ECEF (layer=5) -> ACK
        #   4. CFG-VALGET ECEF read-back verify -> matches written ECEF
        # Read-back values are the exact CFG_TMODE_ECEF_X/Y/Z cm the
        # driver computes for lat=47.3977, lon=8.5456, alt=408.0m —
        # the verify step now checks equality, not just non-zero.
        reader.read.side_effect = [
            (b"", _make_mon_ver_response()),
            (b"", _make_ack_response()),  # ACK for layer=7 disable
            (b"", _make_ack_response()),  # ACK for layer=5 fixed-base write
            (
                b"",
                SimpleNamespace(
                    identity="CFG-VALGET",
                    CFG_TMODE_ECEF_X=427750094,
                    CFG_TMODE_ECEF_Y=64275758,
                    CFG_TMODE_ECEF_Z=467210663,
                ),
            ),
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")

        config = FixedBaseConfig(
            latitude=47.3977,
            longitude=8.5456,
            altitude_m=408.0,
            accuracy_mm=500,
        )
        with patch("sp_rtk_base.services.drivers.ublox.time.sleep"):
            driver.configure_fixed_base(config)

        # Issue #63: only two CFG-VALSETs now — layer=7 disable and
        # layer=5 fixed-base. The base output profile and dyn model
        # force-applies (issues #40/#38) were retired: they used to
        # overwrite an operator-applied profile on every transition.
        # Pre-disable mirrors configure_survey_in — without it, a
        # receiver currently in TMODE=1 silently coalesces the
        # TMODE=2 write and stays in survey-in (the Path 2 bug
        # diagnosed on larson-base before v0.3.5).
        assert mock_ubx_msg.config_set.call_count == 2
        disable_layer = mock_ubx_msg.config_set.call_args_list[0][0][0]
        disable_cfg = mock_ubx_msg.config_set.call_args_list[0][0][2]
        fixed_layer = mock_ubx_msg.config_set.call_args_list[1][0][0]
        fixed_cfg = mock_ubx_msg.config_set.call_args_list[1][0][2]

        assert disable_layer == 7
        assert disable_cfg == [("CFG_TMODE_MODE", 0)]
        # configure_fixed_base writes the new TMODE config to RAM+Flash
        # (layer=5) directly, bypassing CFG-CFG which doesn't reliably
        # persist key/value TMODE config on Gen9+ receivers.
        assert fixed_layer == 5
        keys = [k for k, _ in fixed_cfg]
        assert "CFG_TMODE_MODE" in keys
        assert "CFG_TMODE_LAT" in keys
        assert "CFG_TMODE_LON" in keys
        assert "CFG_TMODE_HEIGHT" in keys
        # TMODE=2 (fixed) in the second call
        mode_vals = [v for k, v in fixed_cfg if k == "CFG_TMODE_MODE"]
        assert mode_vals == [2]
        # The ZED-F9P base engine requires a valid (non-origin) ECEF
        # position before it will generate RTCM — LLH alone leaves
        # CFG_TMODE_ECEF_X/Y/Z at their 0,0,0 default. Assert they're
        # written alongside LLH and are non-zero for a non-origin fix.
        for ecef_key in ("CFG_TMODE_ECEF_X", "CFG_TMODE_ECEF_Y", "CFG_TMODE_ECEF_Z"):
            assert ecef_key in keys
        ecef_vals = {k: v for k, v in fixed_cfg if k.startswith("CFG_TMODE_ECEF_")}
        assert ecef_vals["CFG_TMODE_ECEF_X"] != 0
        assert ecef_vals["CFG_TMODE_ECEF_Y"] != 0
        assert ecef_vals["CFG_TMODE_ECEF_Z"] != 0
        # CFG_TMODE_FIXED_POS_ACC is in 0.1 mm wire units; the
        # Python API takes mm so we multiply by 10 when sending.
        # accuracy_mm=500 -> 5000 on the wire.
        acc_vals = [v for k, v in fixed_cfg if k == "CFG_TMODE_FIXED_POS_ACC"]
        assert acc_vals == [5000]

        # Issue #63: a fixed-base transition must not write the base
        # output profile or dynamics model keys at all — those are
        # exactly the keys an operator-applied ``ReceiverConfig``
        # profile owns.
        all_keys = {
            k for call in mock_ubx_msg.config_set.call_args_list for k, _ in call[0][2]
        }
        assert "CFG_UART1OUTPROT_NMEA" not in all_keys
        assert "CFG_UART1OUTPROT_RTCM3X" not in all_keys
        assert "CFG_UART2OUTPROT_NMEA" not in all_keys
        assert "CFG_UART2OUTPROT_RTCM3X" not in all_keys
        assert "CFG_NAVSPG_DYNMODEL" not in all_keys
        # UBX input protocols must never be touched — the app manages
        # the receiver over the same link.
        assert "CFG_UART1INPROT_UBX" not in all_keys
        assert "CFG_UART2INPROT_UBX" not in all_keys

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_configure_fixed_base_raises_when_ecef_stays_zero(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """Issue #39: an ACK'd write that leaves ECEF at 0,0,0 must not be
        reported as success — this is exactly the failure mode where a
        "successfully configured" base emitted zero RTCM frames."""
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        zero_ecef = SimpleNamespace(
            identity="CFG-VALGET",
            CFG_TMODE_ECEF_X=0,
            CFG_TMODE_ECEF_Y=0,
            CFG_TMODE_ECEF_Z=0,
        )
        reader = MagicMock()
        reader.read.side_effect = [
            (b"", _make_mon_ver_response()),
            (b"", _make_ack_response()),  # ACK for layer=7 disable
            (b"", _make_ack_response()),  # ACK for first layer=5 write
            (b"", zero_ecef),  # first read-back — still zero
            (b"", _make_ack_response()),  # ACK for retried layer=5 write
            (b"", zero_ecef),  # second read-back — still zero
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")

        config = FixedBaseConfig(
            latitude=47.3977, longitude=8.5456, altitude_m=408.0, accuracy_mm=500
        )
        with patch("sp_rtk_base.services.drivers.ublox.time.sleep"):
            with pytest.raises(RuntimeError, match="did not take effect"):
                driver.configure_fixed_base(config)

        # Retried once: layer=7 disable + two layer=5 writes.
        assert mock_ubx_msg.config_set.call_count == 3

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_configure_fixed_base_raises_when_ecef_is_stale(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """Issue #39 spec item 2: the read-back must be consistent with the
        LLH just written, not merely non-zero. A *stale* non-zero ECEF left
        over from a previous fixed-base config must still be rejected —
        a bare non-zero check would have passed this and reported success
        with the wrong position."""
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        # Non-zero, but not the ECEF for this config's lat/lon/alt — as
        # if a prior fixed-base session's coordinates were still latched.
        stale_ecef = SimpleNamespace(
            identity="CFG-VALGET",
            CFG_TMODE_ECEF_X=-245790204,
            CFG_TMODE_ECEF_Y=-477512066,
            CFG_TMODE_ECEF_Z=342909332,
        )
        reader = MagicMock()
        reader.read.side_effect = [
            (b"", _make_mon_ver_response()),
            (b"", _make_ack_response()),  # ACK for layer=7 disable
            (b"", _make_ack_response()),  # ACK for first layer=5 write
            (b"", stale_ecef),  # first read-back — stale, wrong position
            (b"", _make_ack_response()),  # ACK for retried layer=5 write
            (b"", stale_ecef),  # second read-back — still stale
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")

        config = FixedBaseConfig(
            latitude=47.3977, longitude=8.5456, altitude_m=408.0, accuracy_mm=500
        )
        with patch("sp_rtk_base.services.drivers.ublox.time.sleep"):
            with pytest.raises(RuntimeError, match="did not take effect"):
                driver.configure_fixed_base(config)

        # Retried once: layer=7 disable + two layer=5 writes.
        assert mock_ubx_msg.config_set.call_count == 3

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_save_to_flash(
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
            (b"", _make_mon_ver_response()),
            (b"", _make_ack_response()),
        ]
        mock_reader_cls.return_value = reader

        mock_msg_instance = MagicMock()
        mock_msg_instance.serialize.return_value = b"\x00"
        mock_ubx_msg.return_value = mock_msg_instance

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")
        driver.save_to_flash()

        # Verify CFG-CFG was sent
        mock_ubx_msg.assert_called()

    def test_configure_when_disconnected(self) -> None:
        driver = UbloxDriver()
        with pytest.raises(ConnectionError, match="Not connected"):
            driver.configure_survey_in(SurveyInConfig())

    def test_save_flash_when_disconnected(self) -> None:
        driver = UbloxDriver()
        with pytest.raises(ConnectionError, match="Not connected"):
            driver.save_to_flash()


# ---------------------------------------------------------------------------
# ACK/NAK handling
# ---------------------------------------------------------------------------


class TestUbloxDriverAckNak:
    """Test ACK/NAK response handling."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_nak_raises_runtime_error(
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
            (b"", _make_mon_ver_response()),
            (b"", _make_nak_response()),
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")

        with pytest.raises(RuntimeError, match="NAK"):
            driver.configure_survey_in(SurveyInConfig())

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_no_ack_raises_runtime_error(
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
            (b"", _make_mon_ver_response()),
            Exception("read error"),
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")

        with pytest.raises(RuntimeError, match="No ACK/NAK"):
            driver.configure_survey_in(SurveyInConfig())


# ---------------------------------------------------------------------------
# Status polling
# ---------------------------------------------------------------------------


class TestUbloxDriverStatus:
    """Test status polling methods."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_get_survey_in_status(
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
            (b"", _make_mon_ver_response()),  # connect
            (
                b"",
                _make_nav_svin_response(
                    active=1, valid=0, dur=45, mean_acc=25000, obs=45
                ),
            ),
        ]
        mock_reader_cls.return_value = reader

        mock_msg_instance = MagicMock()
        mock_msg_instance.serialize.return_value = b"\x00"
        mock_ubx_msg.return_value = mock_msg_instance

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")

        status = driver.get_survey_in_status()
        assert status.active is True
        assert status.valid is False
        assert status.duration_seconds == 45
        assert status.mean_accuracy_mm == 2500.0  # 25000 / 10
        assert status.observations == 45

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_get_survey_in_status_no_response(
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
            (b"", _make_mon_ver_response()),
            Exception("timeout"),
        ]
        mock_reader_cls.return_value = reader

        mock_msg_instance = MagicMock()
        mock_msg_instance.serialize.return_value = b"\x00"
        mock_ubx_msg.return_value = mock_msg_instance

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")

        status = driver.get_survey_in_status()
        assert status.active is False
        assert status.valid is False

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_get_device_info(
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
            (b"", _make_mon_ver_response()),  # connect
            (b"", _make_mon_ver_response()),  # get_device_info
        ]
        mock_reader_cls.return_value = reader

        mock_msg_instance = MagicMock()
        mock_msg_instance.serialize.return_value = b"\x00"
        mock_ubx_msg.return_value = mock_msg_instance

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")
        info = driver.get_device_info()

        assert info.vendor == "u-blox"
        assert info.model == "ZED-F9P"

    def test_get_survey_status_when_disconnected(self) -> None:
        driver = UbloxDriver()
        with pytest.raises(ConnectionError, match="Not connected"):
            driver.get_survey_in_status()


# ---------------------------------------------------------------------------
# Serial port discovery
# ---------------------------------------------------------------------------


class TestSerialPortDiscovery:
    """Test port discovery via GpsReceiverDriver.list_serial_ports()."""

    @patch("serial.tools.list_ports")
    def test_list_serial_ports(self, mock_list_ports: MagicMock) -> None:
        from sp_rtk_base.services.drivers.base import GpsReceiverDriver

        mock_port = SimpleNamespace(
            device="/dev/ttyUSB0",
            description="u-blox AG - u-blox GNSS receiver",
            manufacturer="u-blox AG",
            vid=0x1546,
            pid=0x01A9,
            serial_number="ABC123",
        )
        mock_list_ports.comports.return_value = [mock_port]

        ports = GpsReceiverDriver.list_serial_ports()
        assert len(ports) == 1
        assert ports[0].port == "/dev/ttyUSB0"
        assert ports[0].is_gps is True
        assert ports[0].vid == 0x1546
        assert ports[0].manufacturer == "u-blox AG"

    @patch("serial.tools.list_ports")
    def test_non_gps_port(self, mock_list_ports: MagicMock) -> None:
        from sp_rtk_base.services.drivers.base import GpsReceiverDriver

        mock_port = SimpleNamespace(
            device="/dev/ttyACM0",
            description="Arduino Uno",
            manufacturer="Arduino",
            vid=0x2341,
            pid=0x0043,
            serial_number="",
        )
        mock_list_ports.comports.return_value = [mock_port]

        ports = GpsReceiverDriver.list_serial_ports()
        assert len(ports) == 1
        assert ports[0].is_gps is False

    @patch("serial.tools.list_ports")
    def test_port_with_none_vid(self, mock_list_ports: MagicMock) -> None:
        from sp_rtk_base.services.drivers.base import GpsReceiverDriver

        mock_port = SimpleNamespace(
            device="/dev/ttyS0",
            description="Serial Port",
            manufacturer=None,
            vid=None,
            pid=None,
            serial_number=None,
        )
        mock_list_ports.comports.return_value = [mock_port]

        ports = GpsReceiverDriver.list_serial_ports()
        assert len(ports) == 1
        assert ports[0].is_gps is False
        assert ports[0].manufacturer == ""

    @patch("serial.tools.list_ports")
    def test_empty_ports(self, mock_list_ports: MagicMock) -> None:
        from sp_rtk_base.services.drivers.base import GpsReceiverDriver

        mock_list_ports.comports.return_value = []
        ports = GpsReceiverDriver.list_serial_ports()
        assert ports == []

    @patch("serial.tools.list_ports")
    def test_ftdi_port_is_gps(self, mock_list_ports: MagicMock) -> None:
        from sp_rtk_base.services.drivers.base import GpsReceiverDriver

        mock_port = SimpleNamespace(
            device="/dev/ttyUSB1",
            description="FTDI USB-Serial",
            manufacturer="FTDI",
            vid=0x0403,
            pid=0x6001,
            serial_number="",
        )
        mock_list_ports.comports.return_value = [mock_port]

        ports = GpsReceiverDriver.list_serial_ports()
        assert len(ports) == 1
        assert ports[0].is_gps is True


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestUbloxDriverRegistry:
    """Test that u-blox driver is properly registered."""

    def test_ublox_registered(self) -> None:
        from sp_rtk_base.services.drivers import get_driver_class

        cls = get_driver_class("ublox")
        assert cls is UbloxDriver

    def test_create_ublox_driver(self) -> None:
        from sp_rtk_base.services.drivers import create_driver

        driver = create_driver("ublox")
        assert isinstance(driver, UbloxDriver)
        assert driver.vendor_name == "u-blox"

    def test_ublox_in_list(self) -> None:
        from sp_rtk_base.services.drivers import list_drivers

        assert "ublox" in list_drivers()


# ---------------------------------------------------------------------------
# MON-VER edge cases
# ---------------------------------------------------------------------------


class TestMonVerParsing:
    """Test MON-VER parsing edge cases."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_mon_ver_bytes_sw_version(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        # SW/HW as bytes (older pyubx2 versions)
        mon_ver = SimpleNamespace(
            identity="MON-VER",
            swVersion=b"EXT CORE 1.00\x00\x00",
            hwVersion=b"00190000\x00\x00",
            extension_00="MOD=ZED-F9P",
            extension_01=None,
        )

        reader = MagicMock()
        reader.read.return_value = (b"", mon_ver)
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        info = driver.connect("/dev/ttyUSB0")
        assert info.model == "ZED-F9P"
        assert "CORE" in info.firmware_version

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_mon_ver_model_from_extension(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        # Model detected by prefix match
        mon_ver = SimpleNamespace(
            identity="MON-VER",
            swVersion="ROM BASE 3.01",
            hwVersion="00080000",
            extension_00="NEO-M9N",
            extension_01=None,
        )

        reader = MagicMock()
        reader.read.return_value = (b"", mon_ver)
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        info = driver.connect("/dev/ttyUSB0")
        assert info.model == "NEO-M9N"

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_mon_ver_hw_version_only_is_confirmed(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """No MOD=/explicit model — hwVersion tier-3 lookup still confirms."""
        from sp_rtk_base.models.hardware_identity import HardwareConfidence

        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        mon_ver = SimpleNamespace(
            identity="MON-VER",
            swVersion="EXT CORE 1.00",
            hwVersion="00190000",
            extension_00=None,
        )

        reader = MagicMock()
        reader.read.return_value = (b"", mon_ver)
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        info = driver.connect("/dev/ttyUSB0")
        assert info.model == "ZED-F9P"
        assert info.hardware_target == "ZED-F9P"
        assert info.hardware_confidence == HardwareConfidence.CONFIRMED

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_mon_ver_firmware_heuristic_is_inferred_not_confirmed(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """No MOD=, no explicit model, unrecognised hwVersion — only the
        firmware-family heuristic hits, so the guess must be `inferred`,
        never indistinguishable from a real read."""
        from sp_rtk_base.models.hardware_identity import HardwareConfidence

        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        mon_ver = SimpleNamespace(
            identity="MON-VER",
            swVersion="EXT CORE 1.00",
            hwVersion="ffffffff",
            extension_00="FWVER=HPG 1.32",
            extension_01=None,
        )

        reader = MagicMock()
        reader.read.return_value = (b"", mon_ver)
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        info = driver.connect("/dev/ttyUSB0")
        # Displayed the same as a confirmed F9P, but confidence tells the
        # truth — this is what unlocks/blocks a specific-model profile.
        assert info.model == "ZED-F9P"
        assert info.hardware_target == "ZED-F9P"
        assert info.hardware_confidence == HardwareConfidence.INFERRED

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_mon_ver_nothing_resolves_is_unknown(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """A receiver this app cannot identify at all — connect still
        succeeds; identity comes back `unknown` rather than a bad guess."""
        from sp_rtk_base.models.hardware_identity import HardwareConfidence

        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        mon_ver = SimpleNamespace(
            identity="MON-VER",
            swVersion="EXT CORE 1.00",
            hwVersion="ffffffff",
            extension_00=None,
        )

        reader = MagicMock()
        reader.read.return_value = (b"", mon_ver)
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        info = driver.connect("/dev/ttyUSB0")
        assert info.model == "Unknown"
        assert info.hardware_target == "unknown"
        assert info.hardware_confidence == HardwareConfidence.UNKNOWN


class TestParseCfgTmode:
    """Tests for _parse_cfg_tmode with ECEF and LLH position types."""

    def test_parse_llh_pos_type(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """POS_TYPE=1 (LLH) reads LAT/LON/HEIGHT directly."""
        parsed = SimpleNamespace(
            CFG_TMODE_MODE=2,  # FIXED
            CFG_TMODE_POS_TYPE=1,  # LLH
            CFG_TMODE_LAT=473977000,  # 47.3977° × 1e7
            CFG_TMODE_LON=85456000,  # 8.5456° × 1e7
            CFG_TMODE_HEIGHT=40800,  # 408.00m in cm
            CFG_TMODE_ECEF_X=0,
            CFG_TMODE_ECEF_Y=0,
            CFG_TMODE_ECEF_Z=0,
            CFG_TMODE_ECEF_X_HP=0,
            CFG_TMODE_ECEF_Y_HP=0,
            CFG_TMODE_ECEF_Z_HP=0,
            CFG_TMODE_FIXED_POS_ACC=5000,  # 0.1 mm units = 500 mm
        )
        result = UbloxDriver._parse_cfg_tmode(parsed)  # pyright: ignore[reportPrivateUsage]
        assert result.mode.value == "fixed"
        assert result.pos_type == "llh"
        assert abs(result.latitude - 47.3977) < 0.0001
        assert abs(result.longitude - 8.5456) < 0.0001
        assert abs(result.altitude_m - 408.0) < 0.1
        # 5000 in 0.1 mm units = 500 mm
        assert result.accuracy_mm == 500

    def test_parse_ecef_pos_type(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """POS_TYPE=0 (ECEF) reads ECEF_X/Y/Z and converts to LLH."""
        # Real values from u-center: a point near Portland, OR area
        parsed = SimpleNamespace(
            CFG_TMODE_MODE=2,  # FIXED
            CFG_TMODE_POS_TYPE=0,  # ECEF
            CFG_TMODE_LAT=0,  # unused in ECEF mode
            CFG_TMODE_LON=0,  # unused
            CFG_TMODE_HEIGHT=0,  # unused
            CFG_TMODE_ECEF_X=-245790204,  # cm
            CFG_TMODE_ECEF_Y=-477512066,  # cm
            CFG_TMODE_ECEF_Z=342909332,  # cm
            CFG_TMODE_ECEF_X_HP=0,
            CFG_TMODE_ECEF_Y_HP=0,
            CFG_TMODE_ECEF_Z_HP=0,
            CFG_TMODE_FIXED_POS_ACC=47308,  # 0.1 mm units = 4730 mm
        )
        result = UbloxDriver._parse_cfg_tmode(parsed)  # pyright: ignore[reportPrivateUsage]
        assert result.mode.value == "fixed"
        assert result.pos_type == "ecef"
        # Should produce valid WGS84 coordinates (not zeros)
        assert result.latitude != 0.0
        assert result.longitude != 0.0
        assert result.altitude_m != 0.0
        # Rough check — should be in North America
        assert 30.0 < result.latitude < 55.0
        assert -130.0 < result.longitude < -60.0
        # 47308 in 0.1 mm units = 4730 mm (∼4.73 m)
        assert result.accuracy_mm == 4730

    def test_parse_ecef_disabled_mode(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """DISABLED mode with ECEF pos_type still parses."""
        parsed = SimpleNamespace(
            CFG_TMODE_MODE=0,  # DISABLED
            CFG_TMODE_POS_TYPE=0,  # ECEF
            CFG_TMODE_LAT=0,
            CFG_TMODE_LON=0,
            CFG_TMODE_HEIGHT=0,
            CFG_TMODE_ECEF_X=0,
            CFG_TMODE_ECEF_Y=0,
            CFG_TMODE_ECEF_Z=0,
            CFG_TMODE_ECEF_X_HP=0,
            CFG_TMODE_ECEF_Y_HP=0,
            CFG_TMODE_ECEF_Z_HP=0,
            CFG_TMODE_FIXED_POS_ACC=0,
        )
        result = UbloxDriver._parse_cfg_tmode(parsed)  # pyright: ignore[reportPrivateUsage]
        assert result.mode.value == "disabled"
        assert result.pos_type == "ecef"

    def test_parse_survey_in_mode(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """Survey-in mode with LLH pos_type."""
        parsed = SimpleNamespace(
            CFG_TMODE_MODE=1,  # SURVEY_IN
            CFG_TMODE_POS_TYPE=1,  # LLH
            CFG_TMODE_LAT=0,
            CFG_TMODE_LON=0,
            CFG_TMODE_HEIGHT=0,
            CFG_TMODE_ECEF_X=0,
            CFG_TMODE_ECEF_Y=0,
            CFG_TMODE_ECEF_Z=0,
            CFG_TMODE_ECEF_X_HP=0,
            CFG_TMODE_ECEF_Y_HP=0,
            CFG_TMODE_ECEF_Z_HP=0,
            CFG_TMODE_FIXED_POS_ACC=0,
        )
        result = UbloxDriver._parse_cfg_tmode(parsed)  # pyright: ignore[reportPrivateUsage]
        assert result.mode.value == "survey_in"
        assert result.pos_type == "llh"


class TestEcefToLlh:
    """Tests for the ECEF→LLH coordinate conversion."""

    def test_known_point_zurich(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """Convert a known ECEF point near Zurich, CH."""
        # Zurich area — approximate ECEF coordinates
        # ECEF (m): x ≈ 4277262, y ≈ 643249, z ≈ 4672551
        lat, lon, alt = UbloxDriver._ecef_to_llh(4277262.0, 643249.0, 4672551.0)  # pyright: ignore[reportPrivateUsage]
        # Should be in Switzerland (lat ~47, lon ~8.5)
        assert 47.0 < lat < 48.0
        assert 8.0 < lon < 9.0
        assert -500.0 < alt < 2000.0  # reasonable altitude

    def test_zero_point(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """ECEF origin should produce zero lat/lon."""
        lat, lon, _alt = UbloxDriver._ecef_to_llh(0.0, 0.0, 0.0)  # pyright: ignore[reportPrivateUsage]
        assert lat == 0.0
        assert lon == 0.0

    def test_north_pole(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """ECEF at north pole."""
        # North pole: lat ≈ 90°, ECEF z ≈ 6356752.3 (semi-minor axis)
        lat, _lon, _alt = UbloxDriver._ecef_to_llh(0.0, 0.0, 6356752.3)  # pyright: ignore[reportPrivateUsage]
        assert abs(lat - 90.0) < 0.01


class TestLlhToEcef:
    """Tests for the WGS84 LLH→ECEF coordinate conversion (issue #39)."""

    def test_origin(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """0,0,0 lands on the equator at the WGS84 semi-major axis."""
        x, y, z = UbloxDriver._llh_to_ecef(0.0, 0.0, 0.0)  # pyright: ignore[reportPrivateUsage]
        assert x == pytest.approx(6378137.0, abs=1e-3)
        assert y == pytest.approx(0.0, abs=1e-6)
        assert z == pytest.approx(0.0, abs=1e-6)

    def test_north_pole(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """90°N lands on the WGS84 semi-minor axis."""
        x, y, z = UbloxDriver._llh_to_ecef(90.0, 0.0, 0.0)  # pyright: ignore[reportPrivateUsage]
        assert x == pytest.approx(0.0, abs=1e-3)
        assert y == pytest.approx(0.0, abs=1e-3)
        assert z == pytest.approx(6356752.3, abs=1.0)

    def test_round_trip_with_ecef_to_llh(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """LLH -> ECEF -> LLH recovers the original coordinate."""
        lat, lon, alt = 47.3977, 8.5456, 408.0
        x, y, z = UbloxDriver._llh_to_ecef(lat, lon, alt)  # pyright: ignore[reportPrivateUsage]
        # Never all-zero for a non-origin fix — this is the exact bug:
        # a fixed base silently kept ECEF at 0,0,0 and emitted no RTCM.
        assert (x, y, z) != (0.0, 0.0, 0.0)
        round_lat, round_lon, round_alt = UbloxDriver._ecef_to_llh(x, y, z)  # pyright: ignore[reportPrivateUsage]
        assert round_lat == pytest.approx(lat, abs=1e-6)
        assert round_lon == pytest.approx(lon, abs=1e-6)
        assert round_alt == pytest.approx(alt, abs=1e-3)


class TestMToCmHp:
    """Tests for the metre -> (cm, HP) wire-format split (issue #39)."""

    def test_positive_value_hp_matches_sign(self) -> None:  # pyright: ignore[reportPrivateUsage]
        cm, hp = UbloxDriver._m_to_cm_hp(2457902.0403)  # pyright: ignore[reportPrivateUsage]
        assert cm == 245790204
        assert hp == 3
        assert cm >= 0 and hp >= 0

    def test_negative_value_hp_matches_sign(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """HP must share cm's sign per the u-blox interface spec."""
        cm, hp = UbloxDriver._m_to_cm_hp(-2457902.0403)  # pyright: ignore[reportPrivateUsage]
        assert cm == -245790204
        assert hp == -3
        assert cm <= 0 and hp <= 0

    def test_round_trip(self) -> None:  # pyright: ignore[reportPrivateUsage]
        cm, hp = UbloxDriver._m_to_cm_hp(-477512.06601)  # pyright: ignore[reportPrivateUsage]
        reconstructed_m = cm / 100.0 + hp * 0.0001
        assert reconstructed_m == pytest.approx(-477512.06601, abs=1e-4)


# ---------------------------------------------------------------------------
# Connect timeout and cancel
# ---------------------------------------------------------------------------


class TestConnectTimeoutAndCancel:
    """Tests for wall-clock timeout and cancel_connect() during _poll_mon_ver."""

    def test_connect_timeout_on_garbage(
        self,
        mock_serial: MagicMock,
        mock_reader_factory: type,
    ) -> None:
        """Connect times out when device returns only garbage (wrong baud)."""
        import time as _time

        # Reader always returns None (garbage bytes, no valid UBX)
        reader = MagicMock()
        reader.read = MagicMock(return_value=(b"\xff\xfe", None))

        driver = UbloxDriver()
        driver.CONNECT_TIMEOUT = 0.2  # Very short for test

        with patch(
            "sp_rtk_base.services.drivers.ublox.serial.Serial", return_value=mock_serial
        ):
            with patch(
                "sp_rtk_base.services.drivers.ublox.UBXReader", return_value=reader
            ):
                start = _time.monotonic()
                with pytest.raises(
                    ConnectionError, match="MON-VER|No response|check baud"
                ):
                    driver.connect("/dev/ttyUSB0", 9600)
                elapsed = _time.monotonic() - start
                # Should time out in ~0.2s, not hang
                assert elapsed < 2.0

    def test_connect_timeout_on_exceptions(
        self,
        mock_serial: MagicMock,
    ) -> None:
        """Connect times out when reader.read() keeps raising exceptions."""
        reader = MagicMock()
        reader.read = MagicMock(side_effect=Exception("corrupt frame"))

        driver = UbloxDriver()
        driver.CONNECT_TIMEOUT = 0.2

        with patch(
            "sp_rtk_base.services.drivers.ublox.serial.Serial", return_value=mock_serial
        ):
            with patch(
                "sp_rtk_base.services.drivers.ublox.UBXReader", return_value=reader
            ):
                with pytest.raises(
                    ConnectionError, match="No response from device|Connection failed"
                ):
                    driver.connect("/dev/ttyUSB0", 9600)

    def test_cancel_connect_sets_event(self) -> None:
        """cancel_connect() sets the cancel event."""
        driver = UbloxDriver()
        assert not driver._cancel_event.is_set()  # pyright: ignore[reportPrivateUsage]
        driver.cancel_connect()
        assert driver._cancel_event.is_set()  # pyright: ignore[reportPrivateUsage]

    def test_cancel_connect_during_poll(
        self,
        mock_serial: MagicMock,
    ) -> None:
        """Connect raises ConnectionError when cancelled mid-poll."""
        import threading

        # Reader blocks then returns garbage; cancel fires after short delay
        reader = MagicMock()
        reader.read = MagicMock(return_value=(b"\xff", None))

        driver = UbloxDriver()
        driver.CONNECT_TIMEOUT = 5.0  # Long timeout — cancel should fire first

        def _cancel_after_delay() -> None:
            import time

            time.sleep(0.1)
            driver.cancel_connect()

        with patch(
            "sp_rtk_base.services.drivers.ublox.serial.Serial", return_value=mock_serial
        ):
            with patch(
                "sp_rtk_base.services.drivers.ublox.UBXReader", return_value=reader
            ):
                t = threading.Thread(target=_cancel_after_delay)
                t.start()
                with pytest.raises(
                    ConnectionError, match="cancelled|Connection failed"
                ):
                    driver.connect("/dev/ttyUSB0", 9600)
                t.join(timeout=2.0)

    def test_connect_clears_cancel_event(
        self,
        mock_serial: MagicMock,
        mock_reader_factory: type,
    ) -> None:
        """connect() clears a previously set cancel event."""
        driver = UbloxDriver()
        driver._cancel_event.set()  # pyright: ignore[reportPrivateUsage]

        # Set up a successful reader
        reader: MagicMock = mock_reader_factory.create([_make_mon_ver_response()])  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]

        with patch(
            "sp_rtk_base.services.drivers.ublox.serial.Serial", return_value=mock_serial
        ):
            with patch(
                "sp_rtk_base.services.drivers.ublox.UBXReader", return_value=reader
            ):
                info = driver.connect("/dev/ttyUSB0", 115200)
                assert info.model == "ZED-F9P"
                assert not driver._cancel_event.is_set()  # pyright: ignore[reportPrivateUsage]


class TestDeviceServiceCancelConnect:
    """Tests for DeviceService.cancel_connect() and set_connecting()."""

    def test_set_connecting(self) -> None:
        """set_connecting() sets state to CONNECTING."""
        from sp_rtk_base.models.device_models import DeviceConnectionState
        from sp_rtk_base.services.device_service import DeviceService

        svc = DeviceService()
        assert svc.state == DeviceConnectionState.DISCONNECTED
        svc.set_connecting()
        assert svc.state == DeviceConnectionState.CONNECTING

    def test_cancel_connect_no_driver(self) -> None:
        """cancel_connect() is safe when no driver is loaded."""
        from sp_rtk_base.models.device_models import DeviceConnectionState
        from sp_rtk_base.services.device_service import DeviceService

        svc = DeviceService()
        svc.cancel_connect()  # should not raise
        assert svc.state == DeviceConnectionState.DISCONNECTED

    def test_cancel_connect_with_driver(self) -> None:
        """cancel_connect() calls driver.cancel_connect() if available."""
        from sp_rtk_base.models.device_models import DeviceConnectionState
        from sp_rtk_base.services.device_service import DeviceService

        mock_driver = MagicMock()
        mock_driver.cancel_connect = MagicMock()
        mock_driver.vendor_name = "mock"

        svc = DeviceService()
        svc.set_driver(mock_driver)
        svc.set_connecting()
        svc.cancel_connect()

        mock_driver.cancel_connect.assert_called_once()
        assert svc.state == DeviceConnectionState.DISCONNECTED
        assert svc.get_status().last_error == "Connection cancelled"


# ---------------------------------------------------------------------------
# Apply-config primitives (issue #61)
# ---------------------------------------------------------------------------


def _connect_driver(
    mock_serial_cls: MagicMock,
    mock_reader_cls: MagicMock,
    mock_ubx_msg: MagicMock,
    responses: list[object],
) -> tuple[UbloxDriver, MagicMock]:
    """Connect a ``UbloxDriver`` against mocked serial/reader/UBXMessage.

    ``responses`` are queued *after* the MON-VER response ``connect()``
    consumes, in the order the driver under test will read them.
    """
    ser = MagicMock()
    ser.is_open = True
    mock_serial_cls.return_value = ser

    reader = MagicMock()
    reader.read.side_effect = [
        (b"", _make_mon_ver_response()),
        *[(b"", r) for r in responses],
    ]
    mock_reader_cls.return_value = reader

    mock_msg = MagicMock()
    mock_msg.serialize.return_value = b"\x00"
    mock_ubx_msg.config_set.return_value = mock_msg

    driver = UbloxDriver()
    driver.connect("/dev/ttyUSB0")
    return driver, reader


class TestConfigurePortProtocols:
    """Tests for ``UbloxDriver.configure_port_protocols`` (issue #61)."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_writes_assertive_keys_for_touched_ports_only(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """Only UART1 is present in either mapping, so only its six
        IN/OUT protocol keys should be written — UART2/USB untouched."""
        read_back = SimpleNamespace(
            identity="CFG-VALGET",
            CFG_UART1INPROT_UBX=1,
            CFG_UART1INPROT_NMEA=0,
            CFG_UART1INPROT_RTCM3X=0,
            CFG_UART1OUTPROT_UBX=0,
            CFG_UART1OUTPROT_NMEA=0,
            CFG_UART1OUTPROT_RTCM3X=1,
        )
        driver, _reader = _connect_driver(
            mock_serial_cls,
            mock_reader_cls,
            mock_ubx_msg,
            [_make_ack_response(), read_back],
        )

        driver.configure_port_protocols(
            in_protocols={PortId.UART1: [UbxProtocol.UBX]},
            out_protocols={PortId.UART1: [UbxProtocol.RTCM3X]},
        )

        _, _, cfg_data = mock_ubx_msg.config_set.call_args[0]
        written = dict(cfg_data)
        assert written == {
            "CFG_UART1INPROT_UBX": 1,
            "CFG_UART1INPROT_NMEA": 0,
            "CFG_UART1INPROT_RTCM3X": 0,
            "CFG_UART1OUTPROT_UBX": 0,
            "CFG_UART1OUTPROT_NMEA": 0,
            "CFG_UART1OUTPROT_RTCM3X": 1,
        }

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_empty_mappings_write_nothing(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, []
        )

        driver.configure_port_protocols(in_protocols={}, out_protocols={})

        mock_ubx_msg.config_set.assert_not_called()


class TestConfigureMeasurementRate:
    """Tests for ``UbloxDriver.configure_measurement_rate`` (issue #61)."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_writes_meas_and_pins_nav_to_one(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """meas_period_ms is a raw ms value passed straight through —
        no Hz conversion — and CFG_RATE_NAV is always pinned to 1."""
        read_back = SimpleNamespace(
            identity="CFG-VALGET", CFG_RATE_MEAS=333, CFG_RATE_NAV=1
        )
        driver, _reader = _connect_driver(
            mock_serial_cls,
            mock_reader_cls,
            mock_ubx_msg,
            [_make_ack_response(), read_back],
        )

        driver.configure_measurement_rate(333)

        _, _, cfg_data = mock_ubx_msg.config_set.call_args[0]
        assert dict(cfg_data) == {"CFG_RATE_MEAS": 333, "CFG_RATE_NAV": 1}


class TestConfigureDynModel:
    """Tests for ``UbloxDriver.configure_dyn_model`` (issue #61)."""

    @pytest.mark.parametrize(
        ("model", "expected_value"),
        [
            (DynModel.PORTABLE, 0),
            (DynModel.STATIONARY, 2),
            (DynModel.AIRBORNE_4G, 8),
        ],
    )
    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_writes_mapped_value(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
        model: DynModel,
        expected_value: int,
    ) -> None:
        driver, _reader = _connect_driver(
            mock_serial_cls,
            mock_reader_cls,
            mock_ubx_msg,
            [
                _make_ack_response(),
                SimpleNamespace(
                    identity="CFG-VALGET", CFG_NAVSPG_DYNMODEL=expected_value
                ),
            ],
        )

        driver.configure_dyn_model(model)

        _, _, cfg_data = mock_ubx_msg.config_set.call_args[0]
        assert dict(cfg_data) == {"CFG_NAVSPG_DYNMODEL": expected_value}


class TestGetDynModel:
    """Tests for ``UbloxDriver.get_dyn_model`` (issue #63)."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_reads_stationary(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, [_make_dyn_model_valget(2)]
        )

        assert driver.get_dyn_model() == DynModel.STATIONARY

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_reads_portable(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, [_make_dyn_model_valget(0)]
        )

        assert driver.get_dyn_model() == DynModel.PORTABLE


class TestConfigureTmodeMode:
    """Tests for ``UbloxDriver.configure_tmode_mode`` (issue #61)."""

    @pytest.mark.parametrize(
        ("mode", "expected_value"),
        [
            (BaseMode.DISABLED, 0),
            (BaseMode.SURVEY_IN, 1),
            (BaseMode.FIXED, 2),
        ],
    )
    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_writes_mapped_value_without_position_keys(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
        mode: BaseMode,
        expected_value: int,
    ) -> None:
        driver, _reader = _connect_driver(
            mock_serial_cls,
            mock_reader_cls,
            mock_ubx_msg,
            [
                _make_ack_response(),
                SimpleNamespace(identity="CFG-VALGET", CFG_TMODE_MODE=expected_value),
            ],
        )

        driver.configure_tmode_mode(mode)

        _, _, cfg_data = mock_ubx_msg.config_set.call_args[0]
        assert dict(cfg_data) == {"CFG_TMODE_MODE": expected_value}


class TestConfigureOptimisations:
    """Tests for ``UbloxDriver.configure_optimisations`` (issue #61)."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_writes_only_provided_fields(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        read_back = SimpleNamespace(identity="CFG-VALGET", CFG_NAVSPG_INFIL_MINELEV=10)
        driver, _reader = _connect_driver(
            mock_serial_cls,
            mock_reader_cls,
            mock_ubx_msg,
            [_make_ack_response(), read_back],
        )

        driver.configure_optimisations(
            elevation_mask_deg=10, bds_b2_enabled=None, spi_enabled=None
        )

        _, _, cfg_data = mock_ubx_msg.config_set.call_args[0]
        assert dict(cfg_data) == {"CFG_NAVSPG_INFIL_MINELEV": 10}

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_all_none_writes_nothing(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, []
        )

        driver.configure_optimisations(
            elevation_mask_deg=None, bds_b2_enabled=None, spi_enabled=None
        )

        mock_ubx_msg.config_set.assert_not_called()

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_writes_all_three_fields_when_all_provided(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        read_back = SimpleNamespace(
            identity="CFG-VALGET",
            CFG_NAVSPG_INFIL_MINELEV=15,
            CFG_SIGNAL_BDS_B2_ENA=0,
            CFG_SPI_ENABLED=1,
        )
        driver, _reader = _connect_driver(
            mock_serial_cls,
            mock_reader_cls,
            mock_ubx_msg,
            [_make_ack_response(), read_back],
        )

        driver.configure_optimisations(
            elevation_mask_deg=15, bds_b2_enabled=False, spi_enabled=True
        )

        _, _, cfg_data = mock_ubx_msg.config_set.call_args[0]
        assert dict(cfg_data) == {
            "CFG_NAVSPG_INFIL_MINELEV": 15,
            "CFG_SIGNAL_BDS_B2_ENA": 0,
            "CFG_SPI_ENABLED": 1,
        }


class TestApplyRtcmMatrix:
    """Tests for ``UbloxDriver.apply_rtcm_matrix`` (issue #61)."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_writes_all_36_cells_including_zeros(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """A row not present in ``matrix`` at all must still be written
        as an explicit 0 — a previously-enabled message left out of a
        profile must end up off, not survive as a silent superset."""
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, [_make_ack_response()]
        )

        matrix = {
            RtcmRowId.RTCM_1005: {PortId.UART1: True, PortId.UART2: True},
            RtcmRowId.RTCM_1077: {PortId.UART1: True},
        }
        driver.apply_rtcm_matrix(matrix)

        _, _, cfg_data = mock_ubx_msg.config_set.call_args[0]
        written = dict(cfg_data)
        assert len(written) == 36
        assert written["CFG_MSGOUT_RTCM_3X_TYPE1005_UART1"] == 1
        assert written["CFG_MSGOUT_RTCM_3X_TYPE1005_UART2"] == 1
        assert written["CFG_MSGOUT_RTCM_3X_TYPE1005_USB"] == 0
        assert written["CFG_MSGOUT_RTCM_3X_TYPE1077_UART1"] == 1
        assert written["CFG_MSGOUT_RTCM_3X_TYPE1077_UART2"] == 0
        # A row entirely absent from the matrix — explicit zeros everywhere.
        assert written["CFG_MSGOUT_RTCM_3X_TYPE1230_UART1"] == 0
        assert written["CFG_MSGOUT_RTCM_3X_TYPE1230_UART2"] == 0
        assert written["CFG_MSGOUT_RTCM_3X_TYPE1230_USB"] == 0
        # I2C/SPI are never claimed by the matrix.
        assert not any(key.endswith(("_I2C", "_SPI")) for key in written)

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_single_write_no_internal_retry(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """Unlike this driver's other layer=5 writers, apply_rtcm_matrix
        does not read back or retry internally — the apply-config
        endpoint owns that verification step."""
        driver, reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, [_make_ack_response()]
        )

        driver.apply_rtcm_matrix({})

        assert mock_ubx_msg.config_set.call_count == 1
        # Exactly MON-VER + ACK consumed — no extra VALGET poll/read.
        assert reader.read.call_count == 2


class TestGetUartBaudRates:
    """Tests for ``UbloxDriver.get_uart_baud_rates`` (issue #61)."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_reads_uart1_and_uart2_baud(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        read_back = SimpleNamespace(
            identity="CFG-VALGET",
            CFG_UART1_BAUDRATE=57600,
            CFG_UART2_BAUDRATE=115200,
        )
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, [read_back]
        )

        result = driver.get_uart_baud_rates()

        assert result == {PortId.UART1: 57600, PortId.UART2: 115200}


class TestReadCfgKeysLocked:
    """Tests for ``UbloxDriver._read_cfg_keys_locked`` (issue #94).

    Covers the layer parameter, the tri-state (present / absent /
    NAK) result, and the immediate-NAK-raise behaviour that
    distinguishes this from ``_wait_for_ack``'s write-side sibling.
    """

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_defaults_to_ram_layer(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """No layer arg polls RAM (0) — unchanged pre-#94 behaviour."""
        driver, _reader = _connect_driver(
            mock_serial_cls,
            mock_reader_cls,
            mock_ubx_msg,
            [_make_dyn_model_valget(2)],
        )

        with driver._lock:  # pyright: ignore[reportPrivateUsage]
            result = driver._read_cfg_keys_locked(  # pyright: ignore[reportPrivateUsage]
                ["CFG_NAVSPG_DYNMODEL"]
            )

        assert result == {"CFG_NAVSPG_DYNMODEL": 2}
        mock_ubx_msg.config_poll.assert_called_once_with(0, 0, ["CFG_NAVSPG_DYNMODEL"])

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_polls_requested_layer(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """An explicit layer is forwarded to ``config_poll``."""
        driver, _reader = _connect_driver(
            mock_serial_cls,
            mock_reader_cls,
            mock_ubx_msg,
            [_make_dyn_model_valget(2)],
        )

        with driver._lock:  # pyright: ignore[reportPrivateUsage]
            driver._read_cfg_keys_locked(  # pyright: ignore[reportPrivateUsage]
                ["CFG_NAVSPG_DYNMODEL"], layer=2
            )

        mock_ubx_msg.config_poll.assert_called_once_with(2, 0, ["CFG_NAVSPG_DYNMODEL"])

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_unrecognised_key_omitted_not_sentineled(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """A key the receiver doesn't recognise is absent from the
        dict, not sentinelled to a fake ``-1`` reading."""
        read_back = SimpleNamespace(identity="CFG-VALGET")  # no attrs
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, [read_back]
        )

        with driver._lock:  # pyright: ignore[reportPrivateUsage]
            result = driver._read_cfg_keys_locked(  # pyright: ignore[reportPrivateUsage]
                ["CFG_UNKNOWN_KEY"]
            )

        assert result == {}
        assert "CFG_UNKNOWN_KEY" not in result

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_nak_raises_immediately_naming_nak(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """A NAK is detected on the first read, not after exhausting
        the read loop, and the error names the NAK."""
        driver, reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, [_make_nak_response()]
        )

        with pytest.raises(RuntimeError, match="NAK"):
            with driver._lock:  # pyright: ignore[reportPrivateUsage]
                driver._read_cfg_keys_locked(  # pyright: ignore[reportPrivateUsage]
                    ["CFG_NAVSPG_DYNMODEL"]
                )

        # 1 read for MON-VER during connect() + 1 for the NAK — proves
        # it raised immediately rather than looping to exhaustion.
        assert reader.read.call_count == 2

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_mixed_batch_returns_only_present_keys(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """A batch poll where only some keys are set at the requested
        layer returns the ones that are, without failing."""
        read_back = SimpleNamespace(identity="CFG-VALGET", CFG_UART1_BAUDRATE=57600)
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, [read_back]
        )

        with driver._lock:  # pyright: ignore[reportPrivateUsage]
            result = driver._read_cfg_keys_locked(  # pyright: ignore[reportPrivateUsage]
                ["CFG_UART1_BAUDRATE", "CFG_UART2_BAUDRATE"]
            )

        assert result == {"CFG_UART1_BAUDRATE": 57600}

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_key_absent_at_other_layer_present_at_ram(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """A key never written to a layer reads as absent at that
        layer while still returning its value at RAM."""
        absent_at_bbr = SimpleNamespace(identity="CFG-VALGET")  # no attrs
        present_at_ram = SimpleNamespace(identity="CFG-VALGET", CFG_NAVSPG_DYNMODEL=2)
        driver, _reader = _connect_driver(
            mock_serial_cls,
            mock_reader_cls,
            mock_ubx_msg,
            [absent_at_bbr, present_at_ram],
        )

        with driver._lock:  # pyright: ignore[reportPrivateUsage]
            bbr_result = driver._read_cfg_keys_locked(  # pyright: ignore[reportPrivateUsage]
                ["CFG_NAVSPG_DYNMODEL"], layer=2
            )
            ram_result = driver._read_cfg_keys_locked(  # pyright: ignore[reportPrivateUsage]
                ["CFG_NAVSPG_DYNMODEL"]
            )

        assert bbr_result == {}
        assert ram_result == {"CFG_NAVSPG_DYNMODEL": 2}

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_no_response_raises_no_cfg_valget_error(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """Total timeout (no CFG-VALGET or ACK-NAK at all) still
        raises the existing 'no response' error."""
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, []
        )

        with pytest.raises(RuntimeError, match="No CFG-VALGET response"):
            with driver._lock:  # pyright: ignore[reportPrivateUsage]
                driver._read_cfg_keys_locked(  # pyright: ignore[reportPrivateUsage]
                    ["CFG_NAVSPG_DYNMODEL"]
                )


class TestConfigureBaud:
    """Tests for ``UbloxDriver.configure_baud`` (issue #62)."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_writes_only_uart1_when_uart2_omitted(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, [_make_ack_response()]
        )

        driver.configure_baud(115200, None)

        layer, _, cfg_data = mock_ubx_msg.config_set.call_args[0]
        assert layer == 5
        assert dict(cfg_data) == {"CFG_UART1_BAUDRATE": 115200}

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_writes_both_fields_when_both_provided(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, [_make_ack_response()]
        )

        driver.configure_baud(115200, 38400)

        _, _, cfg_data = mock_ubx_msg.config_set.call_args[0]
        assert dict(cfg_data) == {
            "CFG_UART1_BAUDRATE": 115200,
            "CFG_UART2_BAUDRATE": 38400,
        }

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_no_write_when_both_omitted(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        driver, _reader = _connect_driver(
            mock_serial_cls, mock_reader_cls, mock_ubx_msg, []
        )

        driver.configure_baud(None, None)

        mock_ubx_msg.config_set.assert_not_called()


class TestReconnectAtBaud:
    """Tests for ``UbloxDriver.reconnect_at_baud`` (issue #62)."""

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_reopens_same_port_at_new_baud(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """Does not touch the chip — closes and reopens the host's own
        serial handle, then redoes the MON-VER handshake."""
        first_ser = MagicMock()
        first_ser.is_open = True
        second_ser = MagicMock()
        second_ser.is_open = True
        mock_serial_cls.side_effect = [first_ser, second_ser]

        reader = MagicMock()
        reader.read.side_effect = [
            (b"", _make_mon_ver_response()),
            (b"", _make_mon_ver_response()),
        ]
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0", 57600)

        info = driver.reconnect_at_baud(115200)

        assert info.model == "ZED-F9P"
        first_ser.close.assert_called_once()
        mock_serial_cls.assert_called_with(
            port="/dev/ttyUSB0",
            baudrate=115200,
            timeout=3.0,
            exclusive=True,
        )
        assert driver.is_connected is True

    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_reopen_failure_raises_connection_error(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
    ) -> None:
        first_ser = MagicMock()
        first_ser.is_open = True
        mock_serial_cls.side_effect = [first_ser, ConnectionError("no response")]

        reader = MagicMock()
        reader.read.return_value = (b"", _make_mon_ver_response())
        mock_reader_cls.return_value = reader

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0", 57600)

        with pytest.raises(ConnectionError):
            driver.reconnect_at_baud(115200)
        assert driver.is_connected is False

    def test_never_connected_raises_runtime_error(self) -> None:
        driver = UbloxDriver()
        with pytest.raises(RuntimeError, match="never connected"):
            driver.reconnect_at_baud(115200)
