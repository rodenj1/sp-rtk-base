"""Netlink, standard library only: pure message parsing and a thin socket.

The parsing (:func:`messages`, :func:`attributes`) is pure and tested
against kernel-layout bytes.  :class:`NetlinkSocket` is the untested
host shell: one long-lived socket per protocol, a request out, every
reply datagram back.  Route netlink (addresses) uses it now; generic
netlink (nl80211) can use the same pieces.

Everything is native byte order.
"""

from __future__ import annotations

import errno
import itertools
import socket
import struct
from collections.abc import Iterator
from dataclasses import dataclass

NETLINK_ROUTE = 0
NETLINK_GENERIC = 16

NLM_F_REQUEST = 0x1
NLM_F_MULTI = 0x2
NLM_F_ACK = 0x4
NLM_F_DUMP = 0x300

NLMSG_ERROR = 2
NLMSG_DONE = 3

_HEADER = struct.Struct("IHHII")  # nlmsghdr: len, type, flags, seq, pid
_ATTR = struct.Struct("HH")  # nlattr: len, type
_NLA_TYPE_MASK = 0x3FFF  # strips NLA_F_NESTED and NLA_F_NET_BYTEORDER


@dataclass(frozen=True)
class Message:
    """One netlink message: its header fields and the payload after them."""

    type: int
    flags: int
    seq: int
    payload: bytes


def _align(length: int) -> int:
    return (length + 3) & ~3


def messages(datagram: bytes) -> Iterator[Message]:
    """The netlink messages in one received datagram.

    Raises:
        OSError: for an ``NLMSG_ERROR`` carrying a negative errno (an
            error reply; error 0 is an ACK, which is yielded).
    """
    offset = 0
    while offset + _HEADER.size <= len(datagram):
        length, kind, flags, seq, _pid = _HEADER.unpack_from(datagram, offset)
        if length < _HEADER.size:
            break
        payload = datagram[offset + _HEADER.size : offset + length]
        if kind == NLMSG_ERROR:
            (error,) = struct.unpack_from("i", payload)
            if error < 0:
                raise OSError(-error, f"netlink error: {errno.errorcode.get(-error)}")
        yield Message(type=kind, flags=flags, seq=seq, payload=payload)
        offset += _align(length)


def attributes(buffer: bytes) -> dict[int, bytes]:
    """The netlink attributes in *buffer*, by type (the last wins)."""
    found: dict[int, bytes] = {}
    offset = 0
    while offset + _ATTR.size <= len(buffer):
        length, kind = _ATTR.unpack_from(buffer, offset)
        if length < _ATTR.size:
            break
        found[kind & _NLA_TYPE_MASK] = buffer[offset + _ATTR.size : offset + length]
        offset += _align(length)
    return found


def request(kind: int, flags: int, seq: int, body: bytes) -> bytes:
    """A netlink request message."""
    return _HEADER.pack(_HEADER.size + len(body), kind, flags, seq, 0) + body


def is_last(datagram: bytes) -> bool:
    """Whether *datagram* ends a reply: it holds ``NLMSG_DONE``, an error or
    ACK, or a message that isn't part of a multipart dump."""
    return any(
        message.type in (NLMSG_DONE, NLMSG_ERROR) or not message.flags & NLM_F_MULTI
        for message in messages(datagram)
    )


class NetlinkSocket:  # pragma: no cover - host I/O; parsing is tested
    """One long-lived netlink socket, opened on first use and reopened after
    any failure.  Blocking, with a timeout: call it off the event loop."""

    TIMEOUT_SECONDS = 1.0
    RECEIVE_BYTES = 65536

    def __init__(self, protocol: int) -> None:
        self._protocol = protocol
        self._socket: socket.socket | None = None
        self._seq = itertools.count(1)

    def exchange(self, kind: int, flags: int, body: bytes) -> list[bytes]:
        """Send one request; return every reply datagram up to its end."""
        try:
            return self._exchange(kind, flags, body)
        except Exception:
            self.close()
            raise

    def _exchange(self, kind: int, flags: int, body: bytes) -> list[bytes]:
        if self._socket is None:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, self._protocol)
            sock.settimeout(self.TIMEOUT_SECONDS)
            sock.bind((0, 0))
            self._socket = sock
        seq = next(self._seq) & 0xFFFFFFFF
        self._socket.send(request(kind, flags, seq, body))
        replies: list[bytes] = []
        while True:
            datagram = self._socket.recv(self.RECEIVE_BYTES)
            if not any(message.seq == seq for message in messages(datagram)):
                continue  # a late reply to an earlier, timed-out request
            replies.append(datagram)
            if is_last(datagram):
                return replies

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()
