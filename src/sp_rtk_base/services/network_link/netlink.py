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


def _parse(datagram: bytes) -> Iterator[Message]:
    """The netlink messages in one received datagram, error replies too."""
    offset = 0
    while offset + _HEADER.size <= len(datagram):
        length, kind, flags, seq, _pid = _HEADER.unpack_from(datagram, offset)
        if length < _HEADER.size:
            break
        payload = datagram[offset + _HEADER.size : offset + length]
        yield Message(type=kind, flags=flags, seq=seq, payload=payload)
        offset += _align(length)


def _raise_if_error(message: Message) -> None:
    if message.type == NLMSG_ERROR:
        (error,) = struct.unpack_from("i", message.payload)
        if error < 0:
            raise OSError(-error, f"netlink error: {errno.errorcode.get(-error)}")


def messages(datagram: bytes) -> Iterator[Message]:
    """The netlink messages in one received datagram.

    Raises:
        OSError: for an ``NLMSG_ERROR`` carrying a negative errno (an
            error reply; error 0 is an ACK, which is yielded).
    """
    for message in _parse(datagram):
        _raise_if_error(message)
        yield message


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


def _ends_reply(message: Message) -> bool:
    """Whether *message* ends a reply: ``NLMSG_DONE``, an error or ACK, or
    a message that isn't part of a multipart dump."""
    return message.type in (NLMSG_DONE, NLMSG_ERROR) or not message.flags & NLM_F_MULTI


def reply(datagrams: Iterator[bytes], seq: int) -> list[bytes]:
    """Request *seq*'s reply: every datagram of it from *datagrams*, up to
    its end.  Datagrams for any other request (a late reply to an earlier,
    timed-out one, error replies included) are skipped.

    Raises:
        OSError: the reply is a netlink error, or *datagrams* ran out
            before its end.
    """
    replies: list[bytes] = []
    for datagram in datagrams:
        ours = [message for message in _parse(datagram) if message.seq == seq]
        if not ours:
            continue
        for message in ours:
            _raise_if_error(message)
        replies.append(datagram)
        if any(_ends_reply(message) for message in ours):
            return replies
    raise OSError(errno.EIO, "netlink reply ended early")


class NetlinkSocket:  # pragma: no cover - host I/O; parsing is tested
    """One long-lived netlink socket, opened on first use and reopened after
    any failure, until :meth:`close`.  Blocking, with a timeout: call it off
    the event loop, and never :meth:`close` it during an :meth:`exchange`."""

    TIMEOUT_SECONDS = 1.0
    RECEIVE_BYTES = 65536

    def __init__(self, protocol: int) -> None:
        self._protocol = protocol
        self._socket: socket.socket | None = None
        self._seq = itertools.count(1)
        self._closed = False

    def exchange(self, kind: int, flags: int, body: bytes) -> list[bytes]:
        """Send one request; return every reply datagram up to its end.

        Raises:
            OSError: the request failed, or the socket is closed.
        """
        if self._closed:
            raise OSError(errno.EBADF, "netlink socket closed")
        try:
            return self._exchange(kind, flags, body)
        except Exception:
            self._drop()
            raise

    def _exchange(self, kind: int, flags: int, body: bytes) -> list[bytes]:
        if self._socket is None:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, self._protocol)
            sock.settimeout(self.TIMEOUT_SECONDS)
            sock.bind((0, 0))
            self._socket = sock
        sock = self._socket
        seq = next(self._seq) & 0xFFFFFFFF
        sock.send(request(kind, flags, seq, body))
        return reply(iter(lambda: sock.recv(self.RECEIVE_BYTES), b""), seq)

    def _drop(self) -> None:
        """Close the socket; the next exchange opens a new one."""
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def close(self) -> None:
        """Close the socket for good: it never reopens."""
        self._closed = True
        self._drop()
