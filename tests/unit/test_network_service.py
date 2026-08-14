"""Tests for NetworkService — console bridge over NmcliAdapter (issues #22-24).

The nmcli/subprocess boundary itself is covered exhaustively by
``test_net_provision_nmcli_adapter.py``; these tests only cover what
this service adds on top: lazy config loading, the "not configured"
error mapping, adapter caching, awaiting the sync adapter calls via
``asyncio.to_thread``, and the fire-and-acknowledge scheduling of
``connect_to_network``, ``switch_to_network``, and ``forget_network``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from sp_rtk_base.models.net_provision_models import (
    ActiveLink,
    LinkType,
    NetProvisionConfig,
    SavedWifiConnection,
    WifiNetwork,
)
from sp_rtk_base.services.net_provision.config_loader import NetProvisionConfigError
from sp_rtk_base.services.net_provision.nmcli_adapter import NmcliAdapter
from sp_rtk_base.services.network_service import (
    NetworkNotConfiguredError,
    NetworkService,
)


def _config() -> NetProvisionConfig:
    return NetProvisionConfig(ap_password="sticker-secret")


def _fake_adapter() -> NmcliAdapter:
    return MagicMock(spec=NmcliAdapter)


def _service_with_adapter(adapter: NmcliAdapter) -> NetworkService:
    return NetworkService(config_loader=_config, adapter_factory=lambda _cfg: adapter)


def _service_unconfigured() -> NetworkService:
    def _raise_config_error() -> NetProvisionConfig:
        raise NetProvisionConfigError("Net-provisioning config not found")

    return NetworkService(
        config_loader=_raise_config_error,
        adapter_factory=lambda _cfg: _fake_adapter(),
    )


class TestGetActiveLink:
    @pytest.mark.asyncio()
    async def test_returns_the_adapter_reading(self) -> None:
        adapter = _fake_adapter()
        link = ActiveLink(link_type=LinkType.WIRED, name="Wired connection 1")
        adapter.read_active_link.return_value = link  # type: ignore[attr-defined]
        svc = _service_with_adapter(adapter)
        assert await svc.get_active_link() is link

    @pytest.mark.asyncio()
    async def test_returns_none_when_nothing_is_connected(self) -> None:
        adapter = _fake_adapter()
        adapter.read_active_link.return_value = None  # type: ignore[attr-defined]
        svc = _service_with_adapter(adapter)
        assert await svc.get_active_link() is None

    @pytest.mark.asyncio()
    async def test_raises_not_configured_when_config_is_missing(self) -> None:
        svc = _service_unconfigured()
        with pytest.raises(NetworkNotConfiguredError):
            await svc.get_active_link()


class TestScanNetworks:
    @pytest.mark.asyncio()
    async def test_returns_the_adapter_scan(self) -> None:
        adapter = _fake_adapter()
        networks = [WifiNetwork(ssid="SiteWiFi", signal=80, security="WPA2")]
        adapter.scan_networks.return_value = networks  # type: ignore[attr-defined]
        svc = _service_with_adapter(adapter)
        assert await svc.scan_networks() == networks

    @pytest.mark.asyncio()
    async def test_raises_not_configured_when_config_is_missing(self) -> None:
        svc = _service_unconfigured()
        with pytest.raises(NetworkNotConfiguredError):
            await svc.scan_networks()


class TestGetApFallbackInfo:
    @pytest.mark.asyncio()
    async def test_returns_ap_ssid_and_window_from_config(self) -> None:
        adapter = _fake_adapter()
        svc = _service_with_adapter(adapter)
        info = await svc.get_ap_fallback_info()
        assert info.ap_ssid == "sp-rtk-base-setup"
        assert info.fallback_window_seconds == 300.0

    @pytest.mark.asyncio()
    async def test_raises_not_configured_when_config_is_missing(self) -> None:
        svc = _service_unconfigured()
        with pytest.raises(NetworkNotConfiguredError):
            await svc.get_ap_fallback_info()


class TestConnectToNetwork:
    @pytest.mark.asyncio()
    async def test_schedules_the_adapter_connect_and_returns_immediately(
        self,
    ) -> None:
        adapter = _fake_adapter()
        svc = _service_with_adapter(adapter)
        await svc.connect_to_network("SiteWiFi", "hunter22")
        # Not called yet — the attempt runs in the background, off-loop.
        adapter.connect_to_network.assert_not_called()  # type: ignore[attr-defined]
        assert svc.last_connect_task is not None
        await svc.last_connect_task
        adapter.connect_to_network.assert_called_once_with(  # type: ignore[attr-defined]
            "SiteWiFi", "hunter22", hidden=False
        )

    @pytest.mark.asyncio()
    async def test_hidden_ssid_is_forwarded(self) -> None:
        adapter = _fake_adapter()
        svc = _service_with_adapter(adapter)
        await svc.connect_to_network("HiddenNet", "hunter22", hidden=True)
        assert svc.last_connect_task is not None
        await svc.last_connect_task
        adapter.connect_to_network.assert_called_once_with(  # type: ignore[attr-defined]
            "HiddenNet", "hunter22", hidden=True
        )

    @pytest.mark.asyncio()
    async def test_raises_not_configured_synchronously_when_config_is_missing(
        self,
    ) -> None:
        svc = _service_unconfigured()
        with pytest.raises(NetworkNotConfiguredError):
            await svc.connect_to_network("SiteWiFi", "hunter22")
        assert svc.last_connect_task is None

    @pytest.mark.asyncio()
    async def test_background_connect_failure_does_not_propagate(self) -> None:
        """A failed connect attempt is logged, not raised — by the time
        nmcli reports it, the request that triggered it has already
        returned its acknowledgement."""
        adapter = _fake_adapter()
        adapter.connect_to_network.side_effect = RuntimeError(  # type: ignore[attr-defined]
            "wrong password"
        )
        svc = _service_with_adapter(adapter)
        await svc.connect_to_network("SiteWiFi", "wrong")
        assert svc.last_connect_task is not None
        # The background task itself completed with the error captured,
        # not re-raised to whoever happens to await it here.
        await asyncio.wait([svc.last_connect_task])
        assert svc.last_connect_task.exception() is not None


class TestListSavedNetworks:
    @pytest.mark.asyncio()
    async def test_returns_the_adapter_listing(self) -> None:
        adapter = _fake_adapter()
        connections = [
            SavedWifiConnection(name="SiteWiFi", active=True),
            SavedWifiConnection(name="OldWiFi", active=False),
        ]
        adapter.list_saved_connections.return_value = connections  # type: ignore[attr-defined]
        svc = _service_with_adapter(adapter)
        assert await svc.list_saved_networks() == connections

    @pytest.mark.asyncio()
    async def test_raises_not_configured_when_config_is_missing(self) -> None:
        svc = _service_unconfigured()
        with pytest.raises(NetworkNotConfiguredError):
            await svc.list_saved_networks()


class TestSwitchToNetwork:
    @pytest.mark.asyncio()
    async def test_schedules_the_adapter_activate_and_returns_immediately(
        self,
    ) -> None:
        adapter = _fake_adapter()
        svc = _service_with_adapter(adapter)
        await svc.switch_to_network("SiteWiFi")
        # Not called yet — the attempt runs in the background, off-loop.
        adapter.activate_connection.assert_not_called()  # type: ignore[attr-defined]
        assert svc.last_switch_task is not None
        await svc.last_switch_task
        adapter.activate_connection.assert_called_once_with(  # type: ignore[attr-defined]
            "SiteWiFi"
        )

    @pytest.mark.asyncio()
    async def test_raises_not_configured_synchronously_when_config_is_missing(
        self,
    ) -> None:
        svc = _service_unconfigured()
        with pytest.raises(NetworkNotConfiguredError):
            await svc.switch_to_network("SiteWiFi")
        assert svc.last_switch_task is None

    @pytest.mark.asyncio()
    async def test_background_switch_failure_does_not_propagate(self) -> None:
        adapter = _fake_adapter()
        adapter.activate_connection.side_effect = RuntimeError(  # type: ignore[attr-defined]
            "connection failed"
        )
        svc = _service_with_adapter(adapter)
        await svc.switch_to_network("SiteWiFi")
        assert svc.last_switch_task is not None
        await asyncio.wait([svc.last_switch_task])
        assert svc.last_switch_task.exception() is not None


class TestForgetNetwork:
    @pytest.mark.asyncio()
    async def test_schedules_the_adapter_forget_and_returns_immediately(self) -> None:
        adapter = _fake_adapter()
        svc = _service_with_adapter(adapter)
        await svc.forget_network("OldWiFi")
        # Not called yet — the attempt runs in the background, off-loop.
        adapter.forget_connection.assert_not_called()  # type: ignore[attr-defined]
        assert svc.last_forget_task is not None
        await svc.last_forget_task
        adapter.forget_connection.assert_called_once_with(  # type: ignore[attr-defined]
            "OldWiFi"
        )

    @pytest.mark.asyncio()
    async def test_forgetting_the_active_network_uses_the_same_background_path(
        self,
    ) -> None:
        """Forgetting the active network can drop this very request's own
        connection — this must go through the same fire-and-acknowledge
        path as any other forget, not a synchronous delete."""
        adapter = _fake_adapter()
        svc = _service_with_adapter(adapter)
        await svc.forget_network("SiteWiFi")
        adapter.forget_connection.assert_not_called()  # type: ignore[attr-defined]
        assert svc.last_forget_task is not None
        await svc.last_forget_task
        adapter.forget_connection.assert_called_once_with(  # type: ignore[attr-defined]
            "SiteWiFi"
        )

    @pytest.mark.asyncio()
    async def test_raises_not_configured_synchronously_when_config_is_missing(
        self,
    ) -> None:
        svc = _service_unconfigured()
        with pytest.raises(NetworkNotConfiguredError):
            await svc.forget_network("SiteWiFi")
        assert svc.last_forget_task is None

    @pytest.mark.asyncio()
    async def test_background_forget_failure_does_not_propagate(self) -> None:
        adapter = _fake_adapter()
        adapter.forget_connection.side_effect = RuntimeError(  # type: ignore[attr-defined]
            "delete failed"
        )
        svc = _service_with_adapter(adapter)
        await svc.forget_network("SiteWiFi")
        assert svc.last_forget_task is not None
        await asyncio.wait([svc.last_forget_task])
        assert svc.last_forget_task.exception() is not None


class TestAdapterCaching:
    @pytest.mark.asyncio()
    async def test_config_is_loaded_only_once(self) -> None:
        """The adapter is built once and reused — repeatedly re-reading
        and re-validating the YAML config on every status poll would be
        wasteful, and the config never changes without a restart anyway."""
        adapter = _fake_adapter()
        adapter.read_active_link.return_value = None  # type: ignore[attr-defined]
        adapter.scan_networks.return_value = []  # type: ignore[attr-defined]
        config_loader = MagicMock(return_value=_config())
        svc = NetworkService(
            config_loader=config_loader, adapter_factory=lambda _cfg: adapter
        )
        await svc.get_active_link()
        await svc.scan_networks()
        config_loader.assert_called_once()
