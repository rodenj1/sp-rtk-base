"""The Bluetooth Console link: an RFCOMM socket the driver reads like a port.

Two pieces (rtk_development#42):

- :class:`RfcommStream`, a serial-shaped stream adapter over a connected
  RFCOMM socket, so the receiver driver reads and writes it unchanged.
- :class:`BluetoothLinkOpener`, the Bluetooth kind's
  :class:`~sp_rtk_base.services.drivers.console_link.LinkOpener`. It opens
  through the relay's shared RFCOMM helper (``open_rfcomm_link``), holds
  one ``BluetoothManager`` for the whole connected session, and reports
  the connect's Stages (Pair, Connect) as they pass.

Teardown keeps ADR 0002's order across the two: closing a stream does
``Device1.Disconnect`` then closes the socket, and closing the opener at
the end of the session closes the manager last. A reopen in between (a
hardware reset, a baud reopen) closes only the stream and keeps the
manager, so teardown always has a live manager for ``Device1.Disconnect``.
"""

from __future__ import annotations

import logging
import select
import socket
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NoReturn

from sp_rtk_base.models.config_models import InputProfile
from sp_rtk_base.models.device_models import (
    BluetoothLink,
    ConnectStage,
    ConnectStageStatus,
)
from sp_rtk_base.services.drivers.console_link import LinkOpener, LinkStream

if TYPE_CHECKING:
    from sp_rtk_base_relay.core.input_sources.bluetooth_input import (
        BluetoothConfig,
    )

logger = logging.getLogger(__name__)

#: How long reads keep waiting after the last write (s). The driver's
#: read loops end on a frame or on end-of-stream, and pyubx2 never
#: reports end-of-stream while bytes keep arriving: a link carrying
#: garbage (a module at a different baud rate from its UART, seen on the
#: bench) would hold a read forever. Every driver exchange starts with a
#: write, and none waits longer than the connect's MON-VER budget (10 s),
#: so reads give up a little after that.
READ_LIMIT_S = 12.0

#: Builds the ``BluetoothManager`` for an adapter.
ManagerFactory = Callable[[str], Any]
#: Hears each connect Stage as it changes: (stage, status, code, message).
StageListener = Callable[
    [ConnectStage, ConnectStageStatus, str | None, str | None], None
]


class LinkClosedError(ConnectionError):
    """The Bluetooth module closed the link (or the link broke under us)."""


