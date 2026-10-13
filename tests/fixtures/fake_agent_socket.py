"""A fake Portal agent on a Unix socket, for the e2e suite (spec #82: "the
agent's Unix socket is faked there").

Speaks the control socket's wire format (one JSON object per line). A test
sets what it answers: ``fake.status`` for ``status``, ``fake.after_claim``
for ``start_claim`` (``None``: refuse with ``fake.claim_error``);
``unclaim`` goes back to not connected. ``ops``
lists every op asked. Runs its own event loop on a thread.
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
        if op == "unclaim":
            self.status = {"state": "not_connected", "message": "Un-claimed."}
            return {"ok": True, "status": self.status}
        return {"ok": False, "error": "unknown op"}

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while line := await reader.readline():
                writer.write((json.dumps(self._answer(line)) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()

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
            await self._server.wait_closed()

        asyncio.run_coroutine_threadsafe(stop(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
