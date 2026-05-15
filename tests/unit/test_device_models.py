"""Tests for device models — vendor-neutral GPS receiver data structures."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sp_rtk_base.models.device_models import (
    DeviceCapability,
    DeviceConnectionState,
    DeviceInfo,
    DeviceStatus,
    FixedBaseConfig,
    RtcmMessageConfig,
    SurveyInConfig,
    SurveyInProgress,
)


class TestDeviceConnectionState:
    """Tests for DeviceConnectionState enum."""

    def test_all_states_exist(self) -> None:
        assert DeviceConnectionState.DISCONNECTED == "disconnected"
        assert DeviceConnectionState.CONNECTING == "connecting"
        assert DeviceConnectionState.CONNECTED == "connected"
        assert DeviceConnectionState.CONFIGURING == "configuring"
        assert DeviceConnectionState.ERROR == "error"

    def test_state_count(self) -> None:
        assert len(DeviceConnectionState) == 5


class TestDeviceCapability:
    """Tests for DeviceCapability enum."""

    def test_all_capabilities_exist(self) -> None:
        assert DeviceCapability.SURVEY_IN == "survey_in"
        assert DeviceCapability.FIXED_BASE == "fixed_base"
        assert DeviceCapability.RTCM_MESSAGE_SELECT == "rtcm_message_select"
        assert DeviceCapability.SAVE_TO_FLASH == "save_to_flash"
        assert DeviceCapability.BACKUP_RESTORE == "backup_restore"
        assert DeviceCapability.POSITION_STREAM == "position_stream"
        assert DeviceCapability.SATELLITE_INFO == "satellite_info"

    def test_capability_count(self) -> None:
        assert len(DeviceCapability) == 8


class TestDeviceInfo:
    """Tests for DeviceInfo model."""

    def test_minimal_creation(self) -> None:
        info = DeviceInfo(vendor="u-blox", model="ZED-F9P")
        assert info.vendor == "u-blox"
        assert info.model == "ZED-F9P"
        assert info.firmware_version == ""
        assert info.serial_number == ""

    def test_full_creation(self) -> None:
        info = DeviceInfo(
            vendor="u-blox",
            model="ZED-F9P",
            firmware_version="1.32",
            protocol_version="27.31",
            hardware_version="00190000",
            serial_number="ABC123",
        )
        assert info.firmware_version == "1.32"
        assert info.protocol_version == "27.31"
        assert info.hardware_version == "00190000"
        assert info.serial_number == "ABC123"

    def test_serialization_roundtrip(self) -> None:
        info = DeviceInfo(vendor="u-blox", model="ZED-F9P", firmware_version="1.32")
        data = info.model_dump()
        restored = DeviceInfo.model_validate(data)
        assert restored == info


class TestSurveyInConfig:
    """Tests for SurveyInConfig model."""

    def test_defaults(self) -> None:
        cfg = SurveyInConfig()
        assert cfg.min_duration_seconds == 120
        assert cfg.accuracy_limit_mm == 50000

    def test_custom_values(self) -> None:
        cfg = SurveyInConfig(min_duration_seconds=300, accuracy_limit_mm=20000)
        assert cfg.min_duration_seconds == 300
        assert cfg.accuracy_limit_mm == 20000

    def test_duration_too_short(self) -> None:
        with pytest.raises(ValidationError, match="greater than or equal to 60"):
            SurveyInConfig(min_duration_seconds=30)

    def test_duration_too_long(self) -> None:
        with pytest.raises(ValidationError, match="less than or equal to 86400"):
            SurveyInConfig(min_duration_seconds=100000)

    def test_accuracy_too_small(self) -> None:
        with pytest.raises(ValidationError, match="greater than or equal to 1000"):
            SurveyInConfig(accuracy_limit_mm=500)


class TestFixedBaseConfig:
    """Tests for FixedBaseConfig model."""

    def test_valid_position(self) -> None:
        cfg = FixedBaseConfig(latitude=47.123, longitude=-122.456, altitude_m=100.5)
        assert cfg.latitude == 47.123
        assert cfg.longitude == -122.456
        assert cfg.altitude_m == 100.5
        assert cfg.accuracy_mm == 1000  # default

    def test_invalid_latitude(self) -> None:
        with pytest.raises(ValidationError, match="greater than or equal to -90"):
            FixedBaseConfig(latitude=-91.0, longitude=0.0, altitude_m=0.0)

    def test_invalid_longitude(self) -> None:
        with pytest.raises(ValidationError, match="less than or equal to 180"):
            FixedBaseConfig(latitude=0.0, longitude=181.0, altitude_m=0.0)


class TestRtcmMessageConfig:
    """Tests for RtcmMessageConfig model."""

    def test_defaults(self) -> None:
        cfg = RtcmMessageConfig()
        assert cfg.message_ids == [1005, 1077, 1087, 1097, 1127, 1230]
        assert cfg.rate_hz == 1

    def test_custom_messages(self) -> None:
        cfg = RtcmMessageConfig(message_ids=[1005, 1077], rate_hz=5)
        assert len(cfg.message_ids) == 2
        assert cfg.rate_hz == 5

    def test_rate_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            RtcmMessageConfig(rate_hz=0)
        with pytest.raises(ValidationError):
            RtcmMessageConfig(rate_hz=11)


class TestSurveyInProgress:
    """Tests for SurveyInProgress model."""

    def test_defaults(self) -> None:
        prog = SurveyInProgress()
        assert prog.active is False
        assert prog.valid is False
        assert prog.duration_seconds == 0
        assert prog.mean_accuracy_mm == 0.0
        assert prog.observations == 0

    def test_active_survey(self) -> None:
        prog = SurveyInProgress(
            active=True, valid=False,
            duration_seconds=60, mean_accuracy_mm=12500.0,
            observations=60,
        )
        assert prog.active is True
        assert prog.valid is False
        assert prog.duration_seconds == 60


class TestDeviceStatus:
    """Tests for DeviceStatus model."""

    def test_default_disconnected(self) -> None:
        status = DeviceStatus()
        assert status.state == DeviceConnectionState.DISCONNECTED
        assert status.port is None
        assert status.info is None
        assert status.capabilities == []
        assert status.survey_in is None

    def test_connected_status(self) -> None:
        status = DeviceStatus(
            state=DeviceConnectionState.CONNECTED,
            port="/dev/ttyACM0",
            baud_rate=115200,
            info=DeviceInfo(vendor="u-blox", model="ZED-F9P"),
            capabilities=[DeviceCapability.SURVEY_IN, DeviceCapability.FIXED_BASE],
        )
        assert status.state == DeviceConnectionState.CONNECTED
        assert status.port == "/dev/ttyACM0"
        assert len(status.capabilities) == 2

    def test_json_roundtrip(self) -> None:
        status = DeviceStatus(
            state=DeviceConnectionState.CONNECTED,
            port="/dev/ttyACM0",
            info=DeviceInfo(vendor="u-blox", model="ZED-F9P"),
        )
        json_str = status.model_dump_json()
        restored = DeviceStatus.model_validate_json(json_str)
        assert restored.state == DeviceConnectionState.CONNECTED
        assert restored.port == "/dev/ttyACM0"
