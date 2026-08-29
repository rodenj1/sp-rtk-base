# pyright: reportPrivateUsage=false
"""Tests for DeviceService — GPS receiver connection & configuration."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, call

import pytest

from sp_rtk_base.models.device_models import (
    BaseMode,
    CurrentBaseConfig,
    DeviceCapability,
    DeviceConnectionState,
    DeviceInfo,
    DynModel,
    FixedBaseConfig,
    GnssConfig,
    GnssConstellation,
    GnssSystemConfig,
    PortId,
    RtcmPortConfig,
    RtcmRowId,
    SurveyInConfig,
    SurveyInProgress,
    UbxProtocol,
)
from sp_rtk_base.models.profile_models import (
    BaudConfig,
    PortProtocolSet,
    ReceiverConfig,
    RtcmStreamConfig,
)
from sp_rtk_base.services.device_service import (
    ApplyConfigLinkLostError,
    ApplyConfigRefusedError,
    DeviceService,
)
from sp_rtk_base.services.drivers.base import GpsReceiverDriver

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
        vendor=vendor,
        model=model,
        firmware_version="1.0",
    )
    driver.get_device_info.return_value = DeviceInfo(
        vendor=vendor,
        model=model,
        firmware_version="1.0",
    )
    driver.get_survey_in_status.return_value = SurveyInProgress(
        active=True,
        valid=False,
        duration_seconds=30,
        mean_accuracy_mm=25000.0,
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
    async def test_configure_survey_in_does_not_touch_applied_profile(
        self, connected_svc: DeviceService
    ) -> None:
        """Regression test for issue #63.

        Starting a survey-in must not re-write the port protocols or
        dynamics model an operator-applied ``ReceiverConfig`` profile
        set — that's exactly the competing-writer bug #40/#38's
        force-applies caused before they were retired.
        """
        config = SurveyInConfig(min_duration_seconds=300, accuracy_limit_mm=20000)
        await connected_svc.configure_survey_in(config)

        driver = connected_svc.driver
        assert driver is not None
        driver.configure_port_protocols.assert_not_called()  # type: ignore[union-attr]
        driver.configure_dyn_model.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_configure_fixed_base(self, connected_svc: DeviceService) -> None:
        config = FixedBaseConfig(latitude=47.0, longitude=-122.0, altitude_m=100.0)
        await connected_svc.configure_fixed_base(config)

        assert connected_svc.state == DeviceConnectionState.CONNECTED
        connected_svc.driver.configure_fixed_base.assert_called_once_with(config)  # type: ignore[union-attr]

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
    async def test_configure_relay_running_raises(
        self, connected_svc: DeviceService
    ) -> None:
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
# Tests: apply-config (issue #61)
# ---------------------------------------------------------------------------


def _minimal_config(**overrides: object) -> ReceiverConfig:
    """A minimal valid ``ReceiverConfig`` — 1005 enabled on the one data-link port."""
    defaults: dict[str, object] = {
        "data_link_port": [PortId.UART1],
        "rtcm_stream": RtcmStreamConfig(
            matrix={RtcmRowId.RTCM_1005: {PortId.UART1: True}}
        ),
    }
    defaults.update(overrides)
    return ReceiverConfig(**defaults)  # type: ignore[arg-type]


class TestApplyReceiverConfig:
    """Tests for ``DeviceService.apply_receiver_config`` (issue #61)."""

    @pytest.fixture()
    def connected_svc(self) -> DeviceService:
        svc = DeviceService()
        driver = _make_mock_driver()
        driver.get_base_config.return_value = CurrentBaseConfig(mode=BaseMode.DISABLED)
        driver.get_gnss_config.return_value = GnssConfig(systems=[])
        # Matches ``_minimal_config()``'s matrix by default so tests that
        # don't care about the read-back diff see status="ok".
        driver.get_rtcm_port_config.return_value = RtcmPortConfig(
            messages={RtcmRowId.RTCM_1005: {"UART1": 1}}
        )
        driver.get_uart_baud_rates.return_value = {
            PortId.UART1: 57600,
            PortId.UART2: 115200,
        }
        svc.set_driver(driver)
        svc._state = DeviceConnectionState.CONNECTED
        svc._info = DeviceInfo(vendor="MockVendor", model="MockModel")
        svc._baud_rate = 57600
        return svc

    @pytest.mark.asyncio()
    async def test_not_connected_raises(self) -> None:
        svc = DeviceService()
        svc.set_driver(_make_mock_driver())
        with pytest.raises(RuntimeError, match="Device not connected"):
            await svc.apply_receiver_config(_minimal_config())

    @pytest.mark.asyncio()
    async def test_baud_omitted_is_allowed(self, connected_svc: DeviceService) -> None:
        result = await connected_svc.apply_receiver_config(
            _minimal_config(baud=BaudConfig())
        )
        assert result.status == "ok"

    @pytest.mark.asyncio()
    async def test_baud_written_last_after_every_other_key(
        self, connected_svc: DeviceService
    ) -> None:
        driver = connected_svc.driver
        assert driver is not None
        driver.reconnect_at_baud.return_value = DeviceInfo(  # type: ignore[union-attr]
            vendor="MockVendor", model="MockModel"
        )
        config = _minimal_config(
            baud=BaudConfig(uart1=115200),
            dyn_model=DynModel.STATIONARY,
            tmode_mode=BaseMode.DISABLED,
        )

        await connected_svc.apply_receiver_config(config)

        write_methods = {
            "configure_measurement_rate",
            "configure_optimisations",
            "configure_dyn_model",
            "configure_tmode_mode",
            "apply_rtcm_matrix",
            "configure_baud",
        }
        order: list[str] = [
            call_[0]
            for call_ in driver.method_calls  # type: ignore[union-attr]
            if call_[0] in write_methods
        ]
        assert order[-1] == "configure_baud"
        driver.configure_baud.assert_called_once_with(115200, None)  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_baud_uart1_and_uart2_both_written(
        self, connected_svc: DeviceService
    ) -> None:
        driver = connected_svc.driver
        assert driver is not None
        driver.reconnect_at_baud.return_value = DeviceInfo(  # type: ignore[union-attr]
            vendor="MockVendor", model="MockModel"
        )
        config = _minimal_config(baud=BaudConfig(uart1=115200, uart2=38400))

        await connected_svc.apply_receiver_config(config)

        driver.configure_baud.assert_called_once_with(115200, 38400)  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_baud_uart2_only_does_not_reopen(
        self, connected_svc: DeviceService
    ) -> None:
        """UART2 isn't the port this console's own link is on — no reopen."""
        driver = connected_svc.driver
        assert driver is not None
        config = _minimal_config(baud=BaudConfig(uart2=38400))

        result = await connected_svc.apply_receiver_config(config)

        assert result.status == "ok"
        driver.configure_baud.assert_called_once_with(None, 38400)  # type: ignore[union-attr]
        driver.reconnect_at_baud.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_baud_uart1_reopens_before_read_back_verify(
        self, connected_svc: DeviceService
    ) -> None:
        driver = connected_svc.driver
        assert driver is not None
        driver.reconnect_at_baud.return_value = DeviceInfo(  # type: ignore[union-attr]
            vendor="MockVendor", model="MockModel"
        )
        config = _minimal_config(baud=BaudConfig(uart1=115200))

        result = await connected_svc.apply_receiver_config(config)

        assert result.status == "ok"
        driver.reconnect_at_baud.assert_called_once_with(115200)  # type: ignore[union-attr]
        order = [
            call_[0]
            for call_ in driver.method_calls  # type: ignore[union-attr]
            if call_[0] in {"reconnect_at_baud", "get_rtcm_port_config"}
        ]
        assert order == ["reconnect_at_baud", "get_rtcm_port_config"]

    @pytest.mark.asyncio()
    async def test_baud_uart1_reopen_success_updates_tracked_baud(
        self, connected_svc: DeviceService
    ) -> None:
        driver = connected_svc.driver
        assert driver is not None
        connected_svc._baud_rate = 57600  # pyright: ignore[reportPrivateUsage]
        driver.reconnect_at_baud.return_value = DeviceInfo(  # type: ignore[union-attr]
            vendor="MockVendor", model="MockModel"
        )
        config = _minimal_config(baud=BaudConfig(uart1=115200))

        await connected_svc.apply_receiver_config(config)

        assert connected_svc._baud_rate == 115200  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio()
    async def test_baud_uart1_reopen_failure_retries_at_previous_baud_then_raises(
        self, connected_svc: DeviceService
    ) -> None:
        driver = connected_svc.driver
        assert driver is not None
        connected_svc._baud_rate = 57600  # pyright: ignore[reportPrivateUsage]
        driver.reconnect_at_baud.side_effect = ConnectionError(  # type: ignore[union-attr]
            "no response"
        )
        config = _minimal_config(baud=BaudConfig(uart1=115200))

        with pytest.raises(ApplyConfigLinkLostError) as exc_info:
            await connected_svc.apply_receiver_config(config)

        assert exc_info.value.previous_baud == 57600
        assert exc_info.value.new_baud == 115200
        driver.reconnect_at_baud.assert_has_calls(  # type: ignore[union-attr]
            [call(115200), call(57600)]
        )
        assert connected_svc.state == DeviceConnectionState.DISCONNECTED
        driver.get_rtcm_port_config.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_baud_uart1_reopen_retry_recovers_link_but_still_raises(
        self, connected_svc: DeviceService
    ) -> None:
        """Even a successful previous-baud retry still surfaces the
        distinct error — the flash write stands and the caller must
        act, not just silently limp along at the stale baud."""
        driver = connected_svc.driver
        assert driver is not None
        connected_svc._baud_rate = 57600  # pyright: ignore[reportPrivateUsage]
        driver.reconnect_at_baud.side_effect = [  # type: ignore[union-attr]
            ConnectionError("no response at new baud"),
            DeviceInfo(vendor="MockVendor", model="MockModel"),
        ]
        config = _minimal_config(baud=BaudConfig(uart1=115200))

        with pytest.raises(ApplyConfigLinkLostError):
            await connected_svc.apply_receiver_config(config)

        assert connected_svc.state == DeviceConnectionState.CONNECTED
        assert connected_svc._baud_rate == 57600  # pyright: ignore[reportPrivateUsage]
        driver.get_rtcm_port_config.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_ubx_in_liveness_guard_refuses_before_any_write(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        config = _minimal_config(
            ports={PortId.USB: PortProtocolSet(**{"in": [UbxProtocol.NMEA], "out": []})}
        )

        with pytest.raises(ApplyConfigRefusedError) as exc_info:
            await connected_svc.apply_receiver_config(config)

        assert exc_info.value.rule == "ubx_in_liveness"
        connected_svc.driver.configure_measurement_rate.assert_not_called()  # type: ignore[union-attr]
        connected_svc.driver.apply_rtcm_matrix.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_ubx_in_present_on_usb_is_allowed(
        self, connected_svc: DeviceService
    ) -> None:
        config = _minimal_config(
            ports={
                PortId.USB: PortProtocolSet(
                    **{"in": [UbxProtocol.UBX], "out": [UbxProtocol.NMEA]}
                )
            }
        )
        result = await connected_svc.apply_receiver_config(config)
        assert result.status == "ok"

    @pytest.mark.asyncio()
    async def test_tmode_fixed_without_coordinates_refused(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        connected_svc.driver.get_base_config.return_value = CurrentBaseConfig(  # type: ignore[union-attr]
            mode=BaseMode.DISABLED, latitude=0.0, longitude=0.0, altitude_m=0.0
        )
        config = _minimal_config(tmode_mode=BaseMode.FIXED)

        with pytest.raises(ApplyConfigRefusedError) as exc_info:
            await connected_svc.apply_receiver_config(config)

        assert exc_info.value.rule == "tmode_fixed_requires_coordinates"
        connected_svc.driver.configure_measurement_rate.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_tmode_fixed_with_coordinates_succeeds(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        connected_svc.driver.get_base_config.return_value = CurrentBaseConfig(  # type: ignore[union-attr]
            mode=BaseMode.FIXED, latitude=47.0, longitude=8.0, altitude_m=400.0
        )
        config = _minimal_config(tmode_mode=BaseMode.FIXED)

        result = await connected_svc.apply_receiver_config(config)

        assert result.status == "ok"
        connected_svc.driver.configure_tmode_mode.assert_called_once_with(  # type: ignore[union-attr]
            BaseMode.FIXED
        )

    @pytest.mark.asyncio()
    async def test_write_order_matches_the_specified_sequence(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        config = _minimal_config(
            ports={
                PortId.UART1: PortProtocolSet(
                    **{"in": [UbxProtocol.UBX], "out": [UbxProtocol.RTCM3X]}
                )
            },
            constellations=[GnssConstellation.GPS],
            elevation_mask_deg=10,
            dyn_model=DynModel.STATIONARY,
            tmode_mode=BaseMode.DISABLED,
        )

        await connected_svc.apply_receiver_config(config)

        write_methods = {
            "configure_measurement_rate",
            "configure_port_protocols",
            "configure_gnss",
            "configure_optimisations",
            "configure_dyn_model",
            "configure_tmode_mode",
            "apply_rtcm_matrix",
        }
        order: list[str] = [
            call[0]
            for call in connected_svc.driver.method_calls  # type: ignore[union-attr]
            if call[0] in write_methods
        ]
        assert order == [
            "configure_measurement_rate",
            "configure_port_protocols",
            "configure_gnss",
            "configure_optimisations",
            "configure_dyn_model",
            "configure_tmode_mode",
            "apply_rtcm_matrix",
        ]

    @pytest.mark.asyncio()
    async def test_ports_not_written_when_omitted(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        await connected_svc.apply_receiver_config(_minimal_config())
        connected_svc.driver.configure_port_protocols.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_constellations_flip_enabled_and_preserve_channels(
        self, connected_svc: DeviceService
    ) -> None:
        driver = connected_svc.driver
        assert isinstance(driver, MagicMock)
        driver.get_gnss_config.return_value = GnssConfig(
            systems=[
                GnssSystemConfig(
                    constellation=GnssConstellation.GPS,
                    enabled=True,
                    min_channels=8,
                    max_channels=16,
                ),
                GnssSystemConfig(
                    constellation=GnssConstellation.GALILEO,
                    enabled=False,
                    min_channels=4,
                    max_channels=12,
                ),
            ]
        )
        config = _minimal_config(constellations=[GnssConstellation.GALILEO])

        await connected_svc.apply_receiver_config(config)

        written: GnssConfig = driver.configure_gnss.call_args[0][0]
        by_constellation: dict[GnssConstellation, GnssSystemConfig] = {
            s.constellation: s for s in written.systems
        }
        assert by_constellation[GnssConstellation.GPS].enabled is False
        assert by_constellation[GnssConstellation.GALILEO].enabled is True
        # Channel tuning is preserved from the live read, not clobbered.
        assert by_constellation[GnssConstellation.GALILEO].min_channels == 4
        assert by_constellation[GnssConstellation.GALILEO].max_channels == 12

    @pytest.mark.asyncio()
    async def test_dyn_model_and_tmode_mode_omitted_write_neither_key(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        await connected_svc.apply_receiver_config(_minimal_config())
        connected_svc.driver.configure_dyn_model.assert_not_called()  # type: ignore[union-attr]
        connected_svc.driver.configure_tmode_mode.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_optimisations_always_invoked(
        self, connected_svc: DeviceService
    ) -> None:
        """The driver call always happens; per-field omission is the
        driver's job (each ``None`` there means leave untouched)."""
        assert connected_svc.driver is not None
        await connected_svc.apply_receiver_config(_minimal_config())
        connected_svc.driver.configure_optimisations.assert_called_once_with(  # type: ignore[union-attr]
            None, None, None
        )

    @pytest.mark.asyncio()
    async def test_meas_period_ms_passed_through_unconverted(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        await connected_svc.apply_receiver_config(_minimal_config(meas_period_ms=333))
        connected_svc.driver.configure_measurement_rate.assert_called_once_with(  # type: ignore[union-attr]
            333
        )

    @pytest.mark.asyncio()
    async def test_read_back_match_returns_ok(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        connected_svc.driver.get_rtcm_port_config.return_value = RtcmPortConfig(  # type: ignore[union-attr]
            messages={RtcmRowId.RTCM_1005: {"UART1": 1}}
        )
        result = await connected_svc.apply_receiver_config(_minimal_config())
        assert result.status == "ok"
        assert result.diff == []

    @pytest.mark.asyncio()
    async def test_read_back_mismatch_returns_failed_with_diff(
        self, connected_svc: DeviceService
    ) -> None:
        """A mismatch is a 200-with-diff outcome, not an exception —
        the writes are left in flash, nothing is rolled back."""
        assert connected_svc.driver is not None
        connected_svc.driver.get_rtcm_port_config.return_value = RtcmPortConfig(  # type: ignore[union-attr]
            messages={RtcmRowId.RTCM_1005: {"UART1": 0}}
        )
        result = await connected_svc.apply_receiver_config(_minimal_config())

        assert result.status == "failed"
        assert len(result.diff) == 1
        cell = result.diff[0]
        assert cell.row_id == RtcmRowId.RTCM_1005
        assert cell.port == PortId.UART1
        assert cell.expected is True
        assert cell.actual is False

    @pytest.mark.asyncio()
    async def test_throughput_warning_when_over_threshold(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        connected_svc.driver.get_uart_baud_rates.return_value = {  # type: ignore[union-attr]
            PortId.UART1: 9600,
            PortId.UART2: 115200,
        }
        heavy_matrix = RtcmStreamConfig(
            matrix={
                RtcmRowId.RTCM_1005: {PortId.UART1: True},
                RtcmRowId.RTCM_1077: {PortId.UART1: True},
                RtcmRowId.RTCM_1087: {PortId.UART1: True},
                RtcmRowId.RTCM_1097: {PortId.UART1: True},
            }
        )
        connected_svc.driver.get_rtcm_port_config.return_value = RtcmPortConfig(  # type: ignore[union-attr]
            messages={
                row: {"UART1": 1 if ports.get(PortId.UART1) else 0}
                for row, ports in heavy_matrix.matrix.items()
            }
        )
        config = _minimal_config(rtcm_stream=heavy_matrix)

        result = await connected_svc.apply_receiver_config(config)

        assert result.status == "ok"
        assert len(result.warnings) == 1
        assert "UART1" in result.warnings[0]

    @pytest.mark.asyncio()
    async def test_no_throughput_warning_under_threshold(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        connected_svc.driver.get_uart_baud_rates.return_value = {  # type: ignore[union-attr]
            PortId.UART1: 115200,
            PortId.UART2: 115200,
        }
        result = await connected_svc.apply_receiver_config(_minimal_config())
        assert result.warnings == []


class TestSurveyInPreservesAppliedProfile:
    """Regression test for issue #63's acceptance criterion:

    "apply a profile, then start a survey-in, and the applied
    port/dyn-model config is unchanged."

    Uses the real ``FakeGpsDriver`` rather than a mock so this proves
    the actual outcome (read-back after survey-in still matches what
    was applied), not just which driver methods got called.
    """

    @pytest.mark.asyncio()
    async def test_survey_in_does_not_revert_an_applied_profile(self) -> None:
        from sp_rtk_base.services.drivers.fake import FakeGpsDriver

        driver = FakeGpsDriver()
        driver.connect("FAKE", 115200)
        svc = DeviceService()
        svc.set_driver(driver)
        svc._state = DeviceConnectionState.CONNECTED
        svc._info = DeviceInfo(vendor="Fake", model="FAKE-F9P")

        # Deliberately not the built-in profile's values, so a
        # regression that force-re-applies the built-in's stationary
        # dyn model / RTCM-only ports would be caught.
        applied = _minimal_config(
            data_link_port=[PortId.UART1, PortId.UART2],
            ports={
                PortId.UART1: PortProtocolSet(
                    in_=[UbxProtocol.UBX], out=[UbxProtocol.RTCM3X, UbxProtocol.NMEA]
                ),
                PortId.UART2: PortProtocolSet(
                    in_=[UbxProtocol.UBX], out=[UbxProtocol.RTCM3X]
                ),
            },
            dyn_model=DynModel.PORTABLE,
            rtcm_stream=RtcmStreamConfig(
                matrix={
                    RtcmRowId.RTCM_1005: {PortId.UART1: True, PortId.UART2: True},
                }
            ),
        )
        apply_result = await svc.apply_receiver_config(applied)
        assert apply_result.status == "ok"

        await svc.configure_survey_in(
            SurveyInConfig(min_duration_seconds=120, accuracy_limit_mm=50000)
        )

        assert driver.get_dyn_model() is DynModel.PORTABLE
        protocols = driver.get_port_protocols()
        assert protocols.enabled_out(PortId.UART1) == [
            UbxProtocol.RTCM3X,
            UbxProtocol.NMEA,
        ]
        assert protocols.enabled_out(PortId.UART2) == [UbxProtocol.RTCM3X]


# ---------------------------------------------------------------------------
# Tests: Base invariants pre-flight check + one-click remedy (issue #63)
# ---------------------------------------------------------------------------


class TestBaseInvariants:
    """Tests for ``check_base_invariants`` / ``apply_base_invariants``."""

    @pytest.fixture()
    def connected_svc(self) -> DeviceService:
        svc = DeviceService()
        driver = _make_mock_driver()
        driver.get_dyn_model.return_value = DynModel.STATIONARY
        driver.get_rtcm_port_config.return_value = RtcmPortConfig(
            messages={RtcmRowId.RTCM_1005: {"UART1": 1, "UART2": 1}}
        )
        svc.set_driver(driver)
        svc._state = DeviceConnectionState.CONNECTED
        svc._info = DeviceInfo(vendor="MockVendor", model="MockModel")
        return svc

    @pytest.mark.asyncio()
    async def test_not_connected_raises(self) -> None:
        svc = DeviceService()
        svc.set_driver(_make_mock_driver())
        with pytest.raises(RuntimeError, match="Device not connected"):
            await svc.check_base_invariants()

    @pytest.mark.asyncio()
    async def test_no_warnings_when_matching_builtin(
        self, connected_svc: DeviceService
    ) -> None:
        result = await connected_svc.check_base_invariants()
        assert result.warnings == []

    @pytest.mark.asyncio()
    async def test_warns_on_wrong_dyn_model(self, connected_svc: DeviceService) -> None:
        assert connected_svc.driver is not None
        connected_svc.driver.get_dyn_model.return_value = DynModel.PORTABLE  # type: ignore[union-attr]

        result = await connected_svc.check_base_invariants()

        assert len(result.warnings) == 1
        assert "portable" in result.warnings[0]

    @pytest.mark.asyncio()
    async def test_warns_on_no_rtcm_on_data_link_ports(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        connected_svc.driver.get_rtcm_port_config.return_value = RtcmPortConfig()  # type: ignore[union-attr]

        result = await connected_svc.check_base_invariants()

        # The built-in profile's data_link_port is [UART1, UART2] — both
        # lack any enabled row, so both are called out by name.
        assert len(result.warnings) == 2
        assert any("UART1" in w for w in result.warnings)
        assert any("UART2" in w for w in result.warnings)

    @pytest.mark.asyncio()
    async def test_apply_delegates_to_apply_receiver_config_with_builtin_profile(
        self, connected_svc: DeviceService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from sp_rtk_base.models.device_models import ApplyConfigResult
        from sp_rtk_base.profiles import BUILTIN_PROFILES

        mock_apply = AsyncMock(return_value=ApplyConfigResult(status="ok"))
        monkeypatch.setattr(connected_svc, "apply_receiver_config", mock_apply)

        result = await connected_svc.apply_base_invariants()

        applied_config = mock_apply.call_args[0][0]
        builtin = BUILTIN_PROFILES["ublox-f9p-base-standard"]
        # Everything except baud must match the built-in profile
        # verbatim — baud is deliberately stripped so this one-click
        # remedy can never strand the console's own link.
        assert applied_config == builtin.model_copy(update={"baud": None})
        assert applied_config.baud is None
        assert result.status == "ok"


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
