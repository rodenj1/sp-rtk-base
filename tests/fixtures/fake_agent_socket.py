"""A fake Portal agent on a Unix socket, for the e2e suite (spec #82: "the
agent's Unix socket is faked there").

Speaks the control socket's wire format (one JSON object per line). A test
sets what it answers: ``fake.status`` for ``status``, ``fake.after_claim``
for ``start_claim`` (``None``: refuse with ``fake.claim_error``);
``unclaim`` goes back to not connected; ``pause``/``resume`` switch
``status["state"]`` between ``paused`` and ``connected`` (and drop
``sessions`` on pause). ``ops`` lists every op asked. Runs its own event
loop on a thread.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

NOT_CONNECTED: dict[str, Any] = {"state": "not_connected"}


class FakeAgentSocket:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.status: dict[str, Any] = dict(NOT_CONNECTED)
        self.after_claim: dict[str, Any] | None = None
        self.claim_error = "Couldn't reach the Portal (test)."
        self.ops: list[str] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._server: asyncio.Server | None = None
        self._open = 0
        self._hung_up = threading.Condition()

    def hung_up(self, timeout: float) -> bool:
        """Whether every client has closed its connection within ``timeout``.

        Blocks the calling thread, so a client's own event loop can't run
        meanwhile: only a connection the client really closed counts.
        """
        with self._hung_up:
            return self._hung_up.wait_for(lambda: self._open == 0, timeout)

    def reset(self) -> None:
        self.status = dict(NOT_CONNECTED)
        self.after_claim = None
        self.ops.clear()

    def _answer(self, line: bytes) -> dict[str, Any]:
        op = json.loads(line).get("op")
        self.ops.append(op)
        if op == "status":
            return {"ok": True, "status": self.status}
        if op == "start_claim":
            if self.after_claim is None:
                return {
                    "ok": False,
                    "error": "claim failed",
                    "message": self.claim_error,
                }
            self.status = self.after_claim
            return {"ok": True, "status": self.status}
        if op in ("pause", "resume"):
            if self.status.get("state") not in ("connected", "paused"):
                return {"ok": False, "error": "not claimed"}
            self.status = dict(self.status)
            self.status["state"] = "paused" if op == "pause" else "connected"
            if op == "pause":
                self.status["sessions"] = []
            return {"ok": True, "status": self.status}
        if op == "unclaim":
            self.status = {"state": "not_connected", "message": "Un-claimed."}
            return {"ok": True, "status": self.status}
        return {"ok": False, "error": "unknown op"}

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        with self._hung_up:
            self._open += 1
        try:
            while line := await reader.readline():
                writer.write((json.dumps(self._answer(line)) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()
            with self._hung_up:
                self._open -= 1
                self._hung_up.notify_all()

    def __enter__(self) -> FakeAgentSocket:
        self._thread.start()
        self._server = asyncio.run_coroutine_threadsafe(
            asyncio.start_unix_server(self._handle, str(self.path)), self._loop
        ).result(10)
        return self

    def __exit__(self, *exc: object) -> None:
        async def stop() -> None:
            assert self._server is not None
            self._server.close()
            # Connections still open (a client that hasn't hung up yet) go
            # first: from Python 3.12, wait_closed() waits for them, and a
            # client blocked on this very call never hangs up.
            handlers = [
                t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            ]
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            await self._server.wait_closed()

        asyncio.run_coroutine_threadsafe(stop(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
