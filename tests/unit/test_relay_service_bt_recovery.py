"""The stale-handle release runs on *every* path into the relay.

It used to sit inside `init_services`, behind `if not settings.auto_start:
return` — so the four manual Start paths (dashboard, `POST /api/relay/start`,
the device handoff, and the Verification's "Save & Start now") never got
it.  That mattered once a Verification existed: with the release on the
auto-start path only, the Verification would have been *more forgiving
than the Start it predicts*, handing out a Green for a Start that then
failed on a handle BlueZ was still holding (issue #129, decision 4).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any
from unittest.mock import MagicMock

import pytest
from sp_rtk_base_relay.config import InputConfig

from sp_rtk_base.services.relay_service import RelayService

# ``services/__init__`` binds singletons named ``relay_service`` and
# ``config_service``, which shadow the same-named submodules: both
# ``import sp_rtk_base.services.relay_service as m`` and the dotted
# string form of ``monkeypatch.setattr`` hand back the *instance*.
# ``import_module`` reads ``sys.modules`` directly and is unaffected.
relay_mod = import_module("sp_rtk_base.services.relay_service")
bt_mod = import_module("sp_rtk_base.services.bluetooth_service")


@pytest.fixture()
def relay(monkeypatch: pytest.MonkeyPatch) -> RelayService:
    """A RelayService whose engine is a stub, so start_relay is cheap."""
    svc = RelayService()
    engine = MagicMock()
    engine.is_running = False

    def _start(destinations: Any = None) -> None:
        engine.is_running = True

    engine.start = _start
    monkeypatch.setattr(relay_mod, "RelayEngine", lambda cfg: engine)
    return svc


class TestStaleHandleReleaseOnEveryStart:
    @pytest.mark.asyncio
    async def test_a_bluetooth_start_releases_the_stale_handle(
        self, relay: RelayService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        released: list[str] = []

        async def _spy(mac: str, **kwargs: Any) -> None:
            released.append(mac)

        monkeypatch.setattr(bt_mod, "release_stale_bluetooth_handle", _spy)

        await relay.start_relay(
            InputConfig(
                source="bluetooth", config={"mac_address": "AA:BB:CC:DD:EE:FF"}
            ),
            trigger="api",
        )

        assert released == ["AA:BB:CC:DD:EE:FF"]

    @pytest.mark.asyncio
    async def test_a_serial_start_does_not(
        self, relay: RelayService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        released: list[str] = []

        async def _spy(mac: str, **kwargs: Any) -> None:
            released.append(mac)

        monkeypatch.setattr(bt_mod, "release_stale_bluetooth_handle", _spy)

        await relay.start_relay(
            InputConfig(
                source="serial",
                config={"port": "/dev/ttyACM0", "baudrate": 115200},
            ),
            trigger="api",
        )

        assert released == []

    @pytest.mark.asyncio
    async def test_the_legacy_address_key_is_still_honoured(
        self, relay: RelayService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        released: list[str] = []

        async def _spy(mac: str, **kwargs: Any) -> None:
            released.append(mac)

        monkeypatch.setattr(bt_mod, "release_stale_bluetooth_handle", _spy)

        await relay.start_relay(
            InputConfig(source="bluetooth", config={"address": "AA:BB:CC:DD:EE:FF"}),
            trigger="api",
        )

        assert released == ["AA:BB:CC:DD:EE:FF"]

    @pytest.mark.asyncio
    async def test_a_bluetooth_start_without_a_mac_is_skipped(
        self, relay: RelayService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        released: list[str] = []

        async def _spy(mac: str, **kwargs: Any) -> None:
            released.append(mac)

        monkeypatch.setattr(bt_mod, "release_stale_bluetooth_handle", _spy)

        await relay.start_relay(
            InputConfig(source="bluetooth", config={"device_name": "RTK_GPS_BASE"}),
            trigger="api",
        )

        assert released == []

    @pytest.mark.asyncio
    async def test_a_failing_release_does_not_block_the_start(
        self, relay: RelayService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Best-effort: a handle that may not even have been stuck must
        not be able to stop the base station coming up."""

        async def _boom(mac: str, **kwargs: Any) -> None:
            raise RuntimeError("BlueZ is wedged")

        monkeypatch.setattr(bt_mod, "release_stale_bluetooth_handle", _boom)

        await relay.start_relay(
            InputConfig(
                source="bluetooth", config={"mac_address": "AA:BB:CC:DD:EE:FF"}
            ),
            trigger="api",
        )

        assert relay.is_running
