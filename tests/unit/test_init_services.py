"""Tests for sp_rtk_base.services.__init__ — service initialization and DI helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sp_rtk_base_relay.exceptions import ConfigurationError, InputSourceError

import sp_rtk_base.services as services_mod
from sp_rtk_base.models.config_models import (
    AppConfig,
    AppSettings,
    DestinationProfile,
    InputProfile,
)
from sp_rtk_base.services import AutoStartStatus
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.event_bridge import EventBridge
from sp_rtk_base.services.metrics_service import MetricsService
from sp_rtk_base.services.relay_service import RelayService


@pytest.fixture()
def reset_auto_start_status() -> None:
    """Reset module-level auto-start status to idle before each test."""
    services_mod.auto_start_status = AutoStartStatus()
    services_mod.auto_start_task = None


@pytest.fixture()
def no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch asyncio.sleep inside the auto-start module so retries don't wait."""
    import asyncio

    async def _instant(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def _make_auto_start_config() -> AppConfig:
    """Build an AppConfig with auto_start=True and one enabled TCP destination."""
    return AppConfig(
        input=InputProfile(source="tcp", config={"host": "127.0.0.1", "port": 5015}),
        destinations=[
            DestinationProfile(
                name="test",
                type="tcp_server",
                config={"host": "0.0.0.0", "port": 9000},
            ),
        ],
        settings=AppSettings(auto_start=True),
    )


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
    async def test_init_auto_start_with_config(
        self,
        tmp_path: Path,
        reset_auto_start_status: None,
        no_backoff_sleep: None,
    ) -> None:
        """init_services schedules auto-start that succeeds on first attempt."""
        config_path = tmp_path / "config.yaml"
        mock_config_svc = ConfigService(config_path=config_path)
        mock_config_svc.save_config(_make_auto_start_config())

        mock_relay_svc = MagicMock(spec=RelayService)
        mock_relay_svc.is_running = False
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
            # init_services schedules a background task — await it.
            assert services_mod.auto_start_task is not None
            await services_mod.auto_start_task
            mock_relay_svc.start_relay.assert_called_once()
            mock_eb.start.assert_called_once_with(mock_relay_svc)
            assert services_mod.auto_start_status.state == "succeeded"
            assert services_mod.auto_start_status.attempts == 1
        finally:
            services_mod.config_service = original_config
            services_mod.relay_service = original_relay
            services_mod.event_bridge = original_eb

    @pytest.mark.asyncio()
    async def test_init_auto_start_no_input_skips(
        self,
        tmp_path: Path,
        reset_auto_start_status: None,
    ) -> None:
        """init_services records skipped_no_input when no input configured."""
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
            assert services_mod.auto_start_task is None
            assert services_mod.auto_start_status.state == "skipped_no_input"
        finally:
            services_mod.config_service = original_config
            services_mod.relay_service = original_relay


class TestAutoStartRetryLoop:
    """Tests for the _auto_start_with_retry background-task path."""

    @pytest.mark.asyncio()
    async def test_retries_on_transient_failure_then_succeeds(
        self,
        tmp_path: Path,
        reset_auto_start_status: None,
        no_backoff_sleep: None,
    ) -> None:
        """3 transient InputSourceError raises followed by success → state=succeeded."""
        config_path = tmp_path / "config.yaml"
        mock_config_svc = ConfigService(config_path=config_path)
        mock_config_svc.save_config(_make_auto_start_config())

        mock_relay_svc = MagicMock(spec=RelayService)
        mock_relay_svc.is_running = False
        mock_relay_svc.start_relay = AsyncMock(
            side_effect=[
                InputSourceError("Host is down"),
                InputSourceError("Host is down"),
                InputSourceError("Host is down"),
                None,
            ]
        )
        mock_eb = MagicMock(spec=EventBridge)

        original_config = services_mod.config_service
        original_relay = services_mod.relay_service
        original_eb = services_mod.event_bridge
        try:
            services_mod.config_service = mock_config_svc
            services_mod.relay_service = mock_relay_svc
            services_mod.event_bridge = mock_eb
            await services_mod.init_services()
            assert services_mod.auto_start_task is not None
            await services_mod.auto_start_task
            assert mock_relay_svc.start_relay.call_count == 4
            assert services_mod.auto_start_status.state == "succeeded"
            assert services_mod.auto_start_status.attempts == 4
            mock_eb.start.assert_called_once_with(mock_relay_svc)
        finally:
            services_mod.config_service = original_config
            services_mod.relay_service = original_relay
            services_mod.event_bridge = original_eb

    @pytest.mark.asyncio()
    async def test_all_attempts_fail_records_failed_after_retries(
        self,
        tmp_path: Path,
        reset_auto_start_status: None,
        no_backoff_sleep: None,
    ) -> None:
        """If every attempt fails, state ends as failed_after_retries with last error."""
        config_path = tmp_path / "config.yaml"
        mock_config_svc = ConfigService(config_path=config_path)
        mock_config_svc.save_config(_make_auto_start_config())

        total = len(services_mod.AUTO_START_BACKOFF_SECONDS)
        mock_relay_svc = MagicMock(spec=RelayService)
        mock_relay_svc.is_running = False
        mock_relay_svc.start_relay = AsyncMock(
            side_effect=[InputSourceError(f"err {i}") for i in range(total)]
        )
        mock_eb = MagicMock(spec=EventBridge)

        original_config = services_mod.config_service
        original_relay = services_mod.relay_service
        original_eb = services_mod.event_bridge
        try:
            services_mod.config_service = mock_config_svc
            services_mod.relay_service = mock_relay_svc
            services_mod.event_bridge = mock_eb
            await services_mod.init_services()
            assert services_mod.auto_start_task is not None
            await services_mod.auto_start_task
            assert mock_relay_svc.start_relay.call_count == total
            assert services_mod.auto_start_status.state == "failed_after_retries"
            assert services_mod.auto_start_status.attempts == total
            assert services_mod.auto_start_status.last_error is not None
            assert f"err {total - 1}" in services_mod.auto_start_status.last_error
            mock_eb.start.assert_not_called()
        finally:
            services_mod.config_service = original_config
            services_mod.relay_service = original_relay
            services_mod.event_bridge = original_eb

    @pytest.mark.asyncio()
    async def test_configuration_error_fails_fast_no_retry(
        self,
        tmp_path: Path,
        reset_auto_start_status: None,
        no_backoff_sleep: None,
    ) -> None:
        """ConfigurationError is permanent — stop after attempt 1 with failed_config."""
        config_path = tmp_path / "config.yaml"
        mock_config_svc = ConfigService(config_path=config_path)
        mock_config_svc.save_config(_make_auto_start_config())

        mock_relay_svc = MagicMock(spec=RelayService)
        mock_relay_svc.is_running = False
        mock_relay_svc.start_relay = AsyncMock(
            side_effect=ConfigurationError("filter.message_ids is required")
        )
        mock_eb = MagicMock(spec=EventBridge)

        original_config = services_mod.config_service
        original_relay = services_mod.relay_service
        original_eb = services_mod.event_bridge
        try:
            services_mod.config_service = mock_config_svc
            services_mod.relay_service = mock_relay_svc
            services_mod.event_bridge = mock_eb
            await services_mod.init_services()
            assert services_mod.auto_start_task is not None
            await services_mod.auto_start_task
            assert mock_relay_svc.start_relay.call_count == 1
            assert services_mod.auto_start_status.state == "failed_config"
            assert services_mod.auto_start_status.attempts == 1
            assert "filter.message_ids" in (
                services_mod.auto_start_status.last_error or ""
            )
            mock_eb.start.assert_not_called()
        finally:
            services_mod.config_service = original_config
            services_mod.relay_service = original_relay
            services_mod.event_bridge = original_eb

    @pytest.mark.asyncio()
    async def test_user_starts_during_backoff_aborts_loop(
        self,
        tmp_path: Path,
        reset_auto_start_status: None,
        no_backoff_sleep: None,
    ) -> None:
        """If relay.is_running flips True during a retry wait, exit as succeeded_user."""
        config_path = tmp_path / "config.yaml"
        mock_config_svc = ConfigService(config_path=config_path)
        mock_config_svc.save_config(_make_auto_start_config())

        mock_relay_svc = MagicMock(spec=RelayService)
        # First attempt fails; before attempt 2, user starts manually.
        mock_relay_svc.is_running = False

        async def _start_relay_impl(*_args: object, **_kwargs: object) -> None:
            # After the first call fails, simulate the user starting it.
            mock_relay_svc.is_running = True
            raise InputSourceError("Host is down")

        mock_relay_svc.start_relay = AsyncMock(side_effect=_start_relay_impl)
        mock_eb = MagicMock(spec=EventBridge)

        original_config = services_mod.config_service
        original_relay = services_mod.relay_service
        original_eb = services_mod.event_bridge
        try:
            services_mod.config_service = mock_config_svc
            services_mod.relay_service = mock_relay_svc
            services_mod.event_bridge = mock_eb
            await services_mod.init_services()
            assert services_mod.auto_start_task is not None
            await services_mod.auto_start_task
            # Exactly one attempt before user-initiated start was detected.
            assert mock_relay_svc.start_relay.call_count == 1
            assert services_mod.auto_start_status.state == "succeeded_user"
            mock_eb.start.assert_not_called()
        finally:
            services_mod.config_service = original_config
            services_mod.relay_service = original_relay
            services_mod.event_bridge = original_eb
