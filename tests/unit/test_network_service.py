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
import threading
from unittest.mock import MagicMock

import pytest

from sp_rtk_base.models.net_provision_models import (
    BandChangeApplied,
    BandChangePending,
    BandChangeRefused,
    BandChangeReverted,
    BandChangeSaved,
    NetProvisionConfig,
    SavedWifiConnection,
    WifiBand,
    WifiNetwork,
)
from sp_rtk_base.services.net_provision import NmcliError
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


class TestIsConfigured:
    @pytest.mark.asyncio()
    async def test_configured_when_the_config_loads(self) -> None:
        assert await _service_with_adapter(_fake_adapter()).is_configured() is True

    @pytest.mark.asyncio()
    async def test_not_configured_when_the_config_is_missing(self) -> None:
        assert await _service_unconfigured().is_configured() is False

    @pytest.mark.asyncio()
    async def test_runs_no_nmcli(self) -> None:
        """It only loads the config: the adapter is built but never used."""
        adapter = _fake_adapter()
        await _service_with_adapter(adapter).is_configured()
        assert adapter.method_calls == []  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_loads_the_config_off_the_event_loop(self) -> None:
        """The Network page asks every 2 s: the YAML load must never block
        the event loop."""
        loop_thread = threading.get_ident()
        loaded_on: list[int] = []

        def _load() -> NetProvisionConfig:
            loaded_on.append(threading.get_ident())
            return _config()

        svc = NetworkService(
            config_loader=_load, adapter_factory=lambda _cfg: _fake_adapter()
        )
        assert await svc.is_configured() is True
        assert loaded_on
        assert loop_thread not in loaded_on


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


