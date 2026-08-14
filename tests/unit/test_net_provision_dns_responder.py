"""Tests for the wildcard DNS responder (issue #10).

``build_wildcard_response`` is the pure seam: given raw query bytes and
an answer IP, it returns raw response bytes — no socket, no I/O. Tests
hand-craft DNS queries with a small local encoder (mirroring how RFC
1035's wire format looks) and inspect the response with ``struct``,
never a real DNS library. :class:`WildcardDnsServer` is a thin
``socketserver`` wrapper around that function; it gets one smoke test
over a real (ephemeral-port) UDP socket, since there's no mockable
boundary inside it worth faking.
"""

from __future__ import annotations

import socket
import struct

from sp_rtk_base.services.net_provision.dns_responder import (
    WildcardDnsServer,
    build_wildcard_response,
)

_HEADER_LEN = 12


def _encode_query(
    qname: str, *, qtype: int = 1, qclass: int = 1, query_id: int = 0x1234
) -> bytes:
    """Build a minimal RFC 1035 query for ``qname``."""
    header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    labels = b"".join(
        struct.pack("B", len(label)) + label.encode("ascii")
        for label in qname.split(".")
    )
    question = labels + b"\x00" + struct.pack("!HH", qtype, qclass)
    return header + question


class TestBuildWildcardResponse:
    def test_echoes_the_transaction_id(self) -> None:
        query = _encode_query("captive.apple.com", query_id=0xBEEF)
        response = build_wildcard_response(query, "10.42.0.1")
        assert response is not None
        assert response[:2] == b"\xbe\xef"

    def test_sets_the_response_flag(self) -> None:
        query = _encode_query("example.com")
        response = build_wildcard_response(query, "10.42.0.1")
        assert response is not None
        flags = struct.unpack("!H", response[2:4])[0]
        assert flags & 0x8000, "QR bit must be set on a response"

    def test_answer_count_is_one(self) -> None:
        query = _encode_query("example.com")
        response = build_wildcard_response(query, "10.42.0.1")
        assert response is not None
        qdcount, ancount = struct.unpack("!HH", response[4:8])
        assert qdcount == 1
        assert ancount == 1

    def test_echoes_the_question_section_verbatim(self) -> None:
        query = _encode_query("connectivitycheck.gstatic.com")
        response = build_wildcard_response(query, "10.42.0.1")
        assert response is not None
        question = query[_HEADER_LEN:]
        assert response[_HEADER_LEN : _HEADER_LEN + len(question)] == question

    def test_answer_rdata_is_the_configured_ip(self) -> None:
        query = _encode_query("example.com")
        response = build_wildcard_response(query, "203.0.113.9")
        assert response is not None
        assert response[-4:] == bytes([203, 0, 113, 9])

    def test_answer_is_an_a_record_in_the_internet_class(self) -> None:
        query = _encode_query("example.com")
        response = build_wildcard_response(query, "10.42.0.1")
        assert response is not None
        answer = response[_HEADER_LEN + len(query[_HEADER_LEN:]) :]
        # answer = NAME pointer(2) + TYPE(2) + CLASS(2) + TTL(4) + RDLENGTH(2) + RDATA(4)
        rtype, rclass = struct.unpack("!HH", answer[2:6])
        assert rtype == 1  # A
        assert rclass == 1  # IN

    def test_answers_a_query_regardless_of_requested_record_type(self) -> None:
        """A phone's captive-portal probe may ask AAAA; wildcard DNS
        still answers with our A record so detection still fires."""
        query = _encode_query("example.com", qtype=28)  # AAAA
        response = build_wildcard_response(query, "10.42.0.1")
        assert response is not None
        assert response[-4:] == bytes([10, 42, 0, 1])

    def test_handles_a_multi_label_hostname(self) -> None:
        query = _encode_query("www.msftconnecttest.com")
        response = build_wildcard_response(query, "10.42.0.1")
        assert response is not None

    def test_returns_none_for_a_query_shorter_than_a_header(self) -> None:
        assert build_wildcard_response(b"\x00\x01", "10.42.0.1") is None

    def test_returns_none_when_qdcount_is_zero(self) -> None:
        header = struct.pack("!HHHHHH", 0x1234, 0x0100, 0, 0, 0, 0)
        assert build_wildcard_response(header, "10.42.0.1") is None

    def test_returns_none_for_a_truncated_qname(self) -> None:
        """A label length byte pointing past the end of the buffer must
        not crash the listener thread — just drop the query."""
        header = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
        truncated_qname = struct.pack("B", 10) + b"short"  # claims 10 bytes, has 5
        assert build_wildcard_response(header + truncated_qname, "10.42.0.1") is None


class TestWildcardDnsServer:
    """One smoke test over a real ephemeral-port UDP socket."""

    def test_answers_a_udp_query_with_the_configured_ip(self) -> None:
        server = WildcardDnsServer(bind_host="127.0.0.1", port=0, answer_ip="10.42.0.1")
        try:
            import threading

            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(2.0)
            query = _encode_query("captive.apple.com")
            client.sendto(query, server.server_address)
            data, _ = client.recvfrom(512)
            assert data[-4:] == bytes([10, 42, 0, 1])
        finally:
            server.shutdown()
            server.server_close()