class LinkStageError(ConnectionError):
    """Opening the link failed, at a named connect Stage."""

    def __init__(self, stage: ConnectStage, code: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code


#: What the operator does next, for each way a Stage can fail.
STAGE_ADVICE: dict[str, str] = {
    "bluetooth_unavailable": (
        "The Bluetooth adapter couldn't be used. Check the adapter in the "
        "Bluetooth Input profile, and that Bluetooth is running on this host."
    ),
    "device_not_found": (
        "The Bluetooth module wasn't found. Check it is powered on and in "
        "range, and that the MAC in the Bluetooth Input profile is right."
    ),
    "pin_rejected": (
        "The module refused the PIN. Correct the PIN in the Bluetooth Input "
        "profile on the Input page, then connect again."
    ),
    "socket_refused": (
        "The module is paired but didn't accept a connection. Check it is "
        "powered on and in range, and that nothing else holds it: stop the "
        "Relay, and disconnect any other host using the module."
    ),
    "no_ubx_answer": (
        "The link connected but the receiver never answered. The module's "
        "baud rate probably doesn't match the receiver UART it is wired to: "
        "set that UART to the module's rate over a serial cable, or check "
        "the wiring."
    ),
}


def bluetooth_config_from(profile: InputProfile | None) -> BluetoothConfig | None:
    """The Bluetooth Input profile's device as the relay would use it.

    ``None`` when the Input profile isn't Bluetooth. Built the way the
    Relay's Start builds it (``to_relay_config``), so the console reaches
    the same module with the same PIN and timeouts.
    """
    if profile is None or profile.source != "bluetooth":
        return None
    from sp_rtk_base_relay.core.input_sources.bluetooth_input import (
        BluetoothConfig,
    )

    return BluetoothConfig(**profile.to_relay_config().config)


def _default_manager_factory(adapter: str) -> Any:
    """The relay's real ``BluetoothManager``, imported late (dbus-fast)."""
    from sp_rtk_base_relay.core.bluetooth_manager import BluetoothManager

    return BluetoothManager(adapter_name=adapter)


def _default_socket_factory() -> socket.socket:
    from sp_rtk_base_relay.core.rfcomm_link import rfcomm_socket

    return rfcomm_socket()


class RfcommStream:
    """A connected RFCOMM socket, shaped like the ``serial.Serial`` the driver reads.

    - ``read(size)`` returns ``size`` bytes, or what arrived before
      ``timeout`` (pyserial's contract). Once ``read_limit`` seconds have
      passed since the last write, reads return at once with what is
      already buffered, so a garbage stream can't hold a read forever.
    - ``write`` sends all of ``data`` (``sendall``); ``flush`` has nothing
      left to push. ``out_waiting`` is 0 for the same reason: RFCOMM's
      ``TIOCOUTQ`` reads a constant 50800, so it isn't read.
    - ``reset_input_buffer`` drains what's waiting without blocking.
    - When the module closes the link, reads and writes raise
      :class:`LinkClosedError`, and ``is_open`` turns false, which the
      driver and ``DeviceService`` see as a lost device. ``is_open``
      notices a closed link even before anything reads it.

    ``close`` hands the socket to ``release`` (``Device1.Disconnect``, then
    the socket close), once.
    """

    def __init__(
        self,
        sock: Any,
        *,
        timeout: float,
        release: Callable[[], None],
        read_limit: float = READ_LIMIT_S,
    ) -> None:
        self._sock = sock
        self.timeout = timeout
        self._release = release
        self._read_limit = read_limit
        self._rx = bytearray()
        self._closed = False  # by us, or by the module
        self._released = False
        self._last_write = time.monotonic()

    # -- state -----------------------------------------------------------

    @property
    def is_open(self) -> bool:
        if self._closed:
            return False
        try:
            readable, _, _ = select.select([self._sock], [], [], 0)
            if readable and not self._sock.recv(1, socket.MSG_PEEK):
                self._closed = True
        except (BlockingIOError, TimeoutError):
            pass
        except (OSError, ValueError):
            self._closed = True
        return not self._closed

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    @property
    def out_waiting(self) -> int:
        return 0

    def close(self) -> None:
        self._closed = True
        if not self._released:
            self._released = True
            self._release()

    # -- reading ---------------------------------------------------------

    def read(self, size: int = 1) -> bytes:
        now = time.monotonic()
        deadline = min(now + self.timeout, self._last_write + self._read_limit)
        while len(self._rx) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not self._recv_into_buffer(remaining):
                break
        out = bytes(self._rx[:size])
        del self._rx[:size]
        return out

    def readline(self) -> bytes:
        line = bytearray()
        while not line.endswith(b"\n"):
            byte = self.read(1)
            if not byte:
                break
            line += byte
        return bytes(line)

    def reset_input_buffer(self) -> None:
        self._rx.clear()
        while self._recv_into_buffer(0):
            self._rx.clear()

    def _recv_into_buffer(self, wait: float) -> bool:
        """Receive what arrives within ``wait`` s; ``False`` if nothing did."""
        if self._closed:
            raise LinkClosedError("The Bluetooth link is closed")
        try:
            if wait <= 0:
                readable, _, _ = select.select([self._sock], [], [], 0)
                if not readable:
                    return False
            else:
                self._sock.settimeout(wait)
            chunk = self._sock.recv(4096)
        except TimeoutError:
            return False
        except OSError as exc:
            self._closed = True
            raise LinkClosedError(f"The Bluetooth link broke: {exc}") from exc
        if not chunk:
            self._closed = True
            raise LinkClosedError("The Bluetooth module closed the link")
        self._rx += chunk
        return True

    # -- writing ---------------------------------------------------------

    def write(self, data: bytes) -> int:
        if self._closed:
            raise LinkClosedError("The Bluetooth link is closed")
        try:
            self._sock.settimeout(self.timeout)
            self._sock.sendall(data)
        except OSError as exc:
            self._closed = True
            raise LinkClosedError(f"The Bluetooth link broke: {exc}") from exc
        self._last_write = time.monotonic()
        return len(data)

    def flush(self) -> None:
        """Nothing to push: ``sendall`` returned only once the kernel had it all."""


class BluetoothLinkOpener(LinkOpener):
    """Opens the Bluetooth Input profile's module as the Console link.

    The ``BluetoothManager`` is created on the first open and kept until
    :meth:`close`, at the end of the connected session. Every open pairs
    on demand and connects through the relay's ``open_rfcomm_link``.
    """

    def __init__(
        self,
        config: BluetoothConfig,
        *,
        manager_factory: ManagerFactory = _default_manager_factory,
        socket_factory: Callable[[], Any] = _default_socket_factory,
        read_limit: float = READ_LIMIT_S,
        on_stage: StageListener | None = None,
    ) -> None:
        self._config = config
        self._manager_factory = manager_factory
        self._socket_factory = socket_factory
        self._read_limit = read_limit
        self.on_stage = on_stage
        self._manager: Any = None
        self._bonds_created: list[bool] = []
        self._stream: RfcommStream | None = None

    @property
    def link(self) -> BluetoothLink:
        return BluetoothLink(
            device_name=self._config.device_name, mac=self._config.mac_address
        )

    def at_baud(self, baud_rate: int) -> BluetoothLinkOpener:
        """Itself: the module keeps its own rate whatever the receiver's."""
        return self

    def open(self, timeout: float) -> LinkStream:
        from sp_rtk_base_relay.core.bluetooth_manager import BluetoothError
        from sp_rtk_base_relay.core.rfcomm_link import (
            RfcommConnectError,
            open_rfcomm_link,
        )

        if self._stream is not None:
            self._stream.close()
            self._stream = None
        self._report(ConnectStage.PAIR, ConnectStageStatus.RUNNING)
        if self._manager is None:
            try:
                self._manager = self._manager_factory(self._config.adapter_name)
            except Exception as exc:
                self._fail(ConnectStage.PAIR, "bluetooth_unavailable", exc)
            self._bonds_created = _record_bond_creation(self._manager)
        manager = self._manager
        self._bonds_created.clear()

        def _socket() -> Any:
            # Called by the helper right before it connects: preparing the
            # device (the Pair Stage) is over.
            if self._bonds_created:
                self._report(ConnectStage.PAIR, ConnectStageStatus.PASSED)
            else:
                self._report(
                    ConnectStage.PAIR,
                    ConnectStageStatus.SKIPPED,
                    "bonded",
                    "Already paired",
                )
            self._report(ConnectStage.CONNECT, ConnectStageStatus.RUNNING)
            return self._socket_factory()

        try:
            link = open_rfcomm_link(manager, self._config, socket_factory=_socket)
        except BluetoothError as exc:
            self._fail(ConnectStage.PAIR, self._pair_failure_code(manager), exc)
        except (RfcommConnectError, OSError) as exc:
            self._fail(ConnectStage.CONNECT, "socket_refused", exc)

        self._report(
            ConnectStage.CONNECT,
            ConnectStageStatus.PASSED,
            message=f"RFCOMM channel {link.channel}",
        )
        mac = link.mac
        sock = link.socket

        def _release() -> None:
            # ADR 0002's first two steps; the manager closes last, in close().
            try:
                manager.disconnect_device(mac)
            except Exception as exc:
                logger.warning("Error disconnecting Bluetooth D-Bus: %s", exc)
            try:
                sock.close()
            except Exception as exc:
                logger.warning("Error closing the Bluetooth socket: %s", exc)

        self._stream = RfcommStream(
            sock, timeout=timeout, release=_release, read_limit=self._read_limit
        )
        return self._stream

    def close(self) -> None:
        """End the session: the stream (if still held), then the manager."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()
        manager, self._manager = self._manager, None
        if manager is not None:
            try:
                manager.close()
            except Exception as exc:
                logger.warning("Error closing the BluetoothManager: %s", exc)

    def _pair_failure_code(self, manager: Any) -> str:
        """Tell an absent module from a refused PIN, as the Verification does."""
        try:
            found = bool(manager.find_device_by_mac(self._config.mac_address))
        except Exception:
            found = True
        return "pin_rejected" if found else "device_not_found"

    def _report(
        self,
        stage: ConnectStage,
        status: ConnectStageStatus,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        if self.on_stage is not None:
            self.on_stage(stage, status, code, message)

    def _fail(self, stage: ConnectStage, code: str, exc: Exception) -> NoReturn:
        self._report(stage, ConnectStageStatus.FAILED, code, str(exc))
        raise LinkStageError(
            stage, code, f"{stage.value.capitalize()} failed: {exc}"
        ) from exc


def _record_bond_creation(manager: Any) -> list[bool]:
    """Note each time pairing creates a Bond, for the manager's lifetime.

    ``pair_device`` returns ``True`` when it created a Bond and ``False``
    when one already existed, and the relay's ``ensure_device_ready``
    calls it through the instance. That return value is the only way to
    know the Pair Stage was skipped (there is no public ``Paired``
    accessor, ADR 0001), so the instance's ``pair_device`` is wrapped,
    once, to record it. The caller clears the list before each open.
    """
    created: list[bool] = []
    original: Callable[[str, str], object] = manager.pair_device

    def pair_device(mac_address: str, pin: str = "0000") -> bool:
        result = bool(original(mac_address, pin))
        if result:
            created.append(True)
        return result

    manager.pair_device = pair_device
    return created
