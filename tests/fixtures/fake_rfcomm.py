"""A fake Bluetooth module in front of the simulated receiver (rtk_development#42).

Three fakes stand in for what BlueZ and the HC-05-family module do on the
bench, so the Bluetooth Console link can be driven end to end without an
adapter:

- :class:`FakeBlueZ` is BlueZ, and each :class:`FakeBluetoothManager` built
  over it is the relay's ``BluetoothManager`` as the shared RFCOMM helper
  uses it. ``ensure_device_ready`` pairs through
  ``self.pair_device`` exactly as the real one does, returning ``False``
  when a Bond already exists, and a wrong PIN or an absent module raise
  ``BluetoothError``.
- :class:`FakeRfcommModule` is the module wired to a receiver UART. Its
  ``new_socket`` is the helper's ``socket_factory``: each connected socket is
  one end of a real ``socketpair``, and a thread on the other end feeds the
  bytes to the :class:`SimulatedUblox` and writes its answers back. Real
  socket semantics come for free: reads time out, and a closed end reads
  as end-of-stream.
"""

from __future__ import annotations

import errno
import socket
import threading
from typing import Any

from sp_rtk_base_relay.core.bluetooth_manager import BluetoothError

from tests.fixtures.simulated_ublox import SimulatedSerial, SimulatedUblox

MAC = "98:D3:31:F5:12:34"
DEVICE_NAME = "RTK_BASE_TST"
PIN = "1234"

#: Bytes that never frame as UBX, NMEA or RTCM 3: what a receiver UART
#: looks like through a module at the wrong baud rate.
GARBAGE = bytes(range(0x01, 0x20)) * 32


class FakeBlueZ:
    """BlueZ on the host, as the Bluetooth managers built over it see it.

    Holds what outlives one ``BluetoothManager``: the Bonds, whether the
    module can be found, and a log of the teardown calls every manager
    made, in order. :meth:`manager` is the ``manager_factory``: each call
    builds a fresh :class:`FakeBluetoothManager` over this BlueZ.
    """

    def __init__(self, *, pin: str = PIN, bonded: bool = True) -> None:
        self.module_pin = pin
        self.bonded: set[str] = {MAC} if bonded else set()
        #: Whether the module is powered and in range (BlueZ can find it).
        self.present = True
        #: Teardown calls, in order: ``("disconnect", mac)``, ``("close", n)``
        #: where ``n`` numbers the manager closed.
        self.log: list[tuple[str, object]] = []
        self.managers: list[FakeBluetoothManager] = []

    def manager(self, adapter: str = "hci0") -> FakeBluetoothManager:
        made = FakeBluetoothManager(self, number=len(self.managers))
        self.managers.append(made)
        return made

    @property
    def disconnects(self) -> list[object]:
        """Each ``Device1.Disconnect``'s MAC, in order."""
        return [arg for call, arg in self.log if call == "disconnect"]

    @property
    def closed(self) -> int:
        """How many managers have been closed."""
        return sum(1 for call, _ in self.log if call == "close")

    @property
    def live(self) -> int:
        """How many managers are built and not yet closed."""
        return sum(1 for m in self.managers if not m.is_closed)


class FakeBluetoothManager:
    """The relay's ``BluetoothManager``, as far as the RFCOMM helper uses it.

    Like the real one, it is no use once closed: any call raises.
    """

    def __init__(self, bluez: FakeBlueZ, *, number: int = 0) -> None:
        self._bluez = bluez
        self._number = number
        self.is_closed = False

    def _check_open(self) -> None:
        if self.is_closed:
            raise BluetoothError("BluetoothManager used after close()")

    def ensure_device_ready(
        self,
        pin: str,
        device_name: str | None = None,
        mac_address: str | None = None,
        scan_timeout: int = 30,
    ) -> tuple[str, int]:
        self._check_open()
        mac = mac_address or MAC
        if mac not in self._bluez.bonded and not self._bluez.present:
            raise BluetoothError(f"Device {mac} not found after {scan_timeout}s")
        self.pair_device(mac, pin)
        return mac, 1

    def pair_device(self, mac_address: str, pin: str = "0000") -> bool:
        self._check_open()
        if mac_address in self._bluez.bonded:
            return False
        if pin != self._bluez.module_pin:
            raise BluetoothError(
                f"Pairing failed: org.bluez.Error.AuthenticationFailed ({mac_address})"
            )
        self._bluez.bonded.add(mac_address)
        return True

    def find_device_by_mac(self, mac_address: str) -> bool:
        self._check_open()
        return self._bluez.present or mac_address in self._bluez.bonded

    def disconnect_device(self, mac_address: str) -> bool:
        self._check_open()
        self._bluez.log.append(("disconnect", mac_address))
        return True

    def close(self) -> None:
        if not self.is_closed:
            self.is_closed = True
            self._bluez.log.append(("close", self._number))


class FakeRfcommModule:
    """The Bluetooth module wired to the receiver: one RFCOMM socket per open."""

    def __init__(self, receiver: SimulatedUblox) -> None:
        self.receiver = receiver
        #: Whether the module accepts an RFCOMM connection.
        self.answers = True
        #: Whether the module sends garbage instead of the receiver's bytes.
        self.garbage = False
        self.connects = 0
        self._peer: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    def new_socket(self) -> FakeRfcommSocket:
        """The RFCOMM helper's ``socket_factory``."""
        return FakeRfcommSocket(self)

    def drop(self) -> None:
        """The module closes the link (powered off, or out of range)."""
        if self._peer is not None:
            # shutdown, not just close: the serving thread is blocked in
            # recv on it, and only shutdown sends the end-of-stream.
            try:
                self._peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._peer.close()
            self._peer = None

    def stop(self) -> None:
        self.drop()
        for thread in self._threads:
            thread.join(timeout=2)

    def accept(self) -> socket.socket:
        """Accept one RFCOMM connection, as the module."""
        if not self.answers:
            raise OSError(errno.EHOSTDOWN, "Host is down")
        ours, peer = socket.socketpair()
        self.drop()  # one link at a time, as on the module
        self._peer = peer
        self.connects += 1
        target = self._spew if self.garbage else self._serve
        thread = threading.Thread(target=target, args=(peer,), daemon=True)
        thread.start()
        self._threads.append(thread)
        return ours

    def _serve(self, peer: socket.socket) -> None:
        uart = SimulatedSerial(self.receiver)
        while True:
            try:
                data = peer.recv(4096)
            except OSError:
                return
            if not data:
                return
            uart.write(data)
            answer = uart.read(uart.in_waiting)
            if answer:
                try:
                    peer.sendall(answer)
                except OSError:
                    return

    @staticmethod
    def _spew(peer: socket.socket) -> None:
        peer.settimeout(0.05)
        while True:
            try:
                peer.sendall(GARBAGE)
                peer.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                return


class FakeRfcommSocket:
    """An unconnected RFCOMM socket; connecting it reaches the module."""

    def __init__(self, module: FakeRfcommModule) -> None:
        self._module = module
        self._sock: socket.socket | None = None
        self._timeout: float | None = None

    def settimeout(self, value: float | None) -> None:
        self._timeout = value
        if self._sock is not None:
            self._sock.settimeout(value)

    def connect(self, address: tuple[str, int]) -> None:
        self._sock = self._module.accept()
        self._sock.settimeout(self._timeout)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()

    def __getattr__(self, name: str) -> Any:
        if self._sock is None:
            raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")
        return getattr(self._sock, name)
