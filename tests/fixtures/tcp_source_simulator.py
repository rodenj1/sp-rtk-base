# pyright: reportUnknownMemberType=false
"""TCP RTCM source simulator for integration testing.

Provides a TCP server that streams synthetic RTCM data to connected
clients, simulating a GPS receiver (e.g., ublox ZED-F9P) connected
via a TCP serial server.

Reusable for any integration test that needs a simulated RTCM source.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
import time
from types import TracebackType

logger = logging.getLogger(__name__)

# CRC24Q lookup table for RTCM3 checksum
_CRC24Q_TABLE: list[int] = []


def _init_crc_table() -> None:
    """Initialize the CRC24Q lookup table."""
    poly = 0x1864CFB
    for i in range(256):
        crc = i << 16
        for _ in range(8):
            crc = (crc << 1) ^ poly if crc & 0x800000 else crc << 1
            crc &= 0xFFFFFF
        _CRC24Q_TABLE.append(crc)


_init_crc_table()


def _crc24q(data: bytes) -> int:
    """Calculate CRC24Q checksum for RTCM3 data."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ _CRC24Q_TABLE[((crc >> 16) ^ b) & 0xFF]
    return crc


def _make_rtcm_frame(payload: bytes) -> bytes:
    """Wrap a payload into a valid RTCM3 frame (preamble + len + payload + CRC24Q)."""
    length = len(payload)
    header = bytes([0xD3, (length >> 8) & 0x03, length & 0xFF])
    frame_no_crc = header + payload
    crc = _crc24q(frame_no_crc)
    return frame_no_crc + struct.pack(">I", crc)[1:]  # 3-byte big-endian CRC


def generate_rtcm_chunk(size: int = 200) -> bytes:
    """Generate a chunk of valid RTCM3 frames.

    Produces RTCM Type 1005 (station position) frames with random-ish
    content, totalling approximately ``size`` bytes.

    Args:
        size: Target chunk size in bytes.

    Returns:
        Binary data containing one or more valid RTCM3 frames.
    """
    buf = bytearray()
    while len(buf) < size:
        # Build a minimal Type 1005 payload (~19 bytes)
        # Message type 1005 = 0x3ED in 12 bits
        payload = bytearray(19)
        payload[0] = 0x3E  # high 8 bits of msg type 1005 = 0x3ED
        payload[1] = 0xD0  # low 4 bits of type + high 4 bits of station id
        # Fill rest with pseudo-random bytes for variety
        payload[2:] = os.urandom(17)
        buf.extend(_make_rtcm_frame(bytes(payload)))
    return bytes(buf[:size])


class TCPSourceSimulator:
    """TCP server that streams synthetic RTCM data to connected clients.

    Mimics a GPS device streaming RTCM corrections over TCP.
    Generates valid RTCM3 frames at a configurable data rate.

    Args:
        host: Bind address.
        port: Bind port (0 = auto-assign).
        data_rate_bps: Target data rate in bytes per second.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        data_rate_bps: int = 2000,
    ) -> None:
        self._host = host
        self._port = port
        self._data_rate_bps = data_rate_bps
        self._server_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._actual_port: int = 0
        self._bytes_sent = 0

    @property
    def port(self) -> int:
        """Actual bound port (resolved after start if port=0)."""
        return self._actual_port

    @property
    def host(self) -> str:
        """Bind address."""
        return self._host

    @property
    def bytes_sent(self) -> int:
        """Total bytes sent to all clients since start."""
        return self._bytes_sent

    def start(self) -> None:
        """Start the TCP source simulator server."""
        if self._running:
            return

        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.settimeout(1.0)
        self._server_socket.bind((self._host, self._port))
        self._server_socket.listen(5)

        self._actual_port = self._server_socket.getsockname()[1]
        self._running = True
        self._bytes_sent = 0

        self._thread = threading.Thread(
            target=self._serve_loop, name="TCPSourceSim", daemon=True
        )
        self._thread.start()
        logger.info(
            "TCP source simulator started on %s:%d", self._host, self._actual_port
        )

    def stop(self) -> None:
        """Stop the simulator and close all connections."""
        if not self._running:
            return
        self._running = False

        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

        logger.info(
            "TCP source simulator stopped (sent %d bytes total)", self._bytes_sent
        )

    def _serve_loop(self) -> None:
        """Accept connections and stream data to each client."""
        while self._running and self._server_socket:
            try:
                client_sock, addr = self._server_socket.accept()
                logger.info("Source sim: client connected from %s", addr)
                handler = threading.Thread(
                    target=self._stream_to_client,
                    args=(client_sock,),
                    name=f"TCPSourceSim-{addr}",
                    daemon=True,
                )
                handler.start()
            except TimeoutError:
                continue
            except OSError:
                break

    def _stream_to_client(self, client_sock: socket.socket) -> None:
        """Stream RTCM data to a connected client at the configured rate."""
        try:
            client_sock.settimeout(2.0)
            interval = 0.1  # send every 100ms
            chunk_size = max(1, int(self._data_rate_bps * interval))

            while self._running:
                data = generate_rtcm_chunk(chunk_size)
                try:
                    client_sock.sendall(data)
                    self._bytes_sent += len(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
                time.sleep(interval)
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

    # Context manager support
    def __enter__(self) -> TCPSourceSimulator:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()
