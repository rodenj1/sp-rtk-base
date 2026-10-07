"""Live relay events through ``RelayService.stream_events()`` (issue #49).

These drive the real ``RelayEngine`` and its event bus. The input is a
TCP input on a local listener, so a start connects without hardware.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Iterator
from typing import Any

import pytest
import pytest_asyncio
from sp_rtk_base_relay.config import InputConfig

from sp_rtk_base.models.config_models import AppConfig
from sp_rtk_base.services.relay_events import STREAM_QUEUE_SIZE, EventStream
from sp_rtk_base.services.relay_service import RelayService


@pytest.fixture()
def tcp_input() -> Iterator[Any]:
    """Make TCP Relay inputs, each on its own local listener."""
    listeners: list[socket.socket] = []

    def _accept(srv: socket.socket) -> None:
        conns: list[socket.socket] = []
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conns.append(conn)

    def make() -> InputConfig:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        listeners.append(srv)
        threading.Thread(target=_accept, args=(srv,), daemon=True).start()
        port = srv.getsockname()[1]
        return InputConfig(source="tcp", config={"host": "127.0.0.1", "port": port})

    yield make
    for srv in listeners:
        srv.close()


@pytest_asyncio.fixture()
async def relay() -> Any:
    svc = RelayService(AppConfig)
    yield svc
    await svc.stop_relay(trigger="test")


async def received(stream: EventStream, quiet: float = 0.3) -> list[str]:
    """The event types the stream delivers until it has been quiet a while."""
    types: list[str] = []
    while True:
        try:
            event = await asyncio.wait_for(stream.get(), quiet)
        except asyncio.TimeoutError:
            return types
        types.append(event["event_type"])


pytestmark = pytest.mark.asyncio()


class TestEventStream:
    async def test_a_start_that_bypasses_the_api_reaches_an_open_stream(
        self, relay: RelayService, tcp_input: Any
    ) -> None:
        """The Dashboard's Start (trigger "ui") streams like the API's."""
        with relay.stream_events() as stream:
            await relay.start_relay(tcp_input(), [], trigger="ui")
            assert "engine.started" in await received(stream)

    async def test_an_open_stream_follows_a_replaced_engine(
        self, relay: RelayService, tcp_input: Any
    ) -> None:
        """Stop, change the input, Start: the new engine's events arrive."""
        with relay.stream_events() as stream:
            await relay.start_relay(tcp_input(), [], trigger="auto-start")
            first_engine = relay.engine
            await relay.stop_relay(trigger="ui")
            assert (await received(stream))[-1] == "engine.stopped"

            await relay.start_relay(tcp_input(), [], trigger="ui")
            assert relay.engine is not first_engine
            assert "engine.started" in await received(stream)

    async def test_every_open_stream_receives_every_event(
        self, relay: RelayService, tcp_input: Any
    ) -> None:
        """Two Dashboards open at once each see the whole log."""
        with relay.stream_events() as phone, relay.stream_events() as laptop:
            await relay.start_relay(tcp_input(), [], trigger="ui")
            on_phone = await received(phone)
            assert "engine.started" in on_phone
            assert await received(laptop) == on_phone

    async def test_a_stream_opened_later_gets_no_earlier_events(
        self, relay: RelayService, tcp_input: Any
    ) -> None:
        """History is the ring buffer's job, not the live stream's."""
        with relay.stream_events() as early:
            await relay.start_relay(tcp_input(), [], trigger="ui")
            await received(early)
            with relay.stream_events() as late:
                assert await received(late) == []
                assert relay.engine is not None
                relay.engine.event_bus.emit(
                    "test.probe", "after the late stream opened"
                )
                assert await received(late) == ["test.probe"]

    async def test_a_stream_that_never_reads_holds_up_no_one(
        self, relay: RelayService, tcp_input: Any
    ) -> None:
        """A stalled client keeps a bounded backlog; the others keep up."""
        with relay.stream_events() as stalled, relay.stream_events() as reader:
            await relay.start_relay(tcp_input(), [], trigger="ui")
            await received(reader)
            assert relay.engine is not None
            read = 0
            for batch in range(5):
                for n in range(STREAM_QUEUE_SIZE // 4):
                    relay.engine.event_bus.emit("test.probe", f"probe {batch}.{n}")
                read += len(await received(reader))
            assert read == 5 * (STREAM_QUEUE_SIZE // 4)
            backlog = await received(stalled)
            assert len(backlog) == STREAM_QUEUE_SIZE
            assert backlog[-1] == "test.probe"

    async def test_the_first_stream_opened_on_a_running_relay_streams(
        self, relay: RelayService, tcp_input: Any
    ) -> None:
        """The Dashboard opened after auto-start, with no other client."""
        await relay.start_relay(tcp_input(), [], trigger="auto-start")
        with relay.stream_events() as stream:
            assert relay.engine is not None
            relay.engine.event_bus.emit("test.probe", "after the first stream opened")
            assert await received(stream) == ["test.probe"]
        with relay.stream_events() as reopened:
            relay.engine.event_bus.emit("test.probe", "after the last stream closed")
            assert await received(reopened) == ["test.probe"]

    async def test_a_stream_opened_while_the_relay_is_starting_streams(
        self, relay: RelayService, tcp_input: Any
    ) -> None:
        """A Dashboard opened mid-start, with no other client, still streams."""
        starting = asyncio.create_task(relay.start_relay(tcp_input(), [], trigger="ui"))
        await asyncio.sleep(0)
        assert not relay.is_running, "the start finished too soon to test this"
        with relay.stream_events() as stream:
            await starting
            assert relay.engine is not None
            relay.engine.event_bus.emit("test.probe", "after the start finished")
            assert "test.probe" in await received(stream)

    async def test_a_stop_that_fails_does_not_strand_open_streams(
        self, relay: RelayService, tcp_input: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop raises; the next start, on a new input, still streams."""
        with relay.stream_events() as stream:
            await relay.start_relay(tcp_input(), [], trigger="ui")
            engine = relay.engine
            assert engine is not None
            real_stop = engine.stop

            def stop_then_fail() -> None:
                real_stop()
                raise RuntimeError("stop failed after stopping")

            monkeypatch.setattr(engine, "stop", stop_then_fail)
            with pytest.raises(RuntimeError):
                await relay.stop_relay(trigger="ui")
            await received(stream)

            await relay.start_relay(tcp_input(), [], trigger="ui")
            assert relay.engine is not engine
            assert "engine.started" in await received(stream)
