"""A fake DNS resolver on 127.0.0.1, for the Portal agent's resolver check.

``FakeDns(reply=...)`` answers every query on its own thread: ``"ok"`` with
a NOERROR reply to the same id, ``"refused"`` with REFUSED, ``"wrong-id"``
with a reply to another id, ``"silent"`` never. ``.server`` is the
``127.0.0.1:<port>`` the resolver check takes; ``.queries`` counts what
arrived.
"""

from __future__ import annotations

import socket
import struct
import threading
from types import TracebackType


class FakeDns:
    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.queries = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.05)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def server(self) -> str:
        return f"127.0.0.1:{self._sock.getsockname()[1]}"

    def __enter__(self) -> FakeDns:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, peer = self._sock.recvfrom(512)
            except TimeoutError:
                continue
            self.queries += 1
            if self.reply == "silent" or len(data) < 12:
                continue
            (qid,) = struct.unpack("!H", data[:2])
            if self.reply == "wrong-id":
                qid ^= 0xFFFF
            rcode = 5 if self.reply == "refused" else 0
            flags = 0x8180 | rcode  # QR, RD, RA
            header = struct.pack("!HHHHHH", qid, flags, 1, 0, 0, 0)
            self._sock.sendto(header + data[12:], peer)


def closed_port() -> str:
    """A ``127.0.0.1:<port>`` nothing listens on (queries are refused)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{s.getsockname()[1]}"
