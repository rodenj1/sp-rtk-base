"""Tests for sp_rtk_base.services.__init__ — service initialization and DI helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import sp_rtk_base.services as services_mod
from sp_rtk_base.models.config_models import (
    AppConfig,
    AppSettings,
    DestinationProfile,
    InputProfile,
)
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.event_bridge import EventBridge
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.relay_service import RelayService


class TestDependencyInjectionHelpers:
    """Tests for DI helper functions that return singleton instances."""

    def test_get_relay_service_returns_relay_service(self) -> None:
        """get_relay_service returns a RelayService instance."""
        result = services_mod.get_relay_service()
        assert isinstance(result, RelayService)

    def test_get_config_service_returns_config_service(self) -> None:
        """get_config_service returns a ConfigService instance."""
        result = services_mod.get_config_service()
        assert isinstance(result, ConfigService)

    def test_get_event_bridge_returns_event_bridge(self) -> None:
        """get_event_bridge returns an EventBridge instance."""
        result = services_mod.get_event_bridge()
        assert isinstance(result, EventBridge)

    def test_get_metrics_service_returns_metrics_service(self) -> None:
        """get_metrics_service returns a MetricsService instance."""
        result = services_mod.get_metrics_service()
        assert isinstance(result, MetricsService)

    def test_singletons_are_consistent(self) -> None:
        """DI helpers return the same instance each time."""
        a = services_mod.get_relay_service()
        b = services_mod.get_relay_service()
        assert a is b


class TestInitServices:
    """Tests for the init_services() async function."""

    @pytest.mark.asyncio()
    async def test_init_loads_config(self, tmp_path: Path) -> None:
        """init_services loads the config from disk."""
        config_path = tmp_path / "config.yaml"
        mock_config_svc = ConfigService(config_path=config_path)

        original_config = services_mod.config_service
        try:
            services_mod.config_service = mock_config_svc
            await services_mod.init_services()
            assert config_path.exists()
        finally:
            services_mod.config_service = original_config

    @pytest.mark.asyncio()
    async def test_init_no_auto_start_when_disabled(self, tmp_path: Path) -> None:
        """init_services does not start relay when auto_start is False."""
        config_path = tmp_path / "config.yaml"
        mock_config_svc = ConfigService(config_path=config_path)

        mock_relay_svc = MagicMock(spec=RelayService)
        mock_relay_svc.start_relay = AsyncMock()

        original_config = services_mod.config_service
        original_relay = services_mod.relay_service
        try:
            services_mod.config_service = mock_config_svc
            services_mod.relay_service = mock_relay_svc
            await services_mod.init_services()
            mock_relay_svc.start_relay.assert_not_called()
        finally:
            services_mod.config_service = original_config
            services_mod.relay_service = original_relay

    @pytest.mark.asyncio()
    async def test_init_auto_start_with_config(self, tmp_path: Path) -> None:
        """init_services auto-starts relay when auto_start=True and config present."""
        config_path = tmp_path / "config.yaml"
        mock_config_svc = ConfigService(config_path=config_path)

        config = AppConfig(
            input=InputProfile(
                source="tcp", config={"host": "127.0.0.1", "port": 5015}
            ),
            destinations=[
                DestinationProfile(
                    name="test",
                    type="tcp_server",
                    config={"host": "0.0.0.0", "port": 9000},
                ),
            ],
            settings=AppSettings(auto_start=True),
        )
        mock_config_svc.save_config(config)

        mock_relay_svc = MagicMock(spec=RelayService)
        mock_relay_svc.start_relay = AsyncMock()
        mock_eb = MagicMock(spec=EventBridge)

        original_config = services_mod.config_service
        original_relay = services_mod.relay_service
        original_eb = services_mod.event_bridge
        try:
            services_mod.config_service = mock_config_svc
            services_mod.relay_service = mock_relay_svc
            services_mod.event_bridge = mock_eb
            await services_mod.init_services()
            mock_relay_svc.start_relay.assert_called_once()
            mock_eb.start.assert_called_once_with(mock_relay_svc)
        finally:
            services_mod.config_service = original_config
            services_mod.relay_service = original_relay
            services_mod.event_bridge = original_eb

    @pytest.mark.asyncio()
    async def test_init_auto_start_no_input_skips(self, tmp_path: Path) -> None:
        """init_services skips auto-start when no input configured."""
        config_path = tmp_path / "config.yaml"
        mock_config_svc = ConfigService(config_path=config_path)

        config = AppConfig(settings=AppSettings(auto_start=True))
        mock_config_svc.save_config(config)

        mock_relay_svc = MagicMock(spec=RelayService)
        mock_relay_svc.start_relay = AsyncMock()

        original_config = services_mod.config_service
        original_relay = services_mod.relay_service
        try:
            services_mod.config_service = mock_config_svc
            services_mod.relay_service = mock_relay_svc
            await services_mod.init_services()
            mock_relay_svc.start_relay.assert_not_called()
        finally:
            services_mod.config_service = original_config
            services_mod.relay_service = original_relay

    @pytest.mark.asyncio()
    async def test_init_auto_start_failure_logs_exception(self, tmp_path: Path) -> None:
        """init_services logs but doesn't crash on auto-start failure."""
        config_path = tmp_path / "config.yaml"
        mock_config_svc = ConfigService(config_path=config_path)

        config = AppConfig(
            input=InputProfile(
                source="tcp", config={"host": "127.0.0.1", "port": 5015}
            ),
            destinations=[
                DestinationProfile(
                    name="test",
                    type="tcp_server",
                    config={"host": "0.0.0.0", "port": 9000},
                ),
            ],
            settings=AppSettings(auto_start=True),
        )
        mock_config_svc.save_config(config)

        mock_relay_svc = MagicMock(spec=RelayService)
        mock_relay_svc.start_relay = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )
        mock_eb = MagicMock(spec=EventBridge)

        original_config = services_mod.config_service
        original_relay = services_mod.relay_service
        original_eb = services_mod.event_bridge
        try:
            services_mod.config_service = mock_config_svc
            services_mod.relay_service = mock_relay_svc
            services_mod.event_bridge = mock_eb
            # Should NOT raise — error is logged
            await services_mod.init_services()
            mock_relay_svc.start_relay.assert_called_once()
            mock_eb.start.assert_not_called()
        finally:
            services_mod.config_service = original_config
            services_mod.relay_service = original_relay
            services_mod.event_bridge = original_eb
