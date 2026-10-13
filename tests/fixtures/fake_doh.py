"""A fake DNS-over-HTTPS server (RFC 8484, GET) on ``https://127.0.0.1``.

``FakeDoh(ca)`` serves ``/dns-query`` over TLS with a certificate for the IP
127.0.0.1 from the test authority, as 1.1.1.1 serves one for its IP.
``.records`` maps a name to its A records ``(address, ttl)``, ``.cnames`` a
name to the name it is an alias of (answered with the CNAME first, then the
target's A records, as resolvers do). Unknown names get NXDOMAIN.

``.reply`` is ``"ok"``, ``"error"`` (HTTP 500), ``"silent"`` (reads the
request, never answers) or ``"garbage"`` (200 with bytes that aren't a DNS
message). ``.queries`` lists ``(name, qtype)`` of every query that arrived.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import struct
from collections.abc import AsyncGenerator
from urllib.parse import parse_qs, urlsplit

from tests.fixtures.tls import TestCa

A, CNAME, IN = 1, 5, 1


def _encode_name(name: str) -> bytes:
    labels = [p.encode() for p in name.rstrip(".").split(".") if p]
    return b"".join(bytes([len(p)]) + p for p in labels) + b"\x00"


def _read_question(message: bytes) -> tuple[str, int, int]:
    """``(name, qtype, end offset)`` of the message's one question."""
    labels: list[str] = []
    i = 12
    while message[i]:
        n = message[i]
        labels.append(message[i + 1 : i + 1 + n].decode())
        i += 1 + n
    qtype = struct.unpack("!H", message[i + 1 : i + 3])[0]
    return ".".join(labels), qtype, i + 5


class FakeDoh:
    def __init__(self, ca: TestCa) -> None:
        self._ca = ca
        self.records: dict[str, list[tuple[str, int]]] = {}
        self.cnames: dict[str, str] = {}
        self.reply = "ok"
        self.queries: list[tuple[str, int]] = []
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"https://127.0.0.1:{port}/dns-query"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle,
            "127.0.0.1",
            0,
            ssl=self._ca.server_context("127.0.0.1"),
        )

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        for task in list(self._handlers):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), 1)

    def answer(self, query: bytes) -> bytes:
        name, qtype, end = _read_question(query)
        self.queries.append((name, qtype))
        qid = query[:2]
        answers: list[bytes] = []
        target = name
        if target in self.cnames:
            target = self.cnames[name]
            rdata = _encode_name(target)
            answers.append(
                b"\xc0\x0c" + struct.pack("!HHIH", CNAME, IN, 300, len(rdata)) + rdata
            )
        records = self.records.get(target)
        if records is None:
            rcode = 3  # NXDOMAIN
            answers = []
        else:
            rcode = 0
            if qtype == A:
                for address, ttl in records:
                    answers.append(
                        _encode_name(target)
                        + struct.pack("!HHIH", A, IN, ttl, 4)
                        + ipaddress.IPv4Address(address).packed
                    )
        header = qid + struct.pack("!HHHHH", 0x8180 | rcode, 1, len(answers), 0, 0)
        return header + query[12:end] + b"".join(answers)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                target = head.split(b"\r\n", 1)[0].split(b" ")[1].decode()
                if self.reply == "silent":
                    await asyncio.sleep(3600)
                if self.reply == "error":
                    status, body = b"500 Internal Server Error", b""
                elif self.reply == "garbage":
                    status, body = b"200 OK", b"not dns"
                else:
                    dns = parse_qs(urlsplit(target).query)["dns"][0]
                    query = base64.urlsafe_b64decode(dns + "=" * (-len(dns) % 4))
                    status, body = b"200 OK", self.answer(query)
                writer.write(
                    b"HTTP/1.1 "
                    + status
                    + b"\r\nContent-Type: application/dns-message\r\n"
                    + b"Content-Length: %d\r\n\r\n" % len(body)
                    + body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._handlers.discard(task)
            writer.close()


@contextlib.asynccontextmanager
async def fake_doh(ca: TestCa) -> AsyncGenerator[FakeDoh]:
    server = FakeDoh(ca)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
