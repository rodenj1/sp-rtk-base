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
    DeviceCapability,
    FixedBaseConfig,
    RtcmMessageConfig,
    SurveyInConfig,
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
        # configure_survey_in now performs:
        #   1. CFG-VALSET TMODE=0 (layer=7: RAM+BBR+Flash)  -> ACK
        #   2. CFG-VALSET TMODE=1 + SVIN params (layer=1)   -> ACK
        #   3. NAV-SVIN poll                                -> dur=0
        #   4. (~2 s gap)
        #   5. NAV-SVIN poll                                -> dur=3 (incremented)
        reader.read.side_effect = [
            (b"", _make_mon_ver_response()),
            (b"", _make_ack_response()),  # full-layer disable
            (b"", _make_ack_response()),  # enable
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

        # Two CFG-VALSET calls: layer=7 disable, layer=1 enable.
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
        # Enable is RAM-only — survey-in is intentionally not persisted.
        assert enable_layer == 1
        keys = [k for k, _ in enable_cfg]
        assert "CFG_TMODE_MODE" in keys
        assert "CFG_TMODE_SVIN_MIN_DUR" in keys
        assert "CFG_TMODE_SVIN_ACC_LIMIT" in keys
        mode_vals = [v for k, v in enable_cfg if k == "CFG_TMODE_MODE"]
        assert mode_vals == [1]

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
        reader.read.side_effect = [
            (b"", _make_mon_ver_response()),
            (b"", _make_ack_response()),
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
        driver.configure_fixed_base(config)

        mock_ubx_msg.config_set.assert_called_once()
        call_args = mock_ubx_msg.config_set.call_args
        cfg_data = call_args[0][2]
        keys = [k for k, _ in cfg_data]
        assert "CFG_TMODE_MODE" in keys
        assert "CFG_TMODE_LAT" in keys
        assert "CFG_TMODE_LON" in keys
        assert "CFG_TMODE_HEIGHT" in keys

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_configure_rtcm_messages(
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

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")

        config = RtcmMessageConfig(message_ids=[1005, 1077, 1087], rate_hz=1)
        driver.configure_rtcm_messages(config)

        mock_ubx_msg.config_set.assert_called_once()

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_configure_rtcm_unknown_message_id(
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

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_set.return_value = mock_msg

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")

        # 9999 is not a known RTCM message
        config = RtcmMessageConfig(message_ids=[1005, 9999], rate_hz=1)
        driver.configure_rtcm_messages(config)  # Should not raise, just warn

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
            CFG_TMODE_FIXED_POS_ACC=5000,
        )
        result = UbloxDriver._parse_cfg_tmode(parsed)  # pyright: ignore[reportPrivateUsage]
        assert result.mode.value == "fixed"
        assert result.pos_type == "llh"
        assert abs(result.latitude - 47.3977) < 0.0001
        assert abs(result.longitude - 8.5456) < 0.0001
        assert abs(result.altitude_m - 408.0) < 0.1
        assert result.accuracy_mm == 5000

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
            CFG_TMODE_FIXED_POS_ACC=47308,
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
        assert result.accuracy_mm == 47308

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


class TestGetRtcmConfig:
    """Tests for get_rtcm_config() and _parse_rtcm_valget()."""

    def test_parse_rtcm_valget_some_enabled(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """Parse a CFG-VALGET with some RTCM messages enabled."""
        parsed = SimpleNamespace(
            identity="CFG-VALGET",
            CFG_MSGOUT_RTCM_3X_TYPE1005_USB=1,
            CFG_MSGOUT_RTCM_3X_TYPE1074_USB=0,
            CFG_MSGOUT_RTCM_3X_TYPE1077_USB=1,
            CFG_MSGOUT_RTCM_3X_TYPE1084_USB=0,
            CFG_MSGOUT_RTCM_3X_TYPE1087_USB=1,
            CFG_MSGOUT_RTCM_3X_TYPE1094_USB=0,
            CFG_MSGOUT_RTCM_3X_TYPE1097_USB=1,
            CFG_MSGOUT_RTCM_3X_TYPE1124_USB=0,
            CFG_MSGOUT_RTCM_3X_TYPE1127_USB=1,
            CFG_MSGOUT_RTCM_3X_TYPE1230_USB=1,
            CFG_MSGOUT_RTCM_3X_TYPE4072_0_USB=0,
        )
        result = UbloxDriver._parse_rtcm_valget(parsed)  # pyright: ignore[reportPrivateUsage]
        assert set(result.message_ids) == {1005, 1077, 1087, 1097, 1127, 1230}
        assert result.rate_hz == 1

    def test_parse_rtcm_valget_all_disabled(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """All messages disabled returns empty list and default rate."""
        parsed = SimpleNamespace(identity="CFG-VALGET")
        result = UbloxDriver._parse_rtcm_valget(parsed)  # pyright: ignore[reportPrivateUsage]
        assert result.message_ids == []
        assert result.rate_hz == 1  # default

    def test_parse_rtcm_valget_mixed_rates(self) -> None:  # pyright: ignore[reportPrivateUsage]
        """Most common rate is returned when messages have different rates."""
        parsed = SimpleNamespace(
            identity="CFG-VALGET",
            CFG_MSGOUT_RTCM_3X_TYPE1005_USB=2,
            CFG_MSGOUT_RTCM_3X_TYPE1077_USB=1,
            CFG_MSGOUT_RTCM_3X_TYPE1087_USB=1,
            CFG_MSGOUT_RTCM_3X_TYPE1097_USB=1,
        )
        result = UbloxDriver._parse_rtcm_valget(parsed)  # pyright: ignore[reportPrivateUsage]
        assert 1005 in result.message_ids
        assert 1077 in result.message_ids
        assert result.rate_hz == 1  # most common

    @patch("sp_rtk_base.services.drivers.ublox.UBXMessage")
    @patch("sp_rtk_base.services.drivers.ublox.UBXReader")
    @patch("sp_rtk_base.services.drivers.ublox.serial.Serial")
    def test_get_rtcm_config_success(
        self,
        mock_serial_cls: MagicMock,
        mock_reader_cls: MagicMock,
        mock_ubx_msg: MagicMock,
    ) -> None:
        """Full get_rtcm_config() flow with mocked serial."""
        ser = MagicMock()
        ser.is_open = True
        mock_serial_cls.return_value = ser

        valget_response = SimpleNamespace(
            identity="CFG-VALGET",
            CFG_MSGOUT_RTCM_3X_TYPE1005_USB=1,
            CFG_MSGOUT_RTCM_3X_TYPE1077_USB=1,
        )

        reader = MagicMock()
        reader.read.side_effect = [
            (b"", _make_mon_ver_response()),
            (b"", valget_response),
        ]
        mock_reader_cls.return_value = reader

        mock_msg = MagicMock()
        mock_msg.serialize.return_value = b"\x00"
        mock_ubx_msg.config_poll.return_value = mock_msg

        driver = UbloxDriver()
        driver.connect("/dev/ttyUSB0")
        result = driver.get_rtcm_config()

        assert 1005 in result.message_ids
        assert 1077 in result.message_ids
        assert result.rate_hz == 1


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
