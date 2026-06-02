"""Tests for sp_rtk_base.services.relay_service — async RelayEngine wrapper."""

# pyright: reportPrivateUsage=false
# Tests need to set internal state (e.g. _engine) for unit testing.

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sp_rtk_base.services.relay_service import RelayService

# Get a reference to the actual module (not the singleton variable
# shadowed by sp_rtk_base.services.__init__).
_relay_module = sys.modules["sp_rtk_base.services.relay_service"]
from sp_rtk_base_relay.config import (
    DestinationConfig,
    DestinationFilterConfig,
    InputConfig,
    NtripDestinationConfig,
)
from sp_rtk_base_relay.exceptions import ServiceError


def _make_input_config() -> InputConfig:
    """Create a test input config."""
    return InputConfig(source="tcp", config={"host": "127.0.0.1", "port": 5015})


def _make_dest_config() -> DestinationConfig:
    """Create a test destination config."""
    return DestinationConfig(
        name="rtk2go",
        type="ntrip",
        enabled=True,
        filter=DestinationFilterConfig(mode="pass_all"),
        config=NtripDestinationConfig(
            caster="rtk2go.com",
            mountpoint="MOUNT",
            password="pw",
        ),
    )


@dataclass(frozen=True)
class _MockInputStatus:
    """Minimal mock of InputStatus."""

    connected: bool = True
    source_type: str = "tcp"
    bytes_received: int = 1000
    messages_received: int = 50
    seconds_since_last_data: float = 0.5
    reconnect_attempts: int = 1
    reconnect_successes: int = 1
    connected_since: float | None = 100.0


