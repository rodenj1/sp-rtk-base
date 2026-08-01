"""Network service — console bridge onto net-provisioning state (issues #22-24).

Bridges the synchronous, subprocess-backed :class:`NmcliAdapter` (issue
#21) to the async API/UI layers, the same way :mod:`device_service`
bridges the synchronous GPS driver. Read paths (issue #22) return their
result directly; the WiFi-join (issue #23), switch, and forget (issue
#24) paths are all fire-and-acknowledge — see
:meth:`NetworkService.connect_to_network`,
:meth:`NetworkService.switch_to_network`, and
:meth:`NetworkService.forget_network`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import NamedTuple

from sp_rtk_base.models.net_provision_models import (
    ActiveLink,
    NetProvisionConfig,
    SavedWifiConnection,
    WifiNetwork,
)
from sp_rtk_base.services.net_provision.config_loader import (
    NetProvisionConfigError,
    load_net_provision_config,
)
from sp_rtk_base.services.net_provision.nmcli_adapter import NmcliAdapter

logger = logging.getLogger(__name__)


class ApFallbackInfo(NamedTuple):
    """AP SSID + fallback window, for the console's pre-apply warning (issue #23)."""

    ap_ssid: str
    fallback_window_seconds: float


ConfigLoader = Callable[[], NetProvisionConfig]
AdapterFactory = Callable[[NetProvisionConfig], NmcliAdapter]


class NetworkNotConfiguredError(RuntimeError):
    """No net-provisioning config exists on this device.

    Raised instead of letting :class:`NetProvisionConfigError` leak
    past this service — callers (API/UI) only need to know "there's no
    network data to show", not the file-path/YAML details of why.
    """


