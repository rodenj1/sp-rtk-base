"""Mock NTRIP Caster for integration testing.

A lightweight TCP server that speaks NTRIP v1.0 and v2.0 protocol,
accepting connections from NtripDestination and collecting streamed
RTCM data for test assertions.

Usage::

    with MockNtripCaster(port=0, password="test123") as caster:
        # caster.port gives the actual ephemeral port
        # ... connect NtripDestination to localhost:caster.port ...
        data = caster.get_received_data()
"""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MockNtripCaster:
    """Mock NTRIP caster for testing NtripDestination.

    Listens on a TCP port, accepts one connection at a time,
    validates the NTRIP handshake, and collects incoming RTCM data.

    Supports both NTRIP v1.0 (SOURCE) and v2.0 (POST) protocols.

    Args:
        port: TCP port to listen on (0 = ephemeral).
        password: Expected password for authentication.
        accept_auth: If False, reject all auth attempts.
        disconnect_after_bytes: Close connection after receiving N bytes (0 = never).
    """

    port: int = 0
    password: str = "test_password"
    accept_auth: bool = True
    disconnect_after_bytes: int = 0

    # Internal state (set after start)
    _server_socket: socket.socket | None = field(default=None, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)
    _received_data: bytearray = field(default_factory=bytearray, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _detected_version: str = field(default="", repr=False)
    _request_headers: str = field(default="", repr=False)
    _client_connected: threading.Event = field(
        default_factory=threading.Event, repr=False
    )
    _connection_count: int = field(default=0, repr=False)

    def start(self) -> None:
        """Start the mock caster listening on the configured port."""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind(("127.0.0.1", self.port))
        self._server_socket.listen(1)
        self._server_socket.settimeout(1.0)  # Allow periodic shutdown check

        # Update port if ephemeral
        actual_addr = self._server_socket.getsockname()
        self.port = actual_addr[1]

        self._running = True
        self._thread = threading.Thread(
            target=self._serve_loop, daemon=True, name="mock-ntrip-caster"
        )
        self._thread.start()
        logger.debug(f"MockNtripCaster started on port {self.port}")

    def stop(self) -> None:
        """Stop the mock caster."""
        self._running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.debug("MockNtripCaster stopped")

    def __enter__(self) -> MockNtripCaster:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    def get_received_data(self) -> bytes:
        """Return all RTCM data received from the NTRIP server."""
        with self._lock:
            return bytes(self._received_data)

    def wait_for_connection(self, timeout: float = 5.0) -> bool:
        """Wait until a client has connected and completed handshake."""
        return self._client_connected.wait(timeout=timeout)

    @property
    def detected_version(self) -> str:
        """NTRIP version detected from client request ('1.0' or '2.0')."""
        return self._detected_version

    @property
    def request_headers(self) -> str:
        """Raw request headers received from the client."""
        return self._request_headers

    @property
    def connection_count(self) -> int:
        """Number of connections accepted so far."""
        return self._connection_count

    @property
    def received_bytes(self) -> int:
        """Total bytes of RTCM data received."""
        with self._lock:
            return len(self._received_data)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _serve_loop(self) -> None:
        """Main server loop — accept connections and handle them."""
        while self._running:
            try:
                assert self._server_socket is not None
                client_sock, addr = self._server_socket.accept()
                self._connection_count += 1
                logger.debug(f"MockNtripCaster: connection from {addr}")
                self._handle_client(client_sock)
            except TimeoutError:
                continue
            except OSError:
                if self._running:
                    logger.debug("MockNtripCaster: server socket error")
                break

    def _handle_client(self, client_sock: socket.socket) -> None:
        """Handle a single client connection."""
        client_sock.settimeout(5.0)

        try:
            # Read the request headers
            request = self._read_request(client_sock)
            self._request_headers = request

            # Detect protocol version and validate
            if request.startswith("SOURCE"):
                self._detected_version = "1.0"
                accepted = self._handle_v1(client_sock, request)
            elif request.startswith("POST"):
                self._detected_version = "2.0"
                accepted = self._handle_v2(client_sock, request)
            else:
                logger.debug(f"MockNtripCaster: unknown request: {request!r}")
                client_sock.close()
                return

            if not accepted:
                client_sock.close()
                return

            # Signal that handshake is complete
            self._client_connected.set()

            # Collect incoming data
            self._collect_data(client_sock)

        except OSError as e:
            logger.debug(f"MockNtripCaster: client error: {e}")
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

    def _handle_v1(self, sock: socket.socket, request: str) -> bool:
        """Handle NTRIP v1.0 SOURCE request.

        Returns True if auth accepted.
        """
        # Parse: "SOURCE <password> /<mountpoint>\r\n..."
        parts = request.split("\r\n")[0].split(" ")
        if len(parts) >= 2:
            received_password = parts[1]
        else:
            received_password = ""

        if self.accept_auth and received_password == self.password:
            sock.sendall(b"ICY 200 OK\r\n")
            return True
        else:
            sock.sendall(b"ERROR - Bad Password\r\n")
            return False

    def _handle_v2(self, sock: socket.socket, request: str) -> bool:
        """Handle NTRIP v2.0 POST request.

        Returns True if auth accepted.
        """
        if not self.accept_auth:
            sock.sendall(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
            return False

        # Just accept — we don't validate Base64 in the mock
        sock.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        return True

    def _collect_data(self, sock: socket.socket) -> None:
        """Collect RTCM data from the connected server.

        For v1.0: raw bytes.
        For v2.0: decode HTTP chunked encoding.
        """
        sock.settimeout(2.0)
        total_received = 0

        try:
            while self._running:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break

                    if self._detected_version == "2.0":
                        # Decode chunked encoding
                        decoded = self._decode_chunks(chunk)
                    else:
                        decoded = chunk

                    with self._lock:
                        self._received_data.extend(decoded)
                    total_received += len(decoded)

                    # Optional: disconnect after N bytes
                    if (
                        self.disconnect_after_bytes > 0
                        and total_received >= self.disconnect_after_bytes
                    ):
                        logger.debug(
                            f"MockNtripCaster: disconnecting after "
                            f"{total_received} bytes"
                        )
                        break

                except TimeoutError:
                    continue
        except OSError:
            pass

    @staticmethod
    def _decode_chunks(raw: bytes) -> bytes:
        """Decode HTTP chunked transfer encoding.

        Simple implementation: parse hex_len\\r\\ndata\\r\\n sequences.
        Handles partial chunks and multiple chunks in one recv().
        """
        result = bytearray()
        remaining = raw

        while remaining:
            # Find the chunk size line
            crlf_idx = remaining.find(b"\r\n")
            if crlf_idx < 0:
                break

            size_str = remaining[:crlf_idx].decode("ascii", errors="replace").strip()
            if not size_str:
                remaining = remaining[crlf_idx + 2 :]
                continue

            try:
                chunk_size = int(size_str, 16)
            except ValueError:
                break

            if chunk_size == 0:
                break

            data_start = crlf_idx + 2
            data_end = data_start + chunk_size

            if data_end > len(remaining):
                # Partial chunk — take what we have
                result.extend(remaining[data_start:])
                break

            result.extend(remaining[data_start:data_end])

            # Skip trailing \r\n after data
            next_start = data_end + 2
            if next_start > len(remaining):
                break
            remaining = remaining[next_start:]

        return bytes(result)

    @staticmethod
    def _read_request(sock: socket.socket) -> str:
        """Read the request headers from the client.

        Reads until \\r\\n\\r\\n (end of headers).
        """
        buf = b""
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if b"\r\n\r\n" in buf:
                    break
                # For v1.0, headers end after Source-Agent line
                if b"\r\n" in buf and buf.startswith(b"SOURCE"):
                    break
            except TimeoutError:
                break
        return buf.decode("ascii", errors="replace")
