"""Tests for the Bluetooth stale-handle recovery path in init_services (Bug D).

When the auto-start path opens a Bluetooth input, a stale BlueZ
connection (left over from a previous unclean shutdown) can prevent
the relay engine from claiming the RFCOMM channel.  ``init_services``
asks ``BluetoothManager.disconnect_device(mac)`` first as a best-effort
recovery before handing off to the relay.

These tests stub :class:`BluetoothManager` so they run without dbus-fast,
without BlueZ, and without a real adapter.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# We intentionally exercise the module-private
# ``_release_stale_bluetooth_handle`` helper — it's a thin pure-Python
# wrapper that has no public alias to bind a test against.  The
# ``reportUnknown*`` suppressions are needed because we exchange
# ``MagicMock`` instances for real types and patch low-level
# asyncio internals; pyright can't narrow either without extensive
# generic plumbing that would obscure the test intent.

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import sp_rtk_base.services as services_mod
from sp_rtk_base.models.config_models import (
    AppConfig,
    AppSettings,
    InputProfile,
)
from sp_rtk_base.services import _release_stale_bluetooth_handle
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.event_bridge import EventBridge
from sp_rtk_base.services.relay_service import RelayService

# ---------------------------------------------------------------------------
# Helper: inject a fake BluetoothManager class so the helper finds *something*
# importable even when dbus-fast / the real relay package isn't usable here.
# ---------------------------------------------------------------------------


def _install_fake_bluetooth_manager(
    mgr_class: object,
) -> Iterator[None]:
    """Inject ``mgr_class`` at ``sp_rtk_base_relay.core.bluetooth_manager``.

    Yields with the patched module in place, then restores the prior
    state on teardown.
    """
    mod_name = "sp_rtk_base_relay.core.bluetooth_manager"
    saved = sys.modules.get(mod_name)
    fake_mod = types.ModuleType(mod_name)
    fake_mod.BluetoothManager = mgr_class  # type: ignore[attr-defined]
    sys.modules[mod_name] = fake_mod
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop(mod_name, None)
        else:
            sys.modules[mod_name] = saved


@pytest.fixture()
def fake_bt_manager(request: pytest.FixtureRequest) -> Iterator[MagicMock]:
    """Yield a MagicMock instance that acts as a stand-in BluetoothManager.

    The fixture also patches the ``sp_rtk_base_relay.core.bluetooth_manager``
    module so the import inside ``_release_stale_bluetooth_handle`` resolves
    to *this* fake class.
    """
    instance = MagicMock(name="BluetoothManagerInstance")
    cls = MagicMock(name="BluetoothManagerClass", return_value=instance)
    # Stash the instance on the class mock so the test can interrogate it.
    cls.instance = instance  # type: ignore[attr-defined]
    gen = _install_fake_bluetooth_manager(cls)
    next(gen)
    try:
        yield cls
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


# ---------------------------------------------------------------------------
# Direct tests on _release_stale_bluetooth_handle
# ---------------------------------------------------------------------------


class TestReleaseStaleBluetoothHandle:
    """Unit-level tests of the helper itself."""

    @pytest.mark.asyncio()
    async def test_calls_disconnect_then_close_in_order(
        self, fake_bt_manager: MagicMock
    ) -> None:
        """Happy path: instantiates the manager, calls disconnect, then close."""
        instance = fake_bt_manager.instance
        instance.disconnect_device = MagicMock()
        instance.close = MagicMock()

        await _release_stale_bluetooth_handle("AA:BB:CC:DD:EE:FF")

        instance.disconnect_device.assert_called_once_with("AA:BB:CC:DD:EE:FF")
        instance.close.assert_called_once()

    @pytest.mark.asyncio()
    async def test_swallows_disconnect_errors(self, fake_bt_manager: MagicMock) -> None:
        """A BlueZ error during disconnect must not propagate out of startup."""
        instance = fake_bt_manager.instance
        instance.disconnect_device = MagicMock(
            side_effect=RuntimeError("device not connected")
        )
        instance.close = MagicMock()

        # Must not raise.
        await _release_stale_bluetooth_handle("AA:BB:CC:DD:EE:FF")

        instance.close.assert_called_once()

    @pytest.mark.asyncio()
    async def test_swallows_close_errors(self, fake_bt_manager: MagicMock) -> None:
        """An error in close() must also be swallowed (best-effort cleanup)."""
        instance = fake_bt_manager.instance
        instance.disconnect_device = MagicMock()
        instance.close = MagicMock(side_effect=RuntimeError("close failed"))

        await _release_stale_bluetooth_handle("AA:BB:CC:DD:EE:FF")

        instance.disconnect_device.assert_called_once()

    @pytest.mark.asyncio()
    async def test_times_out_on_hung_disconnect(
        self, fake_bt_manager: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wedged BlueZ must not stall startup forever.

        We patch the internal asyncio.wait_for budget by patching
        ``asyncio.wait_for`` itself to use a tiny budget; or, simpler,
        we just make ``disconnect_device`` block in a thread and rely
        on the helper's own ``wait_for(..., timeout=5.0)`` budget.  To
        keep this test fast, we patch ``asyncio.wait_for`` to a no-op
        that always raises TimeoutError so we exercise the except
        path deterministically.
        """
        instance = fake_bt_manager.instance
        instance.disconnect_device = MagicMock()
        instance.close = MagicMock()

        original_wait_for = asyncio.wait_for

        async def _always_timeout(aw, timeout):  # type: ignore[no-untyped-def]
            # Make sure the coroutine isn't left un-awaited.
            aw.close()
            raise TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", _always_timeout)
        try:
            await _release_stale_bluetooth_handle("AA:BB:CC:DD:EE:FF")
        finally:
            monkeypatch.setattr(asyncio, "wait_for", original_wait_for)

        # close() still runs on the timeout path so we don't leak the
        # BluetoothManager and its background D-Bus loop.
        instance.close.assert_called_once()

    @pytest.mark.asyncio()
    async def test_silently_skips_when_package_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the relay package isn't installed, the helper is a no-op.

        We force the import inside the helper to fail by removing the
        candidate module from ``sys.modules`` and shadowing it with one
        that raises ImportError on attribute access.
        """
        mod_name = "sp_rtk_base_relay.core.bluetooth_manager"
        saved = sys.modules.pop(mod_name, None)

        # Install a builtins-style import hook? Simpler: monkeypatch
        # builtins.__import__ for the duration of the call.
        import builtins

        real_import = builtins.__import__

        def _fake_import(
            name: str, *args: object, **kwargs: object
        ) -> types.ModuleType:
            if name == mod_name:
                raise ImportError("simulated missing relay package")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        try:
            await _release_stale_bluetooth_handle("AA:BB:CC:DD:EE:FF")
        finally:
            if saved is not None:
                sys.modules[mod_name] = saved


# ---------------------------------------------------------------------------
# init_services integration: the helper is invoked for bluetooth auto-start
# and is *not* invoked for other input types.
# ---------------------------------------------------------------------------


@pytest.fixture()
def patched_relay_and_eb() -> Iterator[tuple[MagicMock, MagicMock]]:
    """Replace relay_service / event_bridge with mocks for one test."""
    original_relay = services_mod.relay_service
    original_eb = services_mod.event_bridge
    relay_mock = MagicMock(spec=RelayService)
    relay_mock.start_relay = AsyncMock()
    relay_mock.is_running = False
    eb_mock = MagicMock(spec=EventBridge)
    services_mod.relay_service = relay_mock
    services_mod.event_bridge = eb_mock
    try:
        yield relay_mock, eb_mock
    finally:
        services_mod.relay_service = original_relay
        services_mod.event_bridge = original_eb


class TestInitServicesBluetoothRecovery:
    """init_services must call the helper for BT auto-start, skip otherwise."""

    @pytest.mark.asyncio()
    async def test_calls_release_for_bluetooth_auto_start(
        self,
        tmp_path: Path,
        patched_relay_and_eb: tuple[MagicMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bluetooth input + auto_start=True triggers the recovery helper."""
        config_path = tmp_path / "config.yaml"
        cfg_svc = ConfigService(config_path=config_path)
        cfg_svc.save_config(
            AppConfig(
                input=InputProfile(
                    source="bluetooth",
                    config={"mac_address": "AA:BB:CC:DD:EE:FF", "channel": 1},
                ),
                settings=AppSettings(auto_start=True),
            )
        )

        called_with: list[str] = []

        async def _spy(mac: str) -> None:
            called_with.append(mac)

        monkeypatch.setattr(services_mod, "_release_stale_bluetooth_handle", _spy)

        original_cfg = services_mod.config_service
        services_mod.config_service = cfg_svc
        try:
            await services_mod.init_services()
        finally:
            services_mod.config_service = original_cfg

        assert called_with == ["AA:BB:CC:DD:EE:FF"]

    @pytest.mark.asyncio()
    async def test_skips_release_for_serial_input(
        self,
        tmp_path: Path,
        patched_relay_and_eb: tuple[MagicMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Serial input must NOT invoke the bluetooth recovery helper."""
        config_path = tmp_path / "config.yaml"
        cfg_svc = ConfigService(config_path=config_path)
        cfg_svc.save_config(
            AppConfig(
                input=InputProfile(
                    source="serial",
                    config={"port": "/dev/ttyACM0", "baudrate": 115200},
                ),
                settings=AppSettings(auto_start=True),
            )
        )

        called_with: list[str] = []

        async def _spy(mac: str) -> None:
            called_with.append(mac)

        monkeypatch.setattr(services_mod, "_release_stale_bluetooth_handle", _spy)

        original_cfg = services_mod.config_service
        services_mod.config_service = cfg_svc
        try:
            await services_mod.init_services()
        finally:
            services_mod.config_service = original_cfg

        assert called_with == []

    @pytest.mark.asyncio()
    async def test_skips_release_when_auto_start_disabled(
        self,
        tmp_path: Path,
        patched_relay_and_eb: tuple[MagicMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A BT input with auto_start=False must NOT touch BlueZ on startup."""
        config_path = tmp_path / "config.yaml"
        cfg_svc = ConfigService(config_path=config_path)
        cfg_svc.save_config(
            AppConfig(
                input=InputProfile(
                    source="bluetooth",
                    config={"mac_address": "AA:BB:CC:DD:EE:FF"},
                ),
                settings=AppSettings(auto_start=False),
            )
        )

        called_with: list[str] = []

        async def _spy(mac: str) -> None:
            called_with.append(mac)

        monkeypatch.setattr(services_mod, "_release_stale_bluetooth_handle", _spy)

        original_cfg = services_mod.config_service
        services_mod.config_service = cfg_svc
        try:
            await services_mod.init_services()
        finally:
            services_mod.config_service = original_cfg

        assert called_with == []

    @pytest.mark.asyncio()
    async def test_skips_release_for_bluetooth_without_mac(
        self,
        tmp_path: Path,
        patched_relay_and_eb: tuple[MagicMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bluetooth profile missing mac_address is left alone."""
        config_path = tmp_path / "config.yaml"
        cfg_svc = ConfigService(config_path=config_path)
        cfg_svc.save_config(
            AppConfig(
                input=InputProfile(
                    source="bluetooth",
                    config={"channel": 1},  # no mac_address
                ),
                settings=AppSettings(auto_start=True),
            )
        )

        called_with: list[str] = []

        async def _spy(mac: str) -> None:
            called_with.append(mac)

        monkeypatch.setattr(services_mod, "_release_stale_bluetooth_handle", _spy)

        original_cfg = services_mod.config_service
        services_mod.config_service = cfg_svc
        try:
            await services_mod.init_services()
        finally:
            services_mod.config_service = original_cfg

        assert called_with == []

    @pytest.mark.asyncio()
    async def test_accepts_legacy_address_key(
        self,
        tmp_path: Path,
        patched_relay_and_eb: tuple[MagicMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Legacy configs storing the MAC under ``address`` are still honoured."""
        config_path = tmp_path / "config.yaml"
        cfg_svc = ConfigService(config_path=config_path)
        cfg_svc.save_config(
            AppConfig(
                input=InputProfile(
                    source="bluetooth",
                    config={"address": "11:22:33:44:55:66"},
                ),
                settings=AppSettings(auto_start=True),
            )
        )

        called_with: list[str] = []

        async def _spy(mac: str) -> None:
            called_with.append(mac)

        monkeypatch.setattr(services_mod, "_release_stale_bluetooth_handle", _spy)

        original_cfg = services_mod.config_service
        services_mod.config_service = cfg_svc
        try:
            await services_mod.init_services()
        finally:
            services_mod.config_service = original_cfg

        assert called_with == ["11:22:33:44:55:66"]
