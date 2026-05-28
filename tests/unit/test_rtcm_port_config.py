"""Tests for multi-port RTCM configuration models and driver methods."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sp_rtk_base.models.device_models import (
    ALL_RTCM_MESSAGE_IDS,
    RTCM_MESSAGE_GROUPS,
    RtcmOutputPort,
    RtcmPortConfig,
)

# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestRtcmPortConfig:
    """Tests for the RtcmPortConfig model."""

    def test_default_empty(self) -> None:
        config = RtcmPortConfig()
        assert config.messages == {}

    def test_enabled_on_port(self) -> None:
        config = RtcmPortConfig(
            messages={
                1005: {"USB": 1, "UART1": 0, "UART2": 1, "I2C": 0, "SPI": 0},
                1077: {"USB": 1, "UART1": 1, "UART2": 0, "I2C": 0, "SPI": 0},
                1087: {"USB": 0, "UART1": 0, "UART2": 0, "I2C": 0, "SPI": 0},
            }
        )
        usb_enabled = config.enabled_on_port(RtcmOutputPort.USB)
        assert 1005 in usb_enabled
        assert 1077 in usb_enabled
        assert 1087 not in usb_enabled

        uart1_enabled = config.enabled_on_port(RtcmOutputPort.UART1)
        assert 1077 in uart1_enabled
        assert 1005 not in uart1_enabled

    def test_is_enabled(self) -> None:
        config = RtcmPortConfig(
            messages={
                1005: {"USB": 1, "UART1": 0},
            }
        )
        assert config.is_enabled(1005, RtcmOutputPort.USB) is True
        assert config.is_enabled(1005, RtcmOutputPort.UART1) is False
        # Non-existent message
        assert config.is_enabled(9999, RtcmOutputPort.USB) is False

    def test_rate(self) -> None:
        config = RtcmPortConfig(
            messages={
                1005: {"USB": 3, "UART1": 0},
            }
        )
        assert config.rate(1005, RtcmOutputPort.USB) == 3
        assert config.rate(1005, RtcmOutputPort.UART1) == 0
        assert config.rate(9999, RtcmOutputPort.USB) == 0


class TestRtcmOutputPort:
    """Tests for the RtcmOutputPort enum."""

    def test_all_ports(self) -> None:
        ports = list(RtcmOutputPort)
        assert len(ports) == 5
        assert RtcmOutputPort.USB in ports
        assert RtcmOutputPort.UART1 in ports
        assert RtcmOutputPort.UART2 in ports
        assert RtcmOutputPort.I2C in ports
        assert RtcmOutputPort.SPI in ports


class TestRtcmMessageGroups:
    """Tests for the RTCM_MESSAGE_GROUPS constant."""

    def test_all_ids_covered(self) -> None:
        """Ensure ALL_RTCM_MESSAGE_IDS matches the groups."""
        ids_from_groups: list[int] = []
        for _, messages in RTCM_MESSAGE_GROUPS:
            for msg_id, _ in messages:
                ids_from_groups.append(msg_id)
        assert sorted(ids_from_groups) == sorted(ALL_RTCM_MESSAGE_IDS)

    def test_groups_present(self) -> None:
        group_names = [name for name, _ in RTCM_MESSAGE_GROUPS]
        assert "Reference" in group_names
        assert "GPS" in group_names
        assert "GLONASS" in group_names
        assert "Galileo" in group_names
        assert "BeiDou" in group_names
        assert "GLONASS Bias" in group_names

    def test_msm4_and_msm7_pairs(self) -> None:
        """MSM4 and MSM7 messages should be paired per constellation."""
        for name, messages in RTCM_MESSAGE_GROUPS:
            if name in ("GPS", "GLONASS", "Galileo", "BeiDou"):
                descs = [desc for _, desc in messages]
                assert "MSM4" in descs, f"{name} missing MSM4"
                assert "MSM7" in descs, f"{name} missing MSM7"


# ---------------------------------------------------------------------------
# Driver key mapping tests
# ---------------------------------------------------------------------------


class TestRtcmKeyMapping:
    """Tests for the _rtcm_key helper and key constants."""

    def test_rtcm_key_standard(self) -> None:
        from sp_rtk_base.services.drivers.ublox import _rtcm_key

        assert _rtcm_key(1005, "USB") == "CFG_MSGOUT_RTCM_3X_TYPE1005_USB"
        assert _rtcm_key(1077, "UART1") == "CFG_MSGOUT_RTCM_3X_TYPE1077_UART1"
        assert _rtcm_key(1087, "I2C") == "CFG_MSGOUT_RTCM_3X_TYPE1087_I2C"

    def test_rtcm_key_4072(self) -> None:
        from sp_rtk_base.services.drivers.ublox import _rtcm_key

        assert _rtcm_key(4072, "USB") == "CFG_MSGOUT_RTCM_3X_TYPE4072_0_USB"
        assert _rtcm_key(4072, "UART2") == "CFG_MSGOUT_RTCM_3X_TYPE4072_0_UART2"

    def test_legacy_usb_keys_match(self) -> None:
        from sp_rtk_base.services.drivers.ublox import _RTCM_KEY_BASES, _RTCM_USB_KEYS

        for msg_id, expected in _RTCM_USB_KEYS.items():
            base = _RTCM_KEY_BASES[msg_id]
            assert expected == f"{base}_USB"


# ---------------------------------------------------------------------------
# Driver parse tests
# ---------------------------------------------------------------------------


class _FakeValget:
    """Fake parsed CFG-VALGET with configurable attribute values."""

    def __init__(self, values: dict[str, int] | None = None) -> None:
        self._values = values or {}
        self.identity = "CFG-VALGET"

    def __getattr__(self, name: str) -> int:
        return self._values.get(name, 0)


class TestParseRtcmPortValget:
    """Tests for UbloxDriver._parse_rtcm_port_valget."""

    def test_parse_all_zero(self) -> None:
        from sp_rtk_base.services.drivers.ublox import UbloxDriver

        parsed = _FakeValget()  # All defaults to 0

        config = UbloxDriver._parse_rtcm_port_valget(parsed)
        assert isinstance(config, RtcmPortConfig)
        # All messages should be present
        assert len(config.messages) == len(ALL_RTCM_MESSAGE_IDS)
        # All rates should be 0
        for port_rates in config.messages.values():
            for r in port_rates.values():
                assert r == 0

    def test_parse_some_enabled(self) -> None:
        from sp_rtk_base.services.drivers.ublox import UbloxDriver, _rtcm_key

        parsed = _FakeValget(
            {
                _rtcm_key(1005, "USB"): 1,
                _rtcm_key(1005, "UART1"): 2,
                _rtcm_key(1077, "USB"): 1,
            }
        )

        config = UbloxDriver._parse_rtcm_port_valget(parsed)
        assert config.is_enabled(1005, RtcmOutputPort.USB)
        assert config.rate(1005, RtcmOutputPort.UART1) == 2
        assert config.is_enabled(1077, RtcmOutputPort.USB)
        assert not config.is_enabled(1087, RtcmOutputPort.USB)


# ---------------------------------------------------------------------------
# Driver configure_rtcm_ports tests
# ---------------------------------------------------------------------------


class TestConfigureRtcmPorts:
    """Tests for UbloxDriver.configure_rtcm_ports."""

    def test_configure_calls_valset(self) -> None:
        from sp_rtk_base.services.drivers.ublox import UbloxDriver

        driver = UbloxDriver()
        driver._serial = MagicMock()
        driver._serial.is_open = True
        driver._reader = MagicMock()

        config = RtcmPortConfig(
            messages={
                1005: {"USB": 1, "UART1": 0, "UART2": 0, "I2C": 0, "SPI": 0},
            }
        )

        # Patch the *locked* variant — that's what writers call now that
        # all CFG-VALSET callers acquire ``self._lock`` directly to make
        # multi-step writes atomic against concurrent polls.
        with patch.object(driver, "_send_cfg_valset_locked") as mock_valset:
            driver.configure_rtcm_ports(config)

            mock_valset.assert_called_once()
            args = mock_valset.call_args[0]
            cfg_data = args[0]
            # Should have 5 keys (one per port)
            assert len(cfg_data) == 5
            # Check USB is rate 1
            usb_entry = [(k, v) for k, v in cfg_data if k.endswith("_USB")]
            assert len(usb_entry) == 1
            assert usb_entry[0][1] == 1

    def test_configure_empty_config(self) -> None:
        from sp_rtk_base.services.drivers.ublox import UbloxDriver

        driver = UbloxDriver()
        driver._serial = MagicMock()
        driver._serial.is_open = True
        driver._reader = MagicMock()

        config = RtcmPortConfig(messages={})

        with patch.object(driver, "_send_cfg_valset_locked") as mock_valset:
            driver.configure_rtcm_ports(config)
            # Should NOT call valset since there are no keys
            mock_valset.assert_not_called()

    def test_configure_unknown_msg_skipped(self) -> None:
        from sp_rtk_base.services.drivers.ublox import UbloxDriver

        driver = UbloxDriver()
        driver._serial = MagicMock()
        driver._serial.is_open = True
        driver._reader = MagicMock()

        config = RtcmPortConfig(
            messages={
                9999: {"USB": 1},  # Unknown message
            }
        )

        with patch.object(driver, "_send_cfg_valset_locked") as mock_valset:
            driver.configure_rtcm_ports(config)
            mock_valset.assert_not_called()


# ---------------------------------------------------------------------------
# DeviceService wrapper tests
# ---------------------------------------------------------------------------


class TestDeviceServiceRtcmPorts:
    """Tests for DeviceService multi-port RTCM wrappers."""

    @pytest.mark.asyncio
    async def test_get_rtcm_port_config(self) -> None:
        from sp_rtk_base.services.device_service import DeviceService

        mock_driver = MagicMock()
        mock_driver.is_connected = True
        expected = RtcmPortConfig(messages={1005: {"USB": 1}})
        mock_driver.get_rtcm_port_config.return_value = expected

        svc = DeviceService()
        svc._driver = mock_driver
        svc._state = MagicMock()
        svc._state.__eq__ = MagicMock(return_value=True)
        # Simulate CONNECTED state
        from sp_rtk_base.models.device_models import DeviceConnectionState

        svc._state = DeviceConnectionState.CONNECTED

        result = await svc.get_rtcm_port_config()
        assert result == expected
        mock_driver.get_rtcm_port_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_configure_rtcm_ports(self) -> None:
        from sp_rtk_base.models.device_models import DeviceConnectionState
        from sp_rtk_base.services.device_service import DeviceService

        mock_driver = MagicMock()
        mock_driver.is_connected = True
        mock_driver.configure_rtcm_ports.return_value = None

        svc = DeviceService()
        svc._driver = mock_driver
        svc._state = DeviceConnectionState.CONNECTED

        config = RtcmPortConfig(messages={1005: {"USB": 1}})
        await svc.configure_rtcm_ports(config)
        mock_driver.configure_rtcm_ports.assert_called_once_with(config)
        assert svc._state == DeviceConnectionState.CONNECTED
