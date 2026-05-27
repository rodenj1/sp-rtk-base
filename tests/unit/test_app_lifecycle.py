"""Tests for sp_rtk_base.app lifecycle hooks (Bug B + adjacent).

These exercise the module-scope ``startup_services`` and
``shutdown_services`` functions in :mod:`sp_rtk_base.app` so that the
device → event-bridge → relay teardown order is enforced and a
stuck driver can never hold up shutdown indefinitely.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

import sp_rtk_base.services as services_mod
from sp_rtk_base.app import (
    DEVICE_DISCONNECT_TIMEOUT_SECONDS,
    shutdown_services,
    startup_services,
)
from sp_rtk_base.services.device_service import DeviceService
from sp_rtk_base.services.event_bridge import EventBridge
from sp_rtk_base.services.relay_service import RelayService

# ---------------------------------------------------------------------------
# Fixtures: replace the three singletons with mocks for the duration of a test
# ---------------------------------------------------------------------------


@pytest.fixture()
def patched_services() -> Iterator[tuple[MagicMock, MagicMock, MagicMock]]:
    """Swap the device/event-bridge/relay singletons with mocks.

    Yields:
        (device_service_mock, event_bridge_mock, relay_service_mock)
    """
    original_device = services_mod.device_service
    original_eb = services_mod.event_bridge
    original_relay = services_mod.relay_service

    device_mock = MagicMock(spec=DeviceService)
    # is_available / is_connected are @property — back them with attribute
    # values instead of method calls so the production code path works.
    type(device_mock).is_available = True  # type: ignore[misc]
    type(device_mock).is_connected = True  # type: ignore[misc]
    device_mock.disconnect = AsyncMock()

    eb_mock = MagicMock(spec=EventBridge)

    relay_mock = MagicMock(spec=RelayService)
    relay_mock.stop_relay = AsyncMock()

    services_mod.device_service = device_mock
    services_mod.event_bridge = eb_mock
    services_mod.relay_service = relay_mock
    try:
        yield device_mock, eb_mock, relay_mock
    finally:
        services_mod.device_service = original_device
        services_mod.event_bridge = original_eb
        services_mod.relay_service = original_relay
        # Restore class-level property surrogates so they don't leak.
        try:
            del type(device_mock).is_available  # type: ignore[misc]
        except AttributeError:
            pass
        try:
            del type(device_mock).is_connected  # type: ignore[misc]
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# startup_services — thin wrapper, but worth a sanity check
# ---------------------------------------------------------------------------


class TestStartupServices:
    """Tests for the startup lifecycle hook."""

    @pytest.mark.asyncio()
    async def test_startup_delegates_to_init_services(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """startup_services calls init_services exactly once."""
        called: list[str] = []

        async def _fake_init() -> None:
            called.append("init")

        monkeypatch.setattr(services_mod, "init_services", _fake_init)
        await startup_services()
        assert called == ["init"]


# ---------------------------------------------------------------------------
# shutdown_services — the substance of Bug B
# ---------------------------------------------------------------------------


class TestShutdownServicesOrdering:
    """The shutdown order must be device → event-bridge → relay."""

    @pytest.mark.asyncio()
    async def test_all_three_services_are_stopped(
        self, patched_services: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Happy path: disconnect, event_bridge.stop, relay.stop all run."""
        device, eb, relay = patched_services
        await shutdown_services()
        device.disconnect.assert_awaited_once()
        eb.stop.assert_called_once()
        relay.stop_relay.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_device_disconnects_before_event_bridge_stops(
        self, patched_services: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Device disconnect happens before event-bridge stop."""
        device, eb, _relay = patched_services
        order: list[str] = []

        async def _disconnect() -> None:
            order.append("device")

        def _eb_stop() -> None:
            order.append("eb")

        device.disconnect.side_effect = _disconnect
        eb.stop.side_effect = _eb_stop

        await shutdown_services()
        assert order.index("device") < order.index("eb")

    @pytest.mark.asyncio()
    async def test_event_bridge_stops_before_relay_stops(
        self, patched_services: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Event bridge stop happens before relay stop."""
        _device, eb, relay = patched_services
        order: list[str] = []

        def _eb_stop() -> None:
            order.append("eb")

        async def _relay_stop() -> None:
            order.append("relay")

        eb.stop.side_effect = _eb_stop
        relay.stop_relay.side_effect = _relay_stop

        await shutdown_services()
        assert order.index("eb") < order.index("relay")


class TestShutdownServicesResilience:
    """A failure in one service must not block teardown of the others."""

    @pytest.mark.asyncio()
    async def test_device_disconnect_error_does_not_block_relay_stop(
        self, patched_services: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """If device.disconnect() raises, relay still shuts down."""
        device, eb, relay = patched_services
        device.disconnect.side_effect = RuntimeError("driver explodes")

        await shutdown_services()  # must NOT raise

        device.disconnect.assert_awaited_once()
        eb.stop.assert_called_once()
        relay.stop_relay.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_event_bridge_stop_error_does_not_block_relay_stop(
        self, patched_services: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """If event_bridge.stop() raises, relay still shuts down."""
        _device, eb, relay = patched_services
        eb.stop.side_effect = RuntimeError("bridge explodes")

        await shutdown_services()

        relay.stop_relay.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_relay_stop_error_is_swallowed(
        self, patched_services: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """If relay.stop_relay() raises, shutdown still returns cleanly."""
        _device, _eb, relay = patched_services
        relay.stop_relay.side_effect = RuntimeError("relay explodes")

        await shutdown_services()  # must NOT raise

    @pytest.mark.asyncio()
    async def test_device_disconnect_timeout_is_bounded(
        self, patched_services: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """A driver that never returns from disconnect() must not stall shutdown.

        The shutdown path wraps ``device.disconnect()`` in
        :func:`asyncio.wait_for` with a budget of
        :data:`DEVICE_DISCONNECT_TIMEOUT_SECONDS`.  We patch the budget
        down to something tiny in this test so the assertion runs fast,
        but the production budget is many seconds.
        """
        device, eb, relay = patched_services

        async def _hang() -> None:
            await asyncio.sleep(10)  # would block forever in practice

        device.disconnect.side_effect = _hang

        # Patch the timeout down to something small for test speed.
        import sp_rtk_base.app as app_mod

        original_budget = app_mod.DEVICE_DISCONNECT_TIMEOUT_SECONDS
        app_mod.DEVICE_DISCONNECT_TIMEOUT_SECONDS = 0.05
        try:
            await asyncio.wait_for(shutdown_services(), timeout=2.0)
        finally:
            app_mod.DEVICE_DISCONNECT_TIMEOUT_SECONDS = original_budget

        # Even though the device hangs, the other two services still ran.
        eb.stop.assert_called_once()
        relay.stop_relay.assert_awaited_once()


class TestShutdownServicesWithIdleDevice:
    """When no device is connected, we skip disconnect() entirely."""

    @pytest.mark.asyncio()
    async def test_skips_disconnect_when_no_driver_registered(
        self, patched_services: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """If is_available is False, disconnect() is never awaited."""
        device, eb, relay = patched_services
        type(device).is_available = False  # type: ignore[misc]
        type(device).is_connected = False  # type: ignore[misc]

        await shutdown_services()

        device.disconnect.assert_not_called()
        eb.stop.assert_called_once()
        relay.stop_relay.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_skips_disconnect_when_driver_already_disconnected(
        self, patched_services: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """If is_available=True but is_connected=False, disconnect is skipped."""
        device, _eb, _relay = patched_services
        type(device).is_available = True  # type: ignore[misc]
        type(device).is_connected = False  # type: ignore[misc]

        await shutdown_services()

        device.disconnect.assert_not_called()


# ---------------------------------------------------------------------------
# Disconnect budget is configurable + sane
# ---------------------------------------------------------------------------


class TestDeviceDisconnectTimeoutConstant:
    """The module-scope timeout knob should be set to something defensible."""

    def test_timeout_is_positive(self) -> None:
        """Negative or zero timeouts would cause TimeoutError immediately."""
        assert DEVICE_DISCONNECT_TIMEOUT_SECONDS > 0

    def test_timeout_is_at_least_a_few_seconds(self) -> None:
        """Bluetooth needs a few seconds of headroom on a healthy bus."""
        # If we ever drop this below 3s, we'll routinely timeout on
        # operator hardware — make that a conscious decision.
        assert DEVICE_DISCONNECT_TIMEOUT_SECONDS >= 3.0
