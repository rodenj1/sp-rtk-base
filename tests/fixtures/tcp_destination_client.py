"""TCP destination client for integration testing.

Connects to the relay's tcp_server_destination port and collects
received data for verification.

Reusable for any integration test that needs to verify data reaches
a TCP server destination.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from types import TracebackType

logger = logging.getLogger(__name__)


class TCPDestinationClient:
    """TCP client that connects to a relay tcp_server destination.

    Collects all received data into a buffer for test verification.

    Args:
        host: Destination host to connect to.
        port: Destination port to connect to.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._buffer = bytearray()
        self._lock = threading.Lock()

    @property
    def bytes_received(self) -> int:
        """Total bytes received so far."""
        with self._lock:
            return len(self._buffer)

    @property
    def data(self) -> bytes:
        """Copy of all received data."""
        with self._lock:
            return bytes(self._buffer)

    def connect(self, timeout: float = 10.0, retry_interval: float = 0.5) -> None:
        """Connect to the destination with retry.

        Args:
            timeout: Max seconds to wait for connection.
            retry_interval: Seconds between retry attempts.

        Raises:
            ConnectionError: If unable to connect within timeout.
        """
        if self._running:
            return

        deadline = time.time() + timeout
        last_error: Exception | None = None

        sock: socket.socket | None = None
        while time.time() < deadline:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect((self.host, self.port))
                self._socket = sock
                self._running = True

                self._thread = threading.Thread(
                    target=self._recv_loop, name="TCPDestClient", daemon=True
                )
                self._thread.start()

                logger.info("TCP dest client connected to %s:%d", self.host, self.port)
                return
            except (ConnectionRefusedError, OSError) as exc:
                last_error = exc
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    sock = None
                time.sleep(retry_interval)

        msg = f"Could not connect to {self.host}:{self.port} within {timeout}s"
        raise ConnectionError(msg) from last_error

    def disconnect(self) -> None:
        """Disconnect and stop collecting data."""
        self._running = False

        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def wait_for_data(self, min_bytes: int, timeout: float = 10.0) -> bool:
        """Wait until at least min_bytes have been received.

        Args:
            min_bytes: Minimum number of bytes to wait for.
            timeout: Max seconds to wait.

        Returns:
            True if min_bytes reached, False on timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.bytes_received >= min_bytes:
                return True
            time.sleep(0.1)
        return False

    def _recv_loop(self) -> None:
        """Receive data from the socket until stopped."""
        while self._running and self._socket:
            try:
                chunk = self._socket.recv(4096)
                if not chunk:
                    break
                with self._lock:
                    self._buffer.extend(chunk)
            except TimeoutError:
                continue
            except OSError:
                break

    # Context manager support
    def __enter__(self) -> TCPDestinationClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.disconnect()
