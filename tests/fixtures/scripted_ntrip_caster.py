"""A scripted NTRIP caster for testing NTRIP clients (the Correction source Verification).

Listens on a real localhost socket and plays one Script per accepted
connection: send a reply once the client's request has arrived, then body
pieces, then hold the connection open or close it. Records each request.
(MockNtripCaster is the other role: a caster receiving a server's upload.)
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Script:
    """What the fake caster does with one connection."""

    # Sent as soon as the client connects, without reading a request (e.g. to
    # answer a TLS hello with plain NTRIP).
    greeting: bytes = b""
    delay: float = 0.0  # seconds to wait before the reply
    reply: bytes = b""  # sent once the request has arrived
    body: list[bytes] = field(default_factory=list[bytes])  # then sent, piece by piece
    # Afterwards, keep the connection open (until the client goes) or close it.
    hold: bool = True


class FakeCaster:
    """A localhost listener that plays one Script per accepted connection."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port: int = self.server.getsockname()[1]
        self.scripts: list[Script] = []
        self.requests: list[bytes] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            script = self.scripts.pop(0) if self.scripts else Script()
            threading.Thread(
                target=self._play, args=(conn, script), daemon=True
            ).start()

    def _play(self, conn: socket.socket, script: Script) -> None:
        with conn:
            try:
                if script.greeting:
                    conn.sendall(script.greeting)
                    conn.recv(4096)
                    return
                request = b""
                while b"\r\n\r\n" not in request:
                    piece = conn.recv(4096)
                    if not piece:
                        return
                    request += piece
                self.requests.append(request)
                time.sleep(script.delay)
                conn.sendall(script.reply)
                for piece in script.body:
                    time.sleep(0.02)
                    conn.sendall(piece)
                if script.hold:
                    while conn.recv(4096):
                        pass
            except OSError:
                pass

    def close(self) -> None:
        self.server.close()
