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
    PortProtocolConfig,
    ReceiverScalarConfig,
    RtcmPortConfig,
    RtcmRowId,
    SurveyInConfig,
    SurveyInProgress,
    UbxProtocol,
)
from sp_rtk_base.models.profile_models import (
    ApplyConfigResult,
    BaudAssertion,
    PortProtocolSet,
    ReceiverApplyRequest,
    ReceiverAssertion,
    RtcmStreamConfig,
)
from sp_rtk_base.services.device_service import (
    ApplyConfigLinkLostError,
    ApplyConfigRefusedError,
    DeviceService,
    build_receiver_assertion,
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
# Tests: build_receiver_assertion + get_receiver_assertion (issue #97)
# ---------------------------------------------------------------------------


def _scalars(**overrides: object) -> ReceiverScalarConfig:
    defaults: dict[str, object] = {
        "uart1_baud": 57600,
        "uart2_baud": 115200,
        "meas_period_ms": 250,
        "dyn_model": DynModel.STATIONARY,
        "tmode_mode": BaseMode.FIXED,
        "elevation_mask_deg": 15,
        "bds_b2_enabled": False,
        "spi_enabled": True,
    }
    defaults.update(overrides)
    return ReceiverScalarConfig(**defaults)  # type: ignore[arg-type]


class TestBuildReceiverAssertion:
    """Pure composition of four driver reads into one ``ReceiverAssertion``."""

    def test_composes_every_field(self) -> None:
        rtcm = RtcmPortConfig(
            messages={RtcmRowId.RTCM_1005: {"UART1": 1, "UART2": 0, "USB": 0}}
        )
        ports = PortProtocolConfig(
            in_protocols={PortId.UART1: [UbxProtocol.UBX]},
            out_protocols={PortId.UART1: [UbxProtocol.RTCM3X]},
        )
        gnss = GnssConfig(
            systems=[
                GnssSystemConfig(constellation=GnssConstellation.GPS, enabled=True),
                GnssSystemConfig(constellation=GnssConstellation.SBAS, enabled=False),
            ]
        )
        scalars = _scalars()

        assertion = build_receiver_assertion(rtcm, ports, gnss, scalars)

        assert assertion.rtcm_stream.matrix[RtcmRowId.RTCM_1005][PortId.UART1] is True
        assert assertion.rtcm_stream.matrix[RtcmRowId.RTCM_1005][PortId.UART2] is False
        assert assertion.ports[PortId.UART1].in_ == [UbxProtocol.UBX]
        assert assertion.ports[PortId.UART1].out == [UbxProtocol.RTCM3X]
        assert assertion.constellations == [GnssConstellation.GPS]
        assert assertion.baud.uart1 == 57600
        assert assertion.baud.uart2 == 115200
        assert assertion.meas_period_ms == 250
        assert assertion.dyn_model == DynModel.STATIONARY
        assert assertion.tmode_mode == BaseMode.FIXED
        assert assertion.elevation_mask_deg == 15
        assert assertion.bds_b2_enabled is False
        assert assertion.spi_enabled is True

    def test_matrix_covers_every_catalog_row_and_matrix_port(self) -> None:
        assertion = build_receiver_assertion(
            RtcmPortConfig(), PortProtocolConfig(), GnssConfig(), _scalars()
        )
        assert set(assertion.rtcm_stream.matrix.keys()) == set(RtcmRowId)
        for row in assertion.rtcm_stream.matrix.values():
            assert set(row.keys()) == {PortId.UART1, PortId.UART2, PortId.USB}

    def test_ports_covers_all_three_ports_even_when_unset(self) -> None:
        assertion = build_receiver_assertion(
            RtcmPortConfig(), PortProtocolConfig(), GnssConfig(), _scalars()
        )
        assert set(assertion.ports.keys()) == {PortId.UART1, PortId.UART2, PortId.USB}


class TestGetReceiverAssertion:
    @pytest.fixture()
    def connected_svc(self) -> DeviceService:
        svc = DeviceService()
        driver = _make_mock_driver()
        driver.get_rtcm_port_config.return_value = RtcmPortConfig(
            messages={RtcmRowId.RTCM_1005: {"UART1": 1}}
        )
        driver.get_port_protocols.return_value = PortProtocolConfig()
        driver.get_gnss_config.return_value = GnssConfig()
        driver.get_receiver_scalars.return_value = _scalars()
        svc.set_driver(driver)
        svc._state = DeviceConnectionState.CONNECTED
        svc._info = DeviceInfo(vendor="MockVendor", model="MockModel")
        return svc

    @pytest.mark.asyncio()
    async def test_composes_the_four_driver_reads(
        self, connected_svc: DeviceService
    ) -> None:
        read = await connected_svc.get_receiver_assertion()

        assertion = read.assertion
        assert assertion.rtcm_stream.matrix[RtcmRowId.RTCM_1005][PortId.UART1] is True
        assert assertion.meas_period_ms == 250
        assert assertion.dyn_model == DynModel.STATIONARY
        driver = connected_svc.driver
        assert driver is not None
        driver.get_rtcm_port_config.assert_called_once()  # type: ignore[union-attr]
        driver.get_port_protocols.assert_called_once()  # type: ignore[union-attr]
        driver.get_gnss_config.assert_called_once()  # type: ignore[union-attr]
        driver.get_receiver_scalars.assert_called_once()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_returns_the_raw_rtcm_read_alongside_the_assertion(
        self, connected_svc: DeviceService
    ) -> None:
        """The raw RTCM read is bundled in, not re-polled (issue #97
        review): the page's I2C/SPI advisory needs it, and a second call
        to ``get_rtcm_port_config`` would reintroduce the extra round
        trip this method exists to collapse away."""
        driver = connected_svc.driver
        assert driver is not None

        read = await connected_svc.get_receiver_assertion()

        assert read.rtcm is driver.get_rtcm_port_config.return_value  # type: ignore[union-attr]
        driver.get_rtcm_port_config.assert_called_once()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_not_connected_raises(self) -> None:
        svc = DeviceService()
        svc.set_driver(_make_mock_driver())
        with pytest.raises(RuntimeError, match="Device not connected"):
            await svc.get_receiver_assertion()


# ---------------------------------------------------------------------------
# Tests: apply-config (issue #61)
# ---------------------------------------------------------------------------


def _minimal_assertion(**overrides: object) -> ReceiverAssertion:
    """A fully-populated, minimal-but-valid assertion — 1005 enabled on
    UART1, everything else at an unambiguous, easy-to-assert-on default.

    Matches ``TestApplyReceiverConfig.connected_svc``'s default driver
    reads exactly (baud, meas rate, dyn model, tmode mode, optimisation
    fields) — so a request built from this with no overrides round-trips
    through a mocked apply as ``status="ok"`` with nothing rewritten,
    and any single overridden field is the one and only thing that
    differs from "the receiver's current state" for that test.
    """
    defaults: dict[str, object] = {
        "baud": BaudAssertion(uart1=57600, uart2=115200),
        "meas_period_ms": 1000,
        "constellations": [],
        "ports": {},
        "dyn_model": DynModel.PORTABLE,
        "tmode_mode": BaseMode.DISABLED,
        "elevation_mask_deg": 0,
        "bds_b2_enabled": False,
        "spi_enabled": False,
        "rtcm_stream": RtcmStreamConfig(
            matrix={RtcmRowId.RTCM_1005: {PortId.UART1: True}}
        ),
    }
    defaults.update(overrides)
    return ReceiverAssertion(**defaults)  # type: ignore[arg-type]


def _minimal_request(
    *, data_link_port: list[PortId] | None = None, **assertion_overrides: object
) -> ReceiverApplyRequest:
    """The envelope ``apply_receiver_config`` takes — wraps
    :func:`_minimal_assertion` with a one-UART data-link selection by
    default."""
    return ReceiverApplyRequest(
        assertion=_minimal_assertion(**assertion_overrides),
        data_link_port=(
            data_link_port if data_link_port is not None else [PortId.UART1]
        ),
    )


def _scalars_for(assertion: ReceiverAssertion) -> ReceiverScalarConfig:
    """The ``ReceiverScalarConfig`` a driver read would need to return
    for :func:`build_receiver_assertion` to reconstruct *assertion*'s
    scalar fields exactly."""
    return ReceiverScalarConfig(
        uart1_baud=assertion.baud.uart1,
        uart2_baud=assertion.baud.uart2,
        meas_period_ms=assertion.meas_period_ms,
        dyn_model=assertion.dyn_model,
        tmode_mode=assertion.tmode_mode,
        elevation_mask_deg=assertion.elevation_mask_deg,
        bds_b2_enabled=assertion.bds_b2_enabled,
        spi_enabled=assertion.spi_enabled,
    )


def _mock_read_back_matches(driver: MagicMock, assertion: ReceiverAssertion) -> None:
    """Point every read-back driver method at values that reconstruct
    exactly *assertion* via ``build_receiver_assertion`` — used by tests
    that apply a request and then assert ``status == "ok"`` even though
    the request deliberately diverges from the fixture's defaults.

    Sets ``get_receiver_scalars.return_value`` too — fine for tests
    that don't care whether the pre-write skip-decision "current"
    reads the new or old scalars, but tests exercising the meas-rate/
    baud unchanged-skip explicitly override ``.side_effect`` afterwards
    (this function's `.return_value`` is then ignored).
    """
    driver.get_rtcm_port_config.return_value = RtcmPortConfig(
        messages={
            row: {port.value: int(on) for port, on in ports.items()}
            for row, ports in assertion.rtcm_stream.matrix.items()
        }
    )
    driver.get_port_protocols.return_value = PortProtocolConfig(
        in_protocols={port: cfg.in_ for port, cfg in assertion.ports.items()},
        out_protocols={port: cfg.out for port, cfg in assertion.ports.items()},
    )
    wanted = set(assertion.constellations)
    driver.get_gnss_config.return_value = GnssConfig(
        systems=[
            GnssSystemConfig(constellation=c, enabled=c in wanted)
            for c in GnssConstellation
        ]
    )
    driver.get_receiver_scalars.return_value = _scalars_for(assertion)


class TestApplyReceiverConfig:
    """Tests for ``DeviceService.apply_receiver_config`` (issue #98)."""

    @pytest.fixture()
    def connected_svc(self) -> DeviceService:
        svc = DeviceService()
        driver = _make_mock_driver()
        driver.get_base_config.return_value = CurrentBaseConfig(mode=BaseMode.DISABLED)
        driver.get_gnss_config.return_value = GnssConfig(systems=[])
        # Matches ``_minimal_assertion()`` exactly by default, so tests
        # that don't care about the read-back diff see status="ok" and
        # the meas-rate/baud unchanged-skip fires for anything that
        # doesn't deliberately override those fields.
        driver.get_rtcm_port_config.return_value = RtcmPortConfig(
            messages={RtcmRowId.RTCM_1005: {"UART1": 1}}
        )
        driver.get_port_protocols.return_value = PortProtocolConfig()
        driver.get_receiver_scalars.return_value = _scalars_for(_minimal_assertion())
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
            await svc.apply_receiver_config(_minimal_request())

    @pytest.mark.asyncio()
    async def test_meas_period_ms_unchanged_is_not_rewritten(
        self, connected_svc: DeviceService
    ) -> None:
        """Issue #98 acceptance criterion: applying with an unchanged
        measurement rate no longer rewrites it."""
        assert connected_svc.driver is not None
        await connected_svc.apply_receiver_config(_minimal_request())
        connected_svc.driver.configure_measurement_rate.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_meas_period_ms_changed_is_rewritten(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        await connected_svc.apply_receiver_config(_minimal_request(meas_period_ms=333))
        connected_svc.driver.configure_measurement_rate.assert_called_once_with(  # type: ignore[union-attr]
            333
        )

    @pytest.mark.asyncio()
    async def test_baud_unchanged_is_not_rewritten(
        self, connected_svc: DeviceService
    ) -> None:
        driver = connected_svc.driver
        assert driver is not None
        result = await connected_svc.apply_receiver_config(_minimal_request())
        assert result.status == "ok"
        driver.configure_baud.assert_not_called()  # type: ignore[union-attr]
        driver.reconnect_at_baud.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_baud_written_last_after_every_other_key(
        self, connected_svc: DeviceService
    ) -> None:
        driver = connected_svc.driver
        assert driver is not None
        driver.reconnect_at_baud.return_value = DeviceInfo(  # type: ignore[union-attr]
            vendor="MockVendor", model="MockModel"
        )
        request = _minimal_request(baud=BaudAssertion(uart1=115200, uart2=115200))

        await connected_svc.apply_receiver_config(request)

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
        request = _minimal_request(baud=BaudAssertion(uart1=115200, uart2=38400))

        await connected_svc.apply_receiver_config(request)

        driver.configure_baud.assert_called_once_with(115200, 38400)  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_baud_uart2_only_does_not_reopen(
        self, connected_svc: DeviceService
    ) -> None:
        """UART2 isn't the port this console's own link is on — no reopen."""
        driver = connected_svc.driver
        assert driver is not None
        request = _minimal_request(baud=BaudAssertion(uart1=57600, uart2=38400))
        _mock_read_back_matches(driver, request.assertion)
        driver.get_receiver_scalars.side_effect = [
            _scalars_for(_minimal_assertion()),
            _scalars_for(request.assertion),
        ]

        result = await connected_svc.apply_receiver_config(request)

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
        request = _minimal_request(baud=BaudAssertion(uart1=115200, uart2=115200))
        _mock_read_back_matches(driver, request.assertion)
        driver.get_receiver_scalars.side_effect = [
            _scalars_for(_minimal_assertion()),
            _scalars_for(request.assertion),
        ]

        result = await connected_svc.apply_receiver_config(request)

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
        request = _minimal_request(baud=BaudAssertion(uart1=115200, uart2=115200))

        await connected_svc.apply_receiver_config(request)

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
        request = _minimal_request(baud=BaudAssertion(uart1=115200, uart2=115200))

        with pytest.raises(ApplyConfigLinkLostError) as exc_info:
            await connected_svc.apply_receiver_config(request)

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
        request = _minimal_request(baud=BaudAssertion(uart1=115200, uart2=115200))

        with pytest.raises(ApplyConfigLinkLostError):
            await connected_svc.apply_receiver_config(request)

        assert connected_svc.state == DeviceConnectionState.CONNECTED
        assert connected_svc._baud_rate == 57600  # pyright: ignore[reportPrivateUsage]
        driver.get_rtcm_port_config.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_ubx_in_liveness_guard_refuses_before_any_write(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        request = _minimal_request(
            ports={PortId.USB: PortProtocolSet(**{"in": [UbxProtocol.NMEA], "out": []})}
        )

        with pytest.raises(ApplyConfigRefusedError) as exc_info:
            await connected_svc.apply_receiver_config(request)

        assert exc_info.value.rule == "ubx_in_liveness"
        connected_svc.driver.configure_measurement_rate.assert_not_called()  # type: ignore[union-attr]
        connected_svc.driver.apply_rtcm_matrix.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_ubx_in_present_on_usb_is_allowed(
        self, connected_svc: DeviceService
    ) -> None:
        driver = connected_svc.driver
        assert driver is not None
        request = _minimal_request(
            ports={
                PortId.USB: PortProtocolSet(
                    **{"in": [UbxProtocol.UBX], "out": [UbxProtocol.NMEA]}
                )
            }
        )
        _mock_read_back_matches(driver, request.assertion)

        result = await connected_svc.apply_receiver_config(request)
        assert result.status == "ok"

    @pytest.mark.asyncio()
    async def test_tmode_fixed_without_coordinates_refused(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        connected_svc.driver.get_base_config.return_value = CurrentBaseConfig(  # type: ignore[union-attr]
            mode=BaseMode.DISABLED, latitude=0.0, longitude=0.0, altitude_m=0.0
        )
        request = _minimal_request(tmode_mode=BaseMode.FIXED)

        with pytest.raises(ApplyConfigRefusedError) as exc_info:
            await connected_svc.apply_receiver_config(request)

        assert exc_info.value.rule == "tmode_fixed_requires_coordinates"
        connected_svc.driver.configure_measurement_rate.assert_not_called()  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_tmode_fixed_with_coordinates_succeeds(
        self, connected_svc: DeviceService
    ) -> None:
        driver = connected_svc.driver
        assert driver is not None
        driver.get_base_config.return_value = CurrentBaseConfig(
            mode=BaseMode.FIXED, latitude=47.0, longitude=8.0, altitude_m=400.0
        )
        request = _minimal_request(tmode_mode=BaseMode.FIXED)
        _mock_read_back_matches(driver, request.assertion)

        result = await connected_svc.apply_receiver_config(request)

        assert result.status == "ok"
        driver.configure_tmode_mode.assert_called_once_with(BaseMode.FIXED)

    @pytest.mark.asyncio()
    async def test_write_order_matches_the_specified_sequence(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        request = _minimal_request(
            ports={
                PortId.UART1: PortProtocolSet(
                    **{"in": [UbxProtocol.UBX], "out": [UbxProtocol.RTCM3X]}
                )
            },
            constellations=[GnssConstellation.GPS],
            elevation_mask_deg=10,
            dyn_model=DynModel.STATIONARY,
            tmode_mode=BaseMode.DISABLED,
            # Differs from the fixture's default current scalars so
            # ``configure_measurement_rate`` actually fires and can be
            # asserted first in the order below.
            meas_period_ms=333,
        )

        await connected_svc.apply_receiver_config(request)

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
    async def test_ports_always_written(self, connected_svc: DeviceService) -> None:
        """Issue #98: every field is sent on every Apply — ``ports`` is no
        longer conditionally omitted."""
        assert connected_svc.driver is not None
        await connected_svc.apply_receiver_config(_minimal_request())
        connected_svc.driver.configure_port_protocols.assert_called_once()  # type: ignore[union-attr]

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
        request = _minimal_request(constellations=[GnssConstellation.GALILEO])

        await connected_svc.apply_receiver_config(request)

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
    async def test_dyn_model_and_tmode_mode_always_written(
        self, connected_svc: DeviceService
    ) -> None:
        """Issue #98: every field is sent on every Apply — ``dyn_model``/
        ``tmode_mode`` are no longer conditionally omitted. Plain
        reassertion of the same value is safe here (not the edge-
        triggered survey-in path — see ``UbloxDriver.configure_tmode_mode``)."""
        assert connected_svc.driver is not None
        await connected_svc.apply_receiver_config(_minimal_request())
        connected_svc.driver.configure_dyn_model.assert_called_once_with(  # type: ignore[union-attr]
            DynModel.PORTABLE
        )
        connected_svc.driver.configure_tmode_mode.assert_called_once_with(  # type: ignore[union-attr]
            BaseMode.DISABLED
        )

    @pytest.mark.asyncio()
    async def test_optimisations_always_invoked(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        await connected_svc.apply_receiver_config(_minimal_request())
        connected_svc.driver.configure_optimisations.assert_called_once_with(  # type: ignore[union-attr]
            0, False, False
        )

    @pytest.mark.asyncio()
    async def test_read_back_match_returns_ok(
        self, connected_svc: DeviceService
    ) -> None:
        assert connected_svc.driver is not None
        result = await connected_svc.apply_receiver_config(_minimal_request())
        assert result.status == "ok"
        assert result.diff == []
        from sp_rtk_base.models.profile_models import diff_receiver_assertions

        assert diff_receiver_assertions(result.read_back, _minimal_assertion()) == []

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
        result = await connected_svc.apply_receiver_config(_minimal_request())

        assert result.status == "failed"
        assert len(result.diff) == 1
        leaf = result.diff[0]
        assert leaf.path == "rtcm.1005.UART1"
        assert leaf.expected is True
        assert leaf.actual is False

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
        request = _minimal_request(rtcm_stream=heavy_matrix)

        result = await connected_svc.apply_receiver_config(request)

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
        result = await connected_svc.apply_receiver_config(_minimal_request())
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

        # Live-seed the whole envelope (issue #98: Apply sends the whole
        # form), then override only the fields this regression cares
        # about — mirroring how the real UI builds ``form`` from
        # ``live`` and mutates specific fields. Deliberately keeps
        # ``dyn_model: portable`` (the fake driver's own live default),
        # not the built-in profile's ``stationary``, so a regression
        # that force-re-applies the built-in's dynamics model would be
        # caught.
        live = (await svc.get_receiver_assertion()).assertion
        applied_assertion = live.model_copy(
            update={
                "dyn_model": DynModel.PORTABLE,
                "ports": {
                    **live.ports,
                    PortId.UART1: PortProtocolSet(
                        in_=[UbxProtocol.UBX],
                        out=[UbxProtocol.RTCM3X, UbxProtocol.NMEA],
                    ),
                    PortId.UART2: PortProtocolSet(
                        in_=[UbxProtocol.UBX], out=[UbxProtocol.RTCM3X]
                    ),
                },
                "rtcm_stream": RtcmStreamConfig(
                    matrix={
                        RtcmRowId.RTCM_1005: {PortId.UART1: True, PortId.UART2: True},
                    }
                ),
            }
        )
        applied = ReceiverApplyRequest(
            assertion=applied_assertion, data_link_port=[PortId.UART1, PortId.UART2]
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
        # ``apply_base_invariants`` now reads a full live assertion via
        # ``get_receiver_assertion()`` before merging the built-in
        # profile onto it (issue #98) — deliberately distinct from the
        # built-in's own values wherever it sets one explicitly, so a
        # regression that used the built-in's value instead of live (or
        # vice versa) is caught by ``test_apply_delegates_...`` below.
        driver.get_port_protocols.return_value = PortProtocolConfig()
        driver.get_gnss_config.return_value = GnssConfig(systems=[])
        driver.get_receiver_scalars.return_value = ReceiverScalarConfig(
            uart1_baud=9600,
            uart2_baud=9600,
            meas_period_ms=250,
            dyn_model=DynModel.PORTABLE,
            tmode_mode=BaseMode.SURVEY_IN,
            elevation_mask_deg=45,
            bds_b2_enabled=True,
            spi_enabled=False,
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

        from sp_rtk_base.profiles import BUILTIN_PROFILES

        mock_apply = AsyncMock(
            return_value=ApplyConfigResult(status="ok", read_back=_minimal_assertion())
        )
        monkeypatch.setattr(connected_svc, "apply_receiver_config", mock_apply)

        result = await connected_svc.apply_base_invariants()

        applied_request: ReceiverApplyRequest = mock_apply.call_args[0][0]
        builtin = BUILTIN_PROFILES["ublox-f9p-base-standard"]

        # data_link_port comes straight from the built-in profile.
        assert applied_request.data_link_port == list(builtin.data_link_port)

        # Fields the built-in profile sets explicitly win over live —
        # proven by the fixture's live scalars deliberately disagreeing
        # with every one of them.
        assert applied_request.assertion.dyn_model == builtin.dyn_model
        assert applied_request.assertion.elevation_mask_deg == (
            builtin.elevation_mask_deg
        )
        assert applied_request.assertion.bds_b2_enabled == builtin.bds_b2_enabled
        assert applied_request.assertion.spi_enabled == builtin.spi_enabled
        assert applied_request.assertion.constellations == builtin.constellations
        assert applied_request.assertion.ports[PortId.UART1].out == [UbxProtocol.RTCM3X]

        # baud is deliberately stripped from the profile copy before
        # merging, so this one-click remedy can never strand the
        # console's own link on a baud change the operator didn't ask
        # for — it falls back to the live baud (9600/9600 in the
        # fixture), not the built-in's own 57600/115200.
        assert applied_request.assertion.baud == BaudAssertion(uart1=9600, uart2=9600)

        # tmode_mode is omitted by the built-in profile — falls back to
        # live (survey_in in the fixture).
        assert applied_request.assertion.tmode_mode == BaseMode.SURVEY_IN

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