class TestHas5Ghz:
    @pytest.mark.asyncio()
    @pytest.mark.parametrize("answer", [True, False, None])
    async def test_returns_the_adapter_answer(self, answer: bool | None) -> None:
        adapter = _fake_adapter()
        adapter.has_5ghz.return_value = answer  # type: ignore[attr-defined]
        assert await _service_with_adapter(adapter).has_5ghz() is answer

    @pytest.mark.asyncio()
    async def test_a_definite_answer_is_read_once(self) -> None:
        """The hardware doesn't change while the app runs, and the Network
        page asks on every visit."""
        adapter = _fake_adapter()
        adapter.has_5ghz.return_value = True  # type: ignore[attr-defined]
        svc = _service_with_adapter(adapter)
        await svc.has_5ghz()
        assert await svc.has_5ghz() is True
        adapter.has_5ghz.assert_called_once_with()  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_unknown_is_asked_again(self) -> None:
        """Unknown may be a transient nmcli failure, so it isn't kept."""
        adapter = _fake_adapter()
        adapter.has_5ghz.side_effect = [None, True]  # type: ignore[attr-defined]
        svc = _service_with_adapter(adapter)
        assert await svc.has_5ghz() is None
        assert await svc.has_5ghz() is True

    @pytest.mark.asyncio()
    async def test_raises_not_configured_when_config_is_missing(self) -> None:
        with pytest.raises(NetworkNotConfiguredError):
            await _service_unconfigured().has_5ghz()


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
            "SiteWiFi", "hunter22", hidden=False, band=WifiBand.AUTO
        )

    @pytest.mark.asyncio()
    async def test_hidden_ssid_is_forwarded(self) -> None:
        adapter = _fake_adapter()
        svc = _service_with_adapter(adapter)
        await svc.connect_to_network("HiddenNet", "hunter22", hidden=True)
        assert svc.last_connect_task is not None
        await svc.last_connect_task
        adapter.connect_to_network.assert_called_once_with(  # type: ignore[attr-defined]
            "HiddenNet", "hunter22", hidden=True, band=WifiBand.AUTO
        )

    @pytest.mark.asyncio()
    async def test_the_wifi_band_is_forwarded(self) -> None:
        """issue #153: the join form's band reaches the adapter, so the
        new profile is locked from its very first join."""
        adapter = _fake_adapter()
        svc = _service_with_adapter(adapter)
        await svc.connect_to_network("SiteWiFi", "hunter22", band=WifiBand.GHZ_2_4)
        assert svc.last_connect_task is not None
        await svc.last_connect_task
        adapter.connect_to_network.assert_called_once_with(  # type: ignore[attr-defined]
            "SiteWiFi", "hunter22", hidden=False, band=WifiBand.GHZ_2_4
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
    async def test_each_saved_network_carries_its_band(self) -> None:
        adapter = _fake_adapter()
        adapter.list_saved_connections.return_value = [  # type: ignore[attr-defined]
            SavedWifiConnection(name="FarmShop", active=True),
            SavedWifiConnection(name="Barn-AP", active=False, band=WifiBand.GHZ_2_4),
        ]
        saved = await _service_with_adapter(adapter).list_saved_networks()
        assert [(c.name, c.band) for c in saved] == [
            ("FarmShop", WifiBand.AUTO),
            ("Barn-AP", WifiBand.GHZ_2_4),
        ]

    @pytest.mark.asyncio()
    async def test_raises_not_configured_when_config_is_missing(self) -> None:
        svc = _service_unconfigured()
        with pytest.raises(NetworkNotConfiguredError):
            await svc.list_saved_networks()


class TestChangeBand:
    @pytest.mark.asyncio()
    async def test_a_network_that_isnt_active_is_saved_before_returning(
        self,
    ) -> None:
        adapter = _fake_adapter()
        saved = BandChangeSaved(name="Barn-AP", band=WifiBand.GHZ_2_4)
        adapter.change_band.return_value = saved  # type: ignore[attr-defined]
        svc = _service_with_adapter(adapter)
        assert await svc.change_band("Barn-AP", WifiBand.GHZ_2_4) == saved
        adapter.change_band.assert_called_once_with(  # type: ignore[attr-defined]
            "Barn-AP", WifiBand.GHZ_2_4
        )

    @pytest.mark.asyncio()
    async def test_a_refusal_comes_back_before_the_link_is_touched(self) -> None:
        adapter = _fake_adapter()
        refused = BandChangeRefused(
            name="FarmShop", band=WifiBand.GHZ_5, reason="not heard"
        )
        adapter.change_band.return_value = refused  # type: ignore[attr-defined]
        svc = _service_with_adapter(adapter)
        assert await svc.change_band("FarmShop", WifiBand.GHZ_5) == refused
        assert svc.last_band_change_task is None
        adapter.apply_band_change.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio()
    async def test_the_active_network_is_reconnected_in_the_background(
        self,
    ) -> None:
        """Fire-and-acknowledge, like switch: the reconnect may drop the
        link carrying this very request."""
        adapter = _fake_adapter()
        pending = BandChangePending(name="FarmShop", band=WifiBand.GHZ_2_4)
        adapter.change_band.return_value = pending  # type: ignore[attr-defined]
        adapter.apply_band_change.return_value = BandChangeApplied(  # type: ignore[attr-defined]
            name="FarmShop", band=WifiBand.GHZ_2_4
        )
        svc = _service_with_adapter(adapter)
        assert await svc.change_band("FarmShop", WifiBand.GHZ_2_4) == pending
        adapter.apply_band_change.assert_not_called()  # type: ignore[attr-defined]
        assert svc.last_band_change_task is not None
        await svc.last_band_change_task
        adapter.apply_band_change.assert_called_once_with(  # type: ignore[attr-defined]
            "FarmShop", WifiBand.GHZ_2_4
        )

    @staticmethod
    async def _reverted_service() -> tuple[NetworkService, NmcliAdapter]:
        """A service whose change of FarmShop to 5 GHz was undone."""
        adapter = _fake_adapter()
        adapter.list_saved_connections.return_value = [  # type: ignore[attr-defined]
            SavedWifiConnection(name="FarmShop", active=True),
            SavedWifiConnection(name="Barn-AP", active=False),
        ]
        adapter.change_band.return_value = BandChangePending(  # type: ignore[attr-defined]
            name="FarmShop", band=WifiBand.GHZ_5
        )
        adapter.apply_band_change.return_value = BandChangeReverted(  # type: ignore[attr-defined]
            name="FarmShop", band=WifiBand.AUTO, tried=WifiBand.GHZ_5
        )
        svc = _service_with_adapter(adapter)
        await svc.change_band("FarmShop", WifiBand.GHZ_5)
        assert svc.last_band_change_task is not None
        await svc.last_band_change_task
        return svc, adapter

    @pytest.mark.asyncio()
    async def test_a_reverted_change_is_kept_with_its_saved_network(self) -> None:
        svc, _adapter = await self._reverted_service()
        saved = await svc.list_saved_networks()
        assert [(c.name, c.last_band_change) for c in saved] == [
            (
                "FarmShop",
                BandChangeReverted(
                    name="FarmShop", band=WifiBand.AUTO, tried=WifiBand.GHZ_5
                ),
            ),
            ("Barn-AP", None),
        ]

    @pytest.mark.asyncio()
    async def test_a_later_change_clears_the_reverted_outcome(self) -> None:
        svc, adapter = await self._reverted_service()
        adapter.change_band.return_value = BandChangePending(  # type: ignore[attr-defined]
            name="FarmShop", band=WifiBand.GHZ_2_4
        )
        adapter.apply_band_change.return_value = BandChangeApplied(  # type: ignore[attr-defined]
            name="FarmShop", band=WifiBand.GHZ_2_4
        )
        await svc.change_band("FarmShop", WifiBand.GHZ_2_4)
        saved = await svc.list_saved_networks()
        assert [c.last_band_change for c in saved] == [None, None]

    @pytest.mark.asyncio()
    async def test_a_refused_change_leaves_the_reverted_outcome(self) -> None:
        """Nothing changed, so the earlier undo still stands."""
        svc, adapter = await self._reverted_service()
        adapter.change_band.return_value = BandChangeRefused(  # type: ignore[attr-defined]
            name="FarmShop", band=WifiBand.GHZ_5, reason="not heard"
        )
        await svc.change_band("FarmShop", WifiBand.GHZ_5)
        saved = await svc.list_saved_networks()
        assert saved[0].last_band_change is not None

    @pytest.mark.asyncio()
    async def test_an_adapter_fault_reaches_the_caller(self) -> None:
        adapter = _fake_adapter()
        adapter.change_band.side_effect = NmcliError("modify failed")  # type: ignore[attr-defined]
        with pytest.raises(NmcliError):
            await _service_with_adapter(adapter).change_band("Barn-AP", WifiBand.GHZ_5)

    @pytest.mark.asyncio()
    async def test_raises_not_configured_when_config_is_missing(self) -> None:
        with pytest.raises(NetworkNotConfiguredError):
            await _service_unconfigured().change_band("Barn-AP", WifiBand.GHZ_5)


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
        adapter.scan_networks.return_value = []  # type: ignore[attr-defined]
        config_loader = MagicMock(return_value=_config())
        svc = NetworkService(
            config_loader=config_loader, adapter_factory=lambda _cfg: adapter
        )
        await svc.is_configured()
        await svc.scan_networks()
        config_loader.assert_called_once()
