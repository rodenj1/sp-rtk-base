"""Tests for GPS receiver driver registry."""

from __future__ import annotations

import pytest

from sp_rtk_base.models.device_models import (
    BaseMode,
    CurrentBaseConfig,
    DeviceCapability,
    DeviceInfo,
    FixedBaseConfig,
    GnssConfig,
    GpsPosition,
    RtcmMessageConfig,
    RtcmPortConfig,
    SurveyInConfig,
    SurveyInProgress,
)
from sp_rtk_base.services.drivers import (
    _DRIVER_REGISTRY,  # pyright: ignore[reportPrivateUsage]
    clear_registry,
    create_driver,
    get_driver_class,
    list_drivers,
    register_driver,
)
from sp_rtk_base.services.drivers.base import GpsReceiverDriver

# ---------------------------------------------------------------------------
# Concrete stub driver for testing
# ---------------------------------------------------------------------------


class StubDriver(GpsReceiverDriver):
    """Minimal concrete driver for registry tests."""

    @property
    def vendor_name(self) -> str:
        return "StubVendor"

    def get_capabilities(self) -> set[DeviceCapability]:
        return {DeviceCapability.SURVEY_IN}

    def connect(self, port: str, baud_rate: int = 115200) -> DeviceInfo:
        return DeviceInfo(vendor="StubVendor", model="StubModel")

    def disconnect(self) -> None:
        pass

    @property
    def is_connected(self) -> bool:
        return False

    def configure_survey_in(self, config: SurveyInConfig) -> None:
        pass

    def configure_fixed_base(self, config: FixedBaseConfig) -> None:
        pass

    def configure_rtcm_messages(self, config: RtcmMessageConfig) -> None:
        pass

    def get_rtcm_config(self) -> RtcmMessageConfig:
        return RtcmMessageConfig(message_ids=[], rate_hz=1)

    def get_rtcm_port_config(self) -> RtcmPortConfig:
        return RtcmPortConfig()

    def configure_rtcm_ports(self, config: RtcmPortConfig) -> None:
        pass

    def get_gnss_config(self) -> GnssConfig:
        return GnssConfig()

    def configure_gnss(self, config: GnssConfig) -> None:
        pass

    def save_to_flash(self) -> None:
        pass

    def disable_base_mode(self) -> None:
        pass

    def get_position(self) -> GpsPosition:
        return GpsPosition()

    def get_survey_in_status(self) -> SurveyInProgress:
        return SurveyInProgress()

    def get_device_info(self) -> DeviceInfo:
        return DeviceInfo(vendor="StubVendor", model="StubModel")

    def get_base_config(self) -> CurrentBaseConfig:
        return CurrentBaseConfig(mode=BaseMode.DISABLED)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry() -> None:  # type: ignore[misc]
    """Save, clear, and restore the driver registry around each test."""
    saved = dict(_DRIVER_REGISTRY)
    clear_registry()
    yield  # type: ignore[misc]
    clear_registry()
    _DRIVER_REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDriverRegistry:
    """Tests for the driver registry functions."""

    def test_register_and_list(self) -> None:
        register_driver("stub", StubDriver)
        assert "stub" in list_drivers()

    def test_get_driver_class_found(self) -> None:
        register_driver("stub", StubDriver)
        cls = get_driver_class("stub")
        assert cls is StubDriver

    def test_get_driver_class_not_found(self) -> None:
        assert get_driver_class("nonexistent") is None

    def test_create_driver_success(self) -> None:
        register_driver("stub", StubDriver)
        driver = create_driver("stub")
        assert isinstance(driver, StubDriver)
        assert driver.vendor_name == "StubVendor"

    def test_create_driver_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown GPS driver 'bad'"):
            create_driver("bad")

    def test_create_driver_shows_available(self) -> None:
        register_driver("alpha", StubDriver)
        register_driver("beta", StubDriver)
        with pytest.raises(ValueError, match="alpha, beta"):
            create_driver("gamma")

    def test_list_drivers_sorted(self) -> None:
        register_driver("zulu", StubDriver)
        register_driver("alpha", StubDriver)
        register_driver("mike", StubDriver)
        assert list_drivers() == ["alpha", "mike", "zulu"]

    def test_list_drivers_empty(self) -> None:
        assert list_drivers() == []

    def test_create_driver_empty_shows_none(self) -> None:
        with pytest.raises(ValueError, match="Available: \\(none\\)"):
            create_driver("anything")

    def test_register_overwrites(self) -> None:
        """Registering same key twice overwrites the previous."""
        register_driver("stub", StubDriver)

        class StubDriver2(StubDriver):
            @property
            def vendor_name(self) -> str:
                return "StubVendor2"

        register_driver("stub", StubDriver2)
        driver = create_driver("stub")
        assert driver.vendor_name == "StubVendor2"


class TestStubDriverInterface:
    """Verify the stub driver implements the ABC correctly."""

    def test_capabilities(self) -> None:
        driver = StubDriver()
        caps = driver.get_capabilities()
        assert DeviceCapability.SURVEY_IN in caps

    def test_connect_returns_info(self) -> None:
        driver = StubDriver()
        info = driver.connect("/dev/ttyACM0")
        assert info.vendor == "StubVendor"
        assert info.model == "StubModel"

    def test_is_connected(self) -> None:
        driver = StubDriver()
        assert driver.is_connected is False

    def test_get_survey_in_status(self) -> None:
        driver = StubDriver()
        status = driver.get_survey_in_status()
        assert status.active is False