@dataclass(frozen=True)
class _MockRelayStatus:
    """Minimal mock of RelayStatus."""

    running: bool = True
    uptime_seconds: float | None = 30.0
    input: _MockInputStatus = _MockInputStatus()
    destinations: list[Any] = ()  # type: ignore[assignment]
    active_destination_count: int = 0
    total_destination_count: int = 0
    bytes_received: int = 1000
    chunks_distributed: int = 50
    frames_parsed: int = 25
    no_data_warnings: int = 0


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestRelayServiceConstruction:
    """Tests for RelayService initialization."""

    def test_initial_state(self) -> None:
        """New RelayService is not running with no engine."""
        svc = RelayService()
        assert svc.is_running is False
        assert svc.engine is None

    def test_get_destination_names_when_no_engine(self) -> None:
        """get_destination_names returns empty list without engine."""
        svc = RelayService()
        assert svc.get_destination_names() == []

    def test_subscribe_events_when_no_engine(self) -> None:
        """subscribe_events returns None without engine."""
        svc = RelayService()
        assert svc.subscribe_events() is None

    def test_get_recent_events_when_no_engine(self) -> None:
        """get_recent_events returns empty list without engine."""
        svc = RelayService()
        assert svc.get_recent_events() == []


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestRelayServiceLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio()
    async def test_start_creates_engine(self) -> None:
        """start_relay creates a RelayEngine and starts it."""
        svc = RelayService()
        input_cfg = _make_input_config()

        with patch.object(_relay_module, "RelayEngine") as mock_cls:
            mock_engine = MagicMock()
            mock_engine.is_running = False
            mock_cls.return_value = mock_engine

            await svc.start_relay(input_cfg)

            mock_cls.assert_called_once_with(input_cfg)
            mock_engine.start.assert_called_once_with(None)
            assert svc.engine is mock_engine

    @pytest.mark.asyncio()
    async def test_start_with_destinations(self) -> None:
        """start_relay passes destinations to engine.start()."""
        svc = RelayService()
        input_cfg = _make_input_config()
        dests = [_make_dest_config()]

        with patch.object(_relay_module, "RelayEngine") as mock_cls:
            mock_engine = MagicMock()
            mock_engine.is_running = False
            mock_cls.return_value = mock_engine

            await svc.start_relay(input_cfg, dests)
            mock_engine.start.assert_called_once_with(dests)

    @pytest.mark.asyncio()
    async def test_start_when_already_running_raises(self) -> None:
        """start_relay raises ServiceError if already running."""
        svc = RelayService()
        input_cfg = _make_input_config()

        with patch.object(_relay_module, "RelayEngine") as mock_cls:
            mock_engine = MagicMock()
            mock_engine.is_running = True
            mock_cls.return_value = mock_engine
            svc._engine = mock_engine

            with pytest.raises(ServiceError, match="already running"):
                await svc.start_relay(input_cfg)

    @pytest.mark.asyncio()
    async def test_stop_relay(self) -> None:
        """stop_relay calls engine.stop()."""
        svc = RelayService()

        with patch.object(_relay_module, "RelayEngine") as mock_cls:
            mock_engine = MagicMock()
            mock_engine.is_running = True
            mock_cls.return_value = mock_engine
            svc._engine = mock_engine

            await svc.stop_relay()
            mock_engine.stop.assert_called_once()

    @pytest.mark.asyncio()
    async def test_stop_noop_when_not_running(self) -> None:
        """stop_relay is a no-op when engine is not running."""
        svc = RelayService()
        # No engine — should not raise
        await svc.stop_relay()

    @pytest.mark.asyncio()
    async def test_stop_noop_when_engine_stopped(self) -> None:
        """stop_relay is a no-op when engine exists but is stopped."""
        svc = RelayService()
        mock_engine = MagicMock()
        mock_engine.is_running = False
        svc._engine = mock_engine

        await svc.stop_relay()
        mock_engine.stop.assert_not_called()

    @pytest.mark.asyncio()
    async def test_start_log_includes_trigger_input_and_destinations(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """v0.3.30: start log includes trigger + input source + dest names."""
        svc = RelayService()
        input_cfg = _make_input_config()
        dests = [_make_dest_config()]

        with patch.object(_relay_module, "RelayEngine") as mock_cls:
            mock_engine = MagicMock()
            mock_engine.is_running = False
            mock_cls.return_value = mock_engine
            with caplog.at_level("INFO", logger="sp_rtk_base.services.relay_service"):
                await svc.start_relay(
                    input_cfg, dests, trigger="auto-start (attempt 2)"
                )

        msgs = [r.getMessage() for r in caplog.records if "started" in r.getMessage()]
        assert any("Relay engine started" in m for m in msgs)
        line = next(m for m in msgs if "Relay engine started" in m)
        assert "trigger=auto-start (attempt 2)" in line
        assert "input=tcp(127.0.0.1:5015)" in line
        assert "rtk2go" in line  # destination name surfaces

    @pytest.mark.asyncio()
    async def test_stop_log_includes_trigger_and_uptime(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """v0.3.30: stop log includes trigger, uptime, bytes_in, chunks_out."""
        svc = RelayService()
        input_cfg = _make_input_config()

        with patch.object(_relay_module, "RelayEngine") as mock_cls:
            mock_engine = MagicMock()
            # First running=False (start ok), then running=True (so stop runs).
            mock_engine.is_running = False
            mock_cls.return_value = mock_engine
            await svc.start_relay(input_cfg, trigger="api")

            # Engine reports throughput via get_status() right before stop.
            mock_engine.is_running = True
            mock_engine.get_status.return_value = _MockRelayStatus(
                bytes_received=2048, chunks_distributed=42
            )

            with caplog.at_level("INFO", logger="sp_rtk_base.services.relay_service"):
                await svc.stop_relay(trigger="shutdown")

        msgs = [r.getMessage() for r in caplog.records if "stopped" in r.getMessage()]
        assert msgs, "expected a 'Relay engine stopped' log line"
        line = msgs[-1]
        assert "trigger=shutdown" in line
        assert "uptime=" in line
        assert "bytes_in=2.0 KB" in line
        assert "chunks_out=42" in line
        assert "(started by api)" in line  # cross-references the start trigger

    @pytest.mark.asyncio()
    async def test_default_trigger_is_unknown(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Callers that don't pass a trigger still get a sensible default."""
        svc = RelayService()
        input_cfg = _make_input_config()
        with patch.object(_relay_module, "RelayEngine") as mock_cls:
            mock_engine = MagicMock()
            mock_engine.is_running = False
            mock_cls.return_value = mock_engine
            with caplog.at_level("INFO", logger="sp_rtk_base.services.relay_service"):
                await svc.start_relay(input_cfg)
        assert any("trigger=unknown" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio()
    async def test_recreates_engine_on_config_change(self) -> None:
        """start_relay recreates engine if input config changes."""
        svc = RelayService()
        input1 = InputConfig(source="tcp", config={"host": "1.1.1.1", "port": 5015})
        input2 = InputConfig(source="tcp", config={"host": "2.2.2.2", "port": 5015})

        with patch.object(_relay_module, "RelayEngine") as mock_cls:
            engine1 = MagicMock()
            engine1.is_running = False
            engine2 = MagicMock()
            engine2.is_running = False
            mock_cls.side_effect = [engine1, engine2]

            await svc.start_relay(input1)
            assert svc.engine is engine1

            # Stop so we can restart
            engine1.is_running = False

            await svc.start_relay(input2)
            assert svc.engine is engine2
            assert mock_cls.call_count == 2


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestRelayServiceStatus:
    """Tests for status queries."""

    @pytest.mark.asyncio()
    async def test_get_status_when_running(self) -> None:
        """get_status returns RelayStatus when engine is running."""
        svc = RelayService()
        mock_engine = MagicMock()
        mock_engine.is_running = True
        mock_engine.get_status.return_value = _MockRelayStatus()
        svc._engine = mock_engine

        status = await svc.get_status()
        assert status is not None
        assert status.running is True

    @pytest.mark.asyncio()
    async def test_get_status_when_not_running(self) -> None:
        """get_status returns None when engine is not running."""
        svc = RelayService()
        assert await svc.get_status() is None

    @pytest.mark.asyncio()
    async def test_get_status_handles_service_error(self) -> None:
        """get_status returns None if engine throws ServiceError."""
        svc = RelayService()
        mock_engine = MagicMock()
        mock_engine.is_running = True
        mock_engine.get_status.side_effect = ServiceError("stopped")
        svc._engine = mock_engine

        status = await svc.get_status()
        assert status is None

    def test_is_running_property(self) -> None:
        """is_running reflects engine state."""
        svc = RelayService()
        assert svc.is_running is False

        mock_engine = MagicMock()
        mock_engine.is_running = True
        svc._engine = mock_engine
        assert svc.is_running is True


# ---------------------------------------------------------------------------
# Destination management
# ---------------------------------------------------------------------------


class TestRelayServiceDestinations:
    """Tests for dynamic destination management."""

    @pytest.mark.asyncio()
    async def test_add_destination(self) -> None:
        """add_destination delegates to engine."""
        svc = RelayService()
        mock_engine = MagicMock()
        mock_engine.is_running = True
        mock_engine.add_destination.return_value = "rtk2go"
        svc._engine = mock_engine

        name = await svc.add_destination(_make_dest_config())
        assert name == "rtk2go"
        mock_engine.add_destination.assert_called_once()

    @pytest.mark.asyncio()
    async def test_add_destination_when_not_running(self) -> None:
        """add_destination raises ServiceError when not running."""
        svc = RelayService()
        with pytest.raises(ServiceError):
            await svc.add_destination(_make_dest_config())

    @pytest.mark.asyncio()
    async def test_remove_destination(self) -> None:
        """remove_destination delegates to engine."""
        svc = RelayService()
        mock_engine = MagicMock()
        mock_engine.is_running = True
        svc._engine = mock_engine

        await svc.remove_destination("rtk2go")
        mock_engine.remove_destination.assert_called_once_with("rtk2go")

    @pytest.mark.asyncio()
    async def test_start_destination(self) -> None:
        """start_destination delegates to engine."""
        svc = RelayService()
        mock_engine = MagicMock()
        mock_engine.is_running = True
        svc._engine = mock_engine

        await svc.start_destination("rtk2go")
        mock_engine.start_destination.assert_called_once_with("rtk2go")

    @pytest.mark.asyncio()
    async def test_stop_destination(self) -> None:
        """stop_destination delegates to engine."""
        svc = RelayService()
        mock_engine = MagicMock()
        mock_engine.is_running = True
        svc._engine = mock_engine

        await svc.stop_destination("rtk2go")
        mock_engine.stop_destination.assert_called_once_with("rtk2go")

    def test_get_destination_names(self) -> None:
        """get_destination_names delegates to engine."""
        svc = RelayService()
        mock_engine = MagicMock()
        mock_engine.get_destination_names.return_value = ["rtk2go", "local-tcp"]
        svc._engine = mock_engine

        names = svc.get_destination_names()
        assert names == ["rtk2go", "local-tcp"]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestRelayServiceEvents:
    """Tests for event subscription and recent events."""

    def test_subscribe_events(self) -> None:
        """subscribe_events delegates to engine."""
        svc = RelayService()
        mock_engine = MagicMock()
        mock_sub = MagicMock()
        mock_engine.subscribe_events.return_value = mock_sub
        svc._engine = mock_engine

        sub = svc.subscribe_events()
        assert sub is mock_sub

    def test_get_recent_events(self) -> None:
        """get_recent_events returns serialized events."""
        svc = RelayService()
        mock_engine = MagicMock()

        from sp_rtk_base_relay import RelayEvent

        mock_events = [
            RelayEvent(
                event_type="engine.started",
                message="Engine started",
                timestamp=100.0,
                payload={"destination_count": 1},
            ),
        ]
        mock_engine.get_recent_events.return_value = mock_events
        svc._engine = mock_engine

        events = svc.get_recent_events(10)
        assert len(events) == 1
        assert events[0]["event_type"] == "engine.started"
        assert events[0]["message"] == "Engine started"