class NetworkService:
    """Console-facing wrapper around :class:`NmcliAdapter`.

    Config is loaded lazily on first use rather than at construction:
    this service is instantiated as a module-level singleton at import
    time (see ``services/__init__.py``), before the net-provisioning
    config file is guaranteed to exist on a freshly-installed device.
    A missing config is a normal, recoverable state here — not a
    startup failure — so every public method raises
    :class:`NetworkNotConfiguredError` per call instead.
    """

    def __init__(
        self,
        config_loader: ConfigLoader = load_net_provision_config,
        adapter_factory: AdapterFactory = NmcliAdapter,
    ) -> None:
        self._config_loader = config_loader
        self._adapter_factory = adapter_factory
        self._adapter: NmcliAdapter | None = None
        self._config: NetProvisionConfig | None = None
        # Exposed only so tests can synchronize on a background connect/
        # switch/forget attempt — production callers never need to await
        # these, that's the whole point of fire-and-acknowledge.
        self.last_connect_task: asyncio.Task[None] | None = None
        self.last_switch_task: asyncio.Task[None] | None = None
        self.last_forget_task: asyncio.Task[None] | None = None

    def _get_adapter(self) -> NmcliAdapter:
        if self._adapter is None:
            try:
                config = self._config_loader()
            except NetProvisionConfigError as exc:
                raise NetworkNotConfiguredError(str(exc)) from exc
            self._config = config
            self._adapter = self._adapter_factory(config)
        return self._adapter

    async def get_active_link(self) -> ActiveLink | None:
        """The device's current wired/WiFi link, or None if neither is up.

        Raises:
            NetworkNotConfiguredError: No net-provisioning config exists.
        """
        adapter = self._get_adapter()
        return await asyncio.to_thread(adapter.read_active_link)

    async def scan_networks(self) -> list[WifiNetwork]:
        """Nearby WiFi networks, strongest signal first.

        Raises:
            NetworkNotConfiguredError: No net-provisioning config exists.
        """
        adapter = self._get_adapter()
        return await asyncio.to_thread(adapter.scan_networks)

    async def get_ap_fallback_info(self) -> ApFallbackInfo:
        """AP SSID + fallback window, for the console's pre-apply warning.

        Raises:
            NetworkNotConfiguredError: No net-provisioning config exists.
        """
        self._get_adapter()
        assert self._config is not None  # set by _get_adapter, just above
        return ApFallbackInfo(
            ap_ssid=self._config.ap_ssid,
            fallback_window_seconds=self._config.fallback_window_seconds,
        )

    async def connect_to_network(
        self, ssid: str, password: str, *, hidden: bool = False
    ) -> None:
        """Join a WiFi network — fire-and-acknowledge (issue #23).

        Returns as soon as the attempt is scheduled, without waiting
        for nmcli to report success or failure: the request calling
        this may itself be riding the very WiFi link that's about to
        be replaced, so awaiting the nmcli subprocess here would just
        hold the response open until a connection that's disappearing
        underneath it times out. The console warns the operator about
        this before ever calling in.

        Args:
            hidden: The SSID is not broadcast, so nmcli must attempt
                the association blind rather than matching a scan
                result.

        Raises:
            NetworkNotConfiguredError: No net-provisioning config
                exists. Raised synchronously — this is a setup
                problem, not a connect-attempt outcome, so it's fair
                to surface it right away rather than in the
                background.
        """
        adapter = self._get_adapter()
        self.last_connect_task = self._run_in_background(
            asyncio.to_thread(
                adapter.connect_to_network, ssid, password, hidden=hidden
            ),
            name="sp_rtk_base.network_connect",
            action="WiFi connect",
        )

    async def list_saved_networks(self) -> list[SavedWifiConnection]:
        """Saved WiFi profiles, for the console's switch/forget list (issue #24).

        Raises:
            NetworkNotConfiguredError: No net-provisioning config exists.
        """
        adapter = self._get_adapter()
        return await asyncio.to_thread(adapter.list_saved_connections)

    async def switch_to_network(self, name: str) -> None:
        """Activate an already-saved WiFi profile — fire-and-acknowledge (issue #24).

        Shares :meth:`connect_to_network`'s disconnect-your-own-request
        problem: the profile being activated may replace the very link
        carrying this request, so this schedules the nmcli call in the
        background and returns immediately rather than waiting for it
        to succeed or fail.

        Raises:
            NetworkNotConfiguredError: No net-provisioning config
                exists. Raised synchronously, same rationale as
                connect_to_network.
        """
        adapter = self._get_adapter()
        self.last_switch_task = self._run_in_background(
            asyncio.to_thread(adapter.activate_connection, name),
            name="sp_rtk_base.network_switch",
            action="WiFi switch",
        )

    async def forget_network(self, name: str) -> None:
        """Delete a saved WiFi profile — fire-and-acknowledge (issue #24).

        Forgetting the currently active network deactivates it as part
        of the delete, which can drop this very request's own
        connection exactly like :meth:`switch_to_network` — so this
        uses the same background-and-log pattern rather than assuming
        a forget is always safe to await synchronously.

        Raises:
            NetworkNotConfiguredError: No net-provisioning config
                exists. Raised synchronously, same rationale as
                connect_to_network.
        """
        adapter = self._get_adapter()
        self.last_forget_task = self._run_in_background(
            asyncio.to_thread(adapter.forget_connection, name),
            name="sp_rtk_base.network_forget",
            action="WiFi forget",
        )

    @staticmethod
    def _run_in_background(
        coro: Coroutine[None, None, None], *, name: str, action: str
    ) -> asyncio.Task[None]:
        """Schedule ``coro`` and log rather than propagate its failure.

        Shared by connect/switch/forget: each may drop the very
        connection carrying its own request, so there's no caller
        left by the time nmcli reports an outcome — a raised
        exception here would have nowhere useful to go.
        """
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(
            lambda t: NetworkService._log_background_failure(t, action)
        )
        return task

    @staticmethod
    def _log_background_failure(task: asyncio.Task[None], action: str) -> None:
        """Surface a background failure since there's no request left to
        return it to by the time nmcli reports one."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Background %s attempt failed: %s", action, exc)
