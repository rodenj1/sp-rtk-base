"""Wildcard DNS responder for captive-portal auto-detection (issue #10).

Issue #6 chose DNS hijack over an iptables port-80 redirect: answering
every hostname lookup with the setup AP's own address is what makes
iOS/Android/Windows pop their "Sign in to network" prompt automatically
when an installer's phone joins the hotspot. NetworkManager's own
``shared``-mode dnsmasq does *not* do this by default — it's a normal
caching resolver — so this module supplies it.

:func:`build_wildcard_response` is the pure seam: raw query bytes in,
raw response bytes out, no socket. It only implements the sliver of
RFC 1035 wire format captive-portal probes exercise (a single question,
answered with one A record) — not a general-purpose DNS server.
"""

from __future__ import annotations

import socketserver
import struct
from typing import cast

_HEADER_LEN = 12
_RESPONSE_FLAGS = 0x8180  # QR=1 (response), RA=1 (recursion available), RCODE=0
_A_RECORD_TYPE = 1
_IN_CLASS = 1
_ANSWER_TTL_SECONDS = 60
_QNAME_POINTER = 0xC00C  # compression pointer at byte 12, the question's QNAME


def build_wildcard_response(query: bytes, answer_ip: str) -> bytes | None:
    """Answer ``query`` with a single A record pointing at ``answer_ip``.

    Args:
        query: Raw bytes of an incoming DNS query (any qtype — an
            AAAA probe still gets an A answer, since the goal is
            triggering captive-portal detection, not RFC compliance).
        answer_ip: Dotted-quad address to answer with — the setup AP's
            own gateway IP.

    Returns:
        Raw response bytes, or ``None`` if ``query`` is too short or
        malformed to safely parse — callers should silently drop it
        rather than crash the listener thread on a garbage packet.
    """
    if len(query) < _HEADER_LEN:
        return None
    transaction_id = query[:2]
    qdcount = struct.unpack("!H", query[4:6])[0]
    if qdcount < 1:
        return None

    question_end = _find_question_end(query)
    if question_end is None:
        return None
    question = query[_HEADER_LEN:question_end]

    header = (
        transaction_id
        + struct.pack("!H", _RESPONSE_FLAGS)
        + struct.pack("!HHHH", 1, 1, 0, 0)  # QDCOUNT, ANCOUNT, NSCOUNT, ARCOUNT
    )
    answer = (
        struct.pack("!H", _QNAME_POINTER)
        + struct.pack("!HH", _A_RECORD_TYPE, _IN_CLASS)
        + struct.pack("!I", _ANSWER_TTL_SECONDS)
        + struct.pack("!H", 4)
        + _encode_ipv4(answer_ip)
    )
    return header + question + answer


def _find_question_end(query: bytes) -> int | None:
    """Index just past the question section (QNAME + QTYPE + QCLASS).

    Only the length is needed — the question is echoed back verbatim,
    never inspected — but walking the QNAME's length-prefixed labels is
    the only way to find where it ends.
    """
    offset = _HEADER_LEN
    while True:
        if offset >= len(query):
            return None
        length = query[offset]
        offset += 1
        if length == 0:
            break
        offset += length
        if offset > len(query):
            return None
    offset += 4  # QTYPE + QCLASS
    if offset > len(query):
        return None
    return offset


def _encode_ipv4(dotted_quad: str) -> bytes:
    return bytes(int(octet) for octet in dotted_quad.split("."))


class _WildcardDnsHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        server = cast("WildcardDnsServer", self.server)
        response = build_wildcard_response(data, server.answer_ip)
        if response is not None:
            sock.sendto(response, self.client_address)


class WildcardDnsServer(socketserver.ThreadingUDPServer):
    """UDP DNS server answering every query with ``answer_ip``.

    Binds a privileged port (53) in production, which requires
    ``CAP_NET_BIND_SERVICE`` on the non-root ``sp-rtk-base`` service
    user — granted on the net-provision systemd unit. Runs only while
    the setup AP is active; lifecycle is owned by
    :class:`~sp_rtk_base.services.net_provision.portal.Portal`.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *, bind_host: str, port: int, answer_ip: str) -> None:
        self.answer_ip = answer_ip
        super().__init__((bind_host, port), _WildcardDnsHandler)
