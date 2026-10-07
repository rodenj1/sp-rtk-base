"""Starting the Relay from the saved configuration (issue #50).

``RelayService.start_saved()`` is the one way every start path (API,
auto-start, Dashboard, Input page, Hand off) starts the Relay from the
saved Input profile and outputs.  These run the real ``RelayEngine``:
a TCP input on a local listener and a TCP-server output on a free port.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from sp_rtk_base.models.config_models import DestinationProfile, InputProfile
from sp_rtk_base.services.config_service import ConfigService
from sp_rtk_base.services.relay_service import RelayService, RelayStartRefusedError

pytestmark = pytest.mark.asyncio()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def tcp_input() -> Iterator[InputProfile]:
    """An Input profile for a TCP source that accepts connections."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    conns: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conns.append(conn)

    threading.Thread(target=accept, daemon=True).start()
    yield InputProfile(
        source="tcp", config={"host": "127.0.0.1", "port": srv.getsockname()[1]}
    )
    srv.close()


def _tcp_output(name: str = "local", *, enabled: bool = True) -> DestinationProfile:
    return DestinationProfile(
        name=name,
        type="tcp_server",
        enabled=enabled,
        config={"host": "127.0.0.1", "port": _free_port()},
    )


@pytest.fixture()
def config(tmp_path: Path) -> ConfigService:
    svc = ConfigService(config_path=tmp_path / "config.yaml")
    svc.load_config()
    return svc


@pytest_asyncio.fixture()
async def relay(config: ConfigService) -> AsyncIterator[RelayService]:
    svc = RelayService(config.get_config)
    yield svc
    await svc.stop_relay(trigger="test")


class TestStartSaved:
    async def test_starts_from_the_saved_input_and_enabled_outputs(
        self, relay: RelayService, config: ConfigService, tcp_input: InputProfile
    ) -> None:
        config.save_input_config(tcp_input)
        config.save_destination(_tcp_output("on"))
        config.save_destination(_tcp_output("off", enabled=False))

        await relay.start_saved(trigger="ui")

        assert relay.is_running
        assert relay.get_destination_names() == ["on"]

    async def _refused(self, relay: RelayService) -> str:
        with pytest.raises(RelayStartRefusedError) as refused:
            await relay.start_saved(trigger="ui")
        assert not relay.is_running
        return refused.value.code

    async def test_refuses_without_an_input(
        self, relay: RelayService, config: ConfigService
    ) -> None:
        config.save_destination(_tcp_output())
        assert await self._refused(relay) == "no_input"

    async def test_refuses_without_an_enabled_output(
        self, relay: RelayService, config: ConfigService, tcp_input: InputProfile
    ) -> None:
        config.save_input_config(tcp_input)
        config.save_destination(_tcp_output(enabled=False))
        assert await self._refused(relay) == "no_destinations"

    async def test_refuses_a_saved_output_that_cannot_run(
        self, relay: RelayService, config: ConfigService, tcp_input: InputProfile
    ) -> None:
        """An NTRIP v2 output without a username (issue #198)."""
        config.save_input_config(tcp_input)
        config.save_destination(
            DestinationProfile(
                name="rtk2go",
                type="ntrip",
                config={
                    "caster": "rtk2go.com",
                    "mountpoint": "MP1",
                    "password": "secret",
                    "version": "2.0",
                },
            )
        )
        with pytest.raises(RelayStartRefusedError) as refused:
            await relay.start_saved(trigger="ui")
        assert refused.value.code == "config_invalid"
        assert "username" in refused.value.message
        assert not relay.is_running

    async def test_refuses_while_already_running(
        self, relay: RelayService, config: ConfigService, tcp_input: InputProfile
    ) -> None:
        config.save_input_config(tcp_input)
        config.save_destination(_tcp_output())
        await relay.start_saved(trigger="ui")
        with pytest.raises(RelayStartRefusedError) as refused:
            await relay.start_saved(trigger="ui")
        assert refused.value.code == "already_running"

    async def test_refuses_while_the_console_is_connected_unless_told_not_to(
        self, relay: RelayService, config: ConfigService, tcp_input: InputProfile
    ) -> None:
        """Auto-start at boot is the one start that doesn't refuse."""
        config.save_input_config(tcp_input)
        config.save_destination(_tcp_output())
        relay.set_console_check(lambda: True)
        assert await self._refused(relay) == "console_connected"

        await relay.start_saved(
            trigger="auto-start", refuse_while_console_connected=False
        )
        assert relay.is_running


class TestCheckSaved:
    async def test_checks_a_given_input_profile_with_the_saved_outputs(
        self, relay: RelayService, config: ConfigService, tcp_input: InputProfile
    ) -> None:
        """Hand off checks the profile it is about to save, before saving it."""
        config.save_destination(_tcp_output("on"))
        serial = InputProfile(
            source="serial", config={"port": "/dev/ttyACM0", "baudrate": 38400}
        )
        saved = relay.check_saved(serial)
        assert saved.input.source == "serial"
        assert [d.name for d in saved.destinations] == ["on"]
        assert config.get_input_config() is None
        assert not relay.is_running
