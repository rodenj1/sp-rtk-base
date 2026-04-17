# pyright: reportPrivateUsage=false
"""Tests for DeviceService — GPS receiver connection & configuration."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from sp_base.models.device_models import (
    BaseMode,
    CurrentBaseConfig,
    DeviceCapability,
    DeviceConnectionState,
    DeviceInfo,
    FixedBaseConfig,
    RtcmMessageConfig,
    SurveyInConfig,
    SurveyInProgress,
)
from sp_base.services.device_service import DeviceService
from sp_base.services.drivers.base import GpsReceiverDriver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_driver(
    *,
    connected: bool = False,
    vendor: str = "MockVendor",
    model: str = "MockModel",
) -> MagicMock:
    """Create a mock GpsReceiverDriver."""
    driver = MagicMock(spec=GpsReceiverDriver)
    driver.vendor_name = vendor
    driver.get_capabilities.return_value = {
        DeviceCapability.SURVEY_IN,
        DeviceCapability.FIXED_BASE,
        DeviceCapability.RTCM_MESSAGE_SELECT,
        DeviceCapability.SAVE_TO_FLASH,
    }
    type(driver).is_connected = PropertyMock(return_value=connected)
    driver.connect.return_value = DeviceInfo(
        vendor=vendor, model=model, firmware_version="1.0",
    )
    driver.get_device_info.return_value = DeviceInfo(
        vendor=vendor, model=model, firmware_version="1.0",
    )
    driver.get_survey_in_status.return_value = SurveyInProgress(
        active=True, valid=False, duration_seconds=30, mean_accuracy_mm=25000.0,
    )
    return driver


# ---------------------------------------------------------------------------
# Tests: Initial state
# ---------------------------------------------------------------------------


class TestDeviceServiceInitial:
    """Tests for DeviceService initial state."""

    def test_starts_disconnected(self) -> None:
        svc = DeviceService()
        assert svc.state == DeviceConnectionState.DISCONNECTED
        assert svc.is_connected is False

    def test_no_driver_initially(self) -> None:
        svc = DeviceService()
        assert svc.is_available is False
        assert svc.driver is None
        assert svc.capabilities == set()
        assert svc.device_info is None

    def test_status_when_disconnected(self) -> None:
        svc = DeviceService()
        status = svc.get_status()
        assert status.state == DeviceConnectionState.DISCONNECTED
        assert status.port is None
        assert status.info is None
        assert status.capabilities == []


# ---------------------------------------------------------------------------
# Tests: Driver management
# ---------------------------------------------------------------------------


class TestDriverManagement:
    """Tests for set_driver and driver-related properties."""

    def test_set_driver(self) -> None:
        svc = DeviceService()
        driver = _make_mock_driver()
        svc.set_driver(driver)
        assert svc.is_available is True
        assert svc.driver is driver

    def test_capabilities_from_driver(self) -> None:
        svc = DeviceService()
        driver = _make_mock_driver()
        svc.set_driver(driver)
        caps = svc.capabilities
        assert DeviceCapability.SURVEY_IN in caps
        assert DeviceCapability.FIXED_BASE in caps

    def test_cannot_change_driver_while_connected(self) -> None:
        svc = DeviceService()
        driver = _make_mock_driver()
        svc.set_driver(driver)
        # Simulate connected state
        svc._state = DeviceConnectionState.CONNECTED
        with pytest.raises(RuntimeError, match="Cannot change driver"):
            svc.set_driver(_make_mock_driver())

    def test_can_change_driver_in_error_state(self) -> None:
        svc = DeviceService()
        svc.set_driver(_make_mock_driver())
        svc._state = DeviceConnectionState.ERROR
        new_driver = _make_mock_driver(vendor="NewVendor")
        svc.set_driver(new_driver)
        assert svc.driver is new_driver


# ---------------------------------------------------------------------------
# Tests: Connection lifecycle
# ---------------------------------------------------------------------------


class TestConnection:
    """Tests for connect and disconnect."""

    @pytest.mark.asyncio()
    async def test_connect_success(self) -> None:
        svc = DeviceService()
        driver = _make_mock_driver()
        svc.set_driver(driver)

        info = await svc.connect("/dev/ttyACM0", 115200)

        assert info.vendor == "MockVendor"
        assert info.model == "MockModel"
        assert svc.state == DeviceConnectionState.CONNECTED
        assert svc.is_connected is True
        assert svc.device_info is not None
        driver.connect.assert_called_once_with("/dev/ttyACM0", 115200)

    @pytest.mark.asyncio()
    async def test_connect_no_driver_raises(self) -> None:
        svc = DeviceService()
        with pytest.raises(RuntimeError, match="No GPS driver loaded"):
            await svc.connect("/dev/ttyACM0")

    @pytest.mark.asyncio()
    async def test_connect_already_connected_raises(self) -> None:
        svc = DeviceService()
        svc.set_driver(_make_mock_driver())
        await svc.connect("/dev/ttyACM0")

        with pytest.raises(RuntimeError, match="Already connected"):
            await svc.connect("/dev/ttyACM0")

    @pytest.mark.asyncio()
    async def test_connect_relay_running_raises(self) -> None:
        svc = DeviceService()
        svc.set_driver(_make_mock_driver())
        svc.set_relay_check(lambda: True)

        with pytest.raises(RuntimeError, match="relay is running"):
            await svc.connect("/dev/ttyACM0")

    @pytest.mark.asyncio()
    async def test_connect_failure_sets_error_state(self) -> None:
        svc = DeviceService()
        driver = _make_mock_driver()
        driver.connect.side_effect = ConnectionError("Port busy")
        svc.set_driver(driver)

        with pytest.raises(ConnectionError, match="Port busy"):
            await svc.connect("/dev/ttyACM0")

        assert svc.state == DeviceConnectionState.ERROR
        status = svc.get_status()
        assert status.last_error == "Port busy"

    @pytest.mark.asyncio()
    async def test_disconnect(self) -> None:
        svc = DeviceService()
        driver = _make_mock_driver(connected=True)
        svc.set_driver(driver)
        await svc.connect("/dev/ttyACM0")

        await svc.disconnect()

        assert svc.state == DeviceConnectionState.DISCONNECTED
        assert svc.is_connected is False
        assert svc.device_info is None
        driver.disconnect.assert_called_once()

    @pytest.mark.asyncio()
    async def test_disconnect_when_already_disconnected(self) -> None:
        """Disconnect when already disconnected — no error."""
        svc = DeviceService()
        await svc.disconnect()
        assert svc.state == DeviceConnectionState.DISCONNECTED

    @pytest.mark.asyncio()
    async def test_status_when_connected(self) -> None:
        svc = DeviceService()
        svc.set_driver(_make_mock_driver())
        await svc.connect("/dev/ttyACM0", 115200)

        status = svc.get_status()
        assert status.state == DeviceConnectionState.CONNECTED
        assert status.port == "/dev/ttyACM0"
        assert status.baud_rate == 115200
        assert status.info is not None
        assert status.info.vendor == "MockVendor"
        assert len(status.capabilities) == 4
        assert status.connected_at is not None


# ---------------------------------------------------------------------------
# Tests: Configuration commands
# ---------------------------------------------------------------------------


class TestConfiguration:
    """Tests for survey-in, fixed base, RTCM, and save."""

    @pytest.fixture()
    def connected_svc(self) -> DeviceService:
        """Provide a connected DeviceService."""
        svc = DeviceService()
        driver = _make_mock_driver()
        svc.set_driver(driver)
        # Simulate connected state directly (avoid async in fixture)
        svc._state = DeviceConnectionState.CONNECTED
        svc._info = DeviceInfo(vendor="MockVendor", model="MockModel")
        return svc

    @pytest.mark.asyncio()
    async def test_configure_survey_in(self, connected_svc: DeviceService) -> None:
        config = SurveyInConfig(min_duration_seconds=300, accuracy_limit_mm=20000)
        await connected_svc.configure_survey_in(config)

        assert connected_svc.state == DeviceConnectionState.CONNECTED
        assert connected_svc.driver is not None
        connected_svc.driver.configure_survey_in.assert_called_once_with(config)  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_configure_fixed_base(self, connected_svc: DeviceService) -> None:
        config = FixedBaseConfig(latitude=47.0, longitude=-122.0, altitude_m=100.0)
        await connected_svc.configure_fixed_base(config)

        assert connected_svc.state == DeviceConnectionState.CONNECTED
        connected_svc.driver.configure_fixed_base.assert_called_once_with(config)  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_configure_rtcm_messages(self, connected_svc: DeviceService) -> None:
        config = RtcmMessageConfig(message_ids=[1005, 1077], rate_hz=2)
        await connected_svc.configure_rtcm_messages(config)

        assert connected_svc.state == DeviceConnectionState.CONNECTED
        connected_svc.driver.configure_rtcm_messages.assert_called_once_with(config)  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_save_to_flash(self, connected_svc: DeviceService) -> None:
        await connected_svc.save_to_flash()
        assert connected_svc.state == DeviceConnectionState.CONNECTED
        connected_svc.driver.save_to_flash.assert_called_once()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_get_base_config_fixed(self, connected_svc: DeviceService) -> None:
        """Read base config when device is in fixed mode."""
        expected = CurrentBaseConfig(
            mode=BaseMode.FIXED,
            latitude=47.123,
            longitude=-122.456,
            altitude_m=100.5,
            accuracy_mm=500,
        )
        assert connected_svc.driver is not None
        connected_svc.driver.get_base_config.return_value = expected  # type: ignore[union-attr]
        result = await connected_svc.get_base_config()
        assert result.mode == BaseMode.FIXED
        assert result.latitude == 47.123
        assert result.accuracy_mm == 500
        connected_svc.driver.get_base_config.assert_called_once()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_get_base_config_disabled(self, connected_svc: DeviceService) -> None:
        """Read base config when device is disabled (no coordinates)."""
        expected = CurrentBaseConfig(mode=BaseMode.DISABLED)
        assert connected_svc.driver is not None
        connected_svc.driver.get_base_config.return_value = expected  # type: ignore[union-attr]
        result = await connected_svc.get_base_config()
        assert result.mode == BaseMode.DISABLED
        assert result.latitude == 0.0

    @pytest.mark.asyncio()
    async def test_get_base_config_not_connected_raises(self) -> None:
        svc = DeviceService()
        svc.set_driver(_make_mock_driver())
        with pytest.raises(RuntimeError, match="Device not connected"):
            await svc.get_base_config()

    @pytest.mark.asyncio()
    async def test_configure_not_connected_raises(self) -> None:
        svc = DeviceService()
        svc.set_driver(_make_mock_driver())
        with pytest.raises(RuntimeError, match="Device not connected"):
            await svc.configure_survey_in(SurveyInConfig())

    @pytest.mark.asyncio()
    async def test_configure_relay_running_raises(self, connected_svc: DeviceService) -> None:
        connected_svc.set_relay_check(lambda: True)
        with pytest.raises(RuntimeError, match="relay is running"):
            await connected_svc.configure_survey_in(SurveyInConfig())

    @pytest.mark.asyncio()
    async def test_configure_failure_preserves_connected_state(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        connected_svc.driver.configure_survey_in.side_effect = RuntimeError("NAK")  # type: ignore[union-attr]

        with pytest.raises(RuntimeError, match="NAK"):
            await connected_svc.configure_survey_in(SurveyInConfig())

        # State should return to CONNECTED, not stuck in CONFIGURING
        assert connected_svc.state == DeviceConnectionState.CONNECTED
        status = connected_svc.get_status()
        assert status.last_error == "NAK"


# ---------------------------------------------------------------------------
# Tests: Survey-in polling
# ---------------------------------------------------------------------------


class TestSurveyInPolling:
    """Tests for get_survey_in_status."""

    @pytest.mark.asyncio()
    async def test_poll_survey_in(self) -> None:
        svc = DeviceService()
        driver = _make_mock_driver()
        svc.set_driver(driver)
        svc._state = DeviceConnectionState.CONNECTED
        svc._info = DeviceInfo(vendor="MockVendor", model="MockModel")

        progress = await svc.get_survey_in_status()
        assert progress.active is True
        assert progress.duration_seconds == 30
        driver.get_survey_in_status.assert_called_once()

    @pytest.mark.asyncio()
    async def test_poll_not_connected_raises(self) -> None:
        svc = DeviceService()
        svc.set_driver(_make_mock_driver())
        with pytest.raises(RuntimeError, match="Device not connected"):
            await svc.get_survey_in_status()


# ---------------------------------------------------------------------------
# Tests: Mutual exclusion
# ---------------------------------------------------------------------------


class TestMutualExclusion:
    """Tests for relay ↔ device mutual exclusion."""

    def test_relay_check_not_set_allows_operations(self) -> None:
        """Without relay check, operations proceed normally."""
        svc = DeviceService()
        driver = _make_mock_driver()
        svc.set_driver(driver)
        svc._state = DeviceConnectionState.CONNECTED
        svc._info = DeviceInfo(vendor="MockVendor", model="MockModel")
        # _require_connected should succeed with no relay check
        result = svc._require_connected()
        assert result is driver

    def test_relay_check_false_allows_operations(self) -> None:
        svc = DeviceService()
        driver = _make_mock_driver()
        svc.set_driver(driver)
        svc._state = DeviceConnectionState.CONNECTED
        svc._info = DeviceInfo(vendor="MockVendor", model="MockModel")
        svc.set_relay_check(lambda: False)
        result = svc._require_connected()
        assert result is driver

    def test_relay_check_true_blocks_operations(self) -> None:
        svc = DeviceService()
        driver = _make_mock_driver()
        svc.set_driver(driver)
        svc._state = DeviceConnectionState.CONNECTED
        svc._info = DeviceInfo(vendor="MockVendor", model="MockModel")
        svc.set_relay_check(lambda: True)
        with pytest.raises(RuntimeError, match="relay is running"):
            svc._require_connected()
