"""Tests for sp_rtk_base.models.config_models — Pydantic config models."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from sp_rtk_base_relay.config import (
    DestinationFilterConfig,
    NtripDestinationConfig,
    SurePathDestinationConfig,
    TcpServerDestinationConfig,
)

from sp_rtk_base.models.config_models import (
    AppConfig,
    AppSettings,
    DestinationProfile,
    FilterProfile,
    InputProfile,
    NtripProfile,
    SurePathProfile,
    TcpServerProfile,
)

# ---------------------------------------------------------------------------
# FilterProfile
# ---------------------------------------------------------------------------


class TestFilterProfile:
    """Tests for FilterProfile model."""

    def test_default_filter(self) -> None:
        """Default filter is pass_all with empty message_ids."""
        f = FilterProfile()
        assert f.mode == "pass_all"
        assert f.message_ids == []

    def test_allowlist_filter(self) -> None:
        """Allowlist filter with message IDs."""
        f = FilterProfile(mode="allowlist", message_ids=[1005, 1077])
        assert f.mode == "allowlist"
        assert f.message_ids == [1005, 1077]

    def test_blocklist_filter(self) -> None:
        """Blocklist filter with message IDs."""
        f = FilterProfile(mode="blocklist", message_ids=[1230])
        assert f.mode == "blocklist"

    def test_invalid_mode_rejected(self) -> None:
        """Invalid filter mode raises ValidationError."""
        with pytest.raises(ValidationError):
            FilterProfile(mode="invalid")  # type: ignore[arg-type]

    def test_to_relay_config(self) -> None:
        """to_relay_config produces DestinationFilterConfig."""
        f = FilterProfile(mode="pass_all")
        relay_cfg = f.to_relay_config()
        assert isinstance(relay_cfg, DestinationFilterConfig)
        assert relay_cfg.mode == "pass_all"
        assert relay_cfg.message_ids == []

    def test_to_relay_config_with_ids(self) -> None:
        """to_relay_config preserves message IDs."""
        f = FilterProfile(mode="allowlist", message_ids=[1005, 1077])
        relay_cfg = f.to_relay_config()
        assert relay_cfg.mode == "allowlist"
        assert relay_cfg.message_ids == [1005, 1077]

    def test_from_relay_config(self) -> None:
        """from_relay_config creates FilterProfile from DestinationFilterConfig."""
        relay_cfg = DestinationFilterConfig(mode="blocklist", message_ids=[1230])
        f = FilterProfile.from_relay_config(relay_cfg)
        assert f.mode == "blocklist"
        assert f.message_ids == [1230]

    def test_roundtrip(self) -> None:
        """FilterProfile → relay config → FilterProfile roundtrip."""
        original = FilterProfile(mode="allowlist", message_ids=[1005, 1087])
        relay_cfg = original.to_relay_config()
        restored = FilterProfile.from_relay_config(relay_cfg)
        assert restored.mode == original.mode
        assert restored.message_ids == original.message_ids


# ---------------------------------------------------------------------------
# SurePathProfile
# ---------------------------------------------------------------------------


class TestSurePathProfile:
    """Tests for SurePathProfile model."""

    def test_required_fields(self) -> None:
        """SurePathProfile requires host, username, password."""
        p = SurePathProfile(host="sp.example.com", username="user", password="pass")
        assert p.host == "sp.example.com"
        assert p.port == 50010  # default

    def test_to_relay_config(self) -> None:
        """to_relay_config produces SurePathDestinationConfig."""
        p = SurePathProfile(host="sp.example.com", username="user", password="pass")
        cfg = p.to_relay_config()
        assert isinstance(cfg, SurePathDestinationConfig)
        assert cfg.host == "sp.example.com"
        assert cfg.username == "user"
        assert cfg.password == "pass"
        assert cfg.port == 50010


# ---------------------------------------------------------------------------
# NtripProfile
# ---------------------------------------------------------------------------


class TestNtripProfile:
    """Tests for NtripProfile model."""

    def test_required_fields(self) -> None:
        """NtripProfile requires caster, mountpoint, password."""
        p = NtripProfile(caster="rtk2go.com", mountpoint="MY_MOUNT", password="secret")
        assert p.caster == "rtk2go.com"
        assert p.port == 2101  # default

    def test_to_relay_config(self) -> None:
        """to_relay_config produces NtripDestinationConfig."""
        p = NtripProfile(caster="rtk2go.com", mountpoint="MY_MOUNT", password="secret")
        cfg = p.to_relay_config()
        assert isinstance(cfg, NtripDestinationConfig)
        assert cfg.caster == "rtk2go.com"
        assert cfg.mountpoint == "MY_MOUNT"
        assert cfg.port == 2101


# ---------------------------------------------------------------------------
# TcpServerProfile
# ---------------------------------------------------------------------------


class TestTcpServerProfile:
    """Tests for TcpServerProfile model."""

    def test_defaults(self) -> None:
        """TcpServerProfile has sensible defaults."""
        p = TcpServerProfile()
        assert p.host == "0.0.0.0"
        assert p.port == 5016
        assert p.max_clients == 10

    def test_to_relay_config(self) -> None:
        """to_relay_config produces TcpServerDestinationConfig."""
        p = TcpServerProfile(port=9000, max_clients=5)
        cfg = p.to_relay_config()
        assert isinstance(cfg, TcpServerDestinationConfig)
        assert cfg.port == 9000
        assert cfg.max_clients == 5


# ---------------------------------------------------------------------------
# DestinationProfile
# ---------------------------------------------------------------------------


class TestDestinationProfile:
    """Tests for DestinationProfile model."""

    @staticmethod
    def _surepath_data() -> dict[str, Any]:
        return {
            "name": "surepath-1",
            "type": "surepath",
            "enabled": True,
            "config": {
                "host": "sp.example.com",
                "username": "user",
                "password": "pass",
            },
        }

    @staticmethod
    def _ntrip_data() -> dict[str, Any]:
        return {
            "name": "rtk2go",
            "type": "ntrip",
            "config": {
                "caster": "rtk2go.com",
                "mountpoint": "MY_MOUNT",
                "password": "secret",
            },
        }

    @staticmethod
    def _tcp_data() -> dict[str, Any]:
        return {
            "name": "local-tcp",
            "type": "tcp_server",
            "config": {"port": 9000},
        }

    def test_surepath_profile(self) -> None:
        """DestinationProfile with surepath type."""
        dp = DestinationProfile(**self._surepath_data())
        assert dp.name == "surepath-1"
        assert dp.type == "surepath"

    def test_ntrip_profile(self) -> None:
        """DestinationProfile with ntrip type."""
        dp = DestinationProfile(**self._ntrip_data())
        assert dp.name == "rtk2go"
        assert dp.type == "ntrip"

    def test_tcp_profile(self) -> None:
        """DestinationProfile with tcp_server type."""
        dp = DestinationProfile(**self._tcp_data())
        assert dp.name == "local-tcp"
        assert dp.type == "tcp_server"

    def test_invalid_type_rejected(self) -> None:
        """Invalid destination type raises ValidationError."""
        with pytest.raises(ValidationError):
            DestinationProfile(name="bad", type="websocket", config={})  # type: ignore[arg-type]

    def test_default_enabled(self) -> None:
        """enabled defaults to True."""
        dp = DestinationProfile(**self._surepath_data())
        assert dp.enabled is True

    def test_default_filter(self) -> None:
        """filter defaults to pass_all."""
        dp = DestinationProfile(**self._surepath_data())
        assert dp.filter.mode == "pass_all"

    def test_to_relay_config_surepath(self) -> None:
        """to_relay_config for surepath type."""
        dp = DestinationProfile(**self._surepath_data())
        cfg = dp.to_relay_config()
        assert cfg.name == "surepath-1"
        assert cfg.type == "surepath"
        assert cfg.enabled is True
        assert isinstance(cfg.config, SurePathDestinationConfig)

    def test_to_relay_config_ntrip(self) -> None:
        """to_relay_config for ntrip type."""
        dp = DestinationProfile(**self._ntrip_data())
        cfg = dp.to_relay_config()
        assert cfg.type == "ntrip"
        assert isinstance(cfg.config, NtripDestinationConfig)

    def test_to_relay_config_tcp(self) -> None:
        """to_relay_config for tcp_server type."""
        dp = DestinationProfile(**self._tcp_data())
        cfg = dp.to_relay_config()
        assert cfg.type == "tcp_server"
        assert isinstance(cfg.config, TcpServerDestinationConfig)

    def test_serialization_roundtrip(self) -> None:
        """model_dump → DestinationProfile roundtrip."""
        dp = DestinationProfile(**self._surepath_data())
        data = dp.model_dump()
        restored = DestinationProfile.model_validate(data)
        assert restored.name == dp.name
        assert restored.type == dp.type
        assert restored.config == dp.config


# ---------------------------------------------------------------------------
# InputProfile
# ---------------------------------------------------------------------------


class TestInputProfile:
    """Tests for InputProfile model."""

    def test_serial_input(self) -> None:
        """Serial input profile."""
        ip = InputProfile(
            source="serial",
            config={"port": "/dev/ttyACM0", "baudrate": 115200},
        )
        assert ip.source == "serial"

    def test_tcp_input(self) -> None:
        """TCP input profile."""
        ip = InputProfile(
            source="tcp",
            config={"host": "127.0.0.1", "port": 5015},
        )
        assert ip.source == "tcp"

    def test_bluetooth_input(self) -> None:
        """Bluetooth input profile."""
        ip = InputProfile(
            source="bluetooth",
            config={"address": "AA:BB:CC:DD:EE:FF", "channel": 1},
        )
        assert ip.source == "bluetooth"

    def test_invalid_source_rejected(self) -> None:
        """Invalid source type raises ValidationError."""
        with pytest.raises(ValidationError):
            InputProfile(source="wifi", config={})  # type: ignore[arg-type]

    def test_to_relay_config(self) -> None:
        """to_relay_config produces InputConfig."""
        ip = InputProfile(
            source="tcp",
            config={"host": "127.0.0.1", "port": 5015},
        )
        cfg = ip.to_relay_config()
        assert cfg.source == "tcp"
        assert cfg.config["host"] == "127.0.0.1"
        assert cfg.config["port"] == 5015


# ---------------------------------------------------------------------------
# AppSettings
# ---------------------------------------------------------------------------


class TestAppSettings:
    """Tests for AppSettings model."""

    def test_defaults(self) -> None:
        """Default settings values."""
        s = AppSettings()
        assert s.auto_start is False
        assert s.status_poll_interval == 2.0

    def test_custom_values(self) -> None:
        """Custom settings values."""
        s = AppSettings(auto_start=True, status_poll_interval=5.0)
        assert s.auto_start is True
        assert s.status_poll_interval == 5.0


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------


class TestAppConfig:
    """Tests for AppConfig model."""

    def test_empty_config(self) -> None:
        """Empty AppConfig has sensible defaults."""
        cfg = AppConfig()
        assert cfg.input is None
        assert cfg.destinations == []
        assert cfg.settings.auto_start is False

    def test_full_config(self) -> None:
        """AppConfig with all fields populated."""
        cfg = AppConfig(
            input=InputProfile(
                source="serial",
                config={"port": "/dev/ttyACM0", "baudrate": 115200},
            ),
            destinations=[
                DestinationProfile(
                    name="rtk2go",
                    type="ntrip",
                    config={
                        "caster": "rtk2go.com",
                        "mountpoint": "MOUNT",
                        "password": "pw",
                    },
                ),
            ],
            settings=AppSettings(auto_start=True),
        )
        assert cfg.input is not None
        assert cfg.input.source == "serial"
        assert len(cfg.destinations) == 1
        assert cfg.settings.auto_start is True

    def test_serialization_roundtrip(self) -> None:
        """model_dump → model_validate roundtrip."""
        cfg = AppConfig(
            input=InputProfile(source="tcp", config={"host": "1.2.3.4", "port": 5015}),
            destinations=[
                DestinationProfile(
                    name="test",
                    type="tcp_server",
                    config={"port": 9000},
                ),
            ],
        )
        data = cfg.model_dump()
        restored = AppConfig.model_validate(data)
        assert restored.input is not None
        assert restored.input.source == "tcp"
        assert len(restored.destinations) == 1
        assert restored.destinations[0].name == "test"


class TestDestinationProfileUnknownType:
    """Tests for DestinationProfile.to_relay_config with unknown types."""

    def test_unknown_type_raises_value_error(self) -> None:
        """to_relay_config raises ValueError for unknown destination type.

        This tests the 'else' branch in to_relay_config() that handles
        unrecognized destination types (lines 191-192).
        """
        # Bypass Pydantic Literal validation by constructing directly
        dp = DestinationProfile.model_construct(
            name="bad-dest",
            type="unknown_type",  # type: ignore[arg-type]
            enabled=True,
            filter=FilterProfile(),
            config={},
        )
        with pytest.raises(ValueError, match="Unknown destination type"):
            dp.to_relay_config()
