"""Netlink replies: collecting one request's reply from the datagrams the
socket receives, with hand-built kernel-layout datagrams."""

from __future__ import annotations

import errno
import struct

import pytest

from sp_rtk_base.services.network_link import netlink

NLM_F_MULTI = 0x2
RTM_NEWADDR = 20


def _message(kind: int, seq: int, body: bytes = b"", flags: int = 0) -> bytes:
    return struct.pack("IHHII", 16 + len(body), kind, flags, seq, 0) + body


def _error(seq: int, error: int) -> bytes:
    return _message(netlink.NLMSG_ERROR, seq, struct.pack("i", error) + b"\0" * 16)


def test_a_reply_is_every_datagram_of_the_request_up_to_its_end() -> None:
    first = _message(RTM_NEWADDR, 7, flags=NLM_F_MULTI)
    done = _message(netlink.NLMSG_DONE, 7, struct.pack("i", 0), flags=NLM_F_MULTI)
    unread = _message(RTM_NEWADDR, 8)

    assert netlink.reply(iter([first, done, unread]), seq=7) == [first, done]


def test_a_late_reply_to_an_earlier_request_is_skipped() -> None:
    late = _message(RTM_NEWADDR, 6)
    ours = _message(RTM_NEWADDR, 7)

    assert netlink.reply(iter([late, ours]), seq=7) == [ours]


def test_a_late_error_reply_to_an_earlier_request_is_skipped() -> None:
    late = _error(6, -errno.EBUSY)
    ours = _message(RTM_NEWADDR, 7)

    assert netlink.reply(iter([late, ours]), seq=7) == [ours]


def test_an_error_reply_to_the_request_raises() -> None:
    with pytest.raises(OSError, match="netlink") as raised:
        netlink.reply(iter([_error(7, -errno.EPERM)]), seq=7)
    assert raised.value.errno == errno.EPERM


def test_datagrams_that_run_out_before_the_end_of_the_reply_raise() -> None:
    first = _message(RTM_NEWADDR, 7, flags=NLM_F_MULTI)

    with pytest.raises(OSError, match="ended early"):
        netlink.reply(iter([first]), seq=7)
