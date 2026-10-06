"""Opening the Console link, one opener per kind (rtk_development#40).

A receiver driver never opens a host path at a baud rate itself. It asks a
:class:`LinkOpener` for a serial-shaped byte stream, and keeps the opener
for as long as it stays connected, so that every reopen (after a baud
change, after a hardware reset) goes back through the same opener.

Each kind of Console link (see ``CONTEXT.md``) has its own opener. The
Serial kind's is :class:`SerialLinkOpener`. A Bluetooth kind
(rtk_development#42) adds its own subclass: ``open`` returns an RFCOMM
stream adapter, ``at_baud`` returns the opener unchanged (the module does
not follow a receiver baud change), and ``close`` tears the session down.
"""

from __future__ import annotations

import abc
import fcntl
from typing import Protocol

import serial  # type: ignore[import-untyped]

from sp_rtk_base.models.device_models import ConsoleLink, SerialLink


class LinkStream(Protocol):
    """The serial-shaped byte stream a driver reads and writes."""

    @property
    def is_open(self) -> bool: ...

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int | None: ...

    def reset_input_buffer(self) -> None: ...

    def close(self) -> None: ...


class LinkOpener(abc.ABC):
    """Opens one Console link, as often as the driver needs it reopened."""

    @property
    @abc.abstractmethod
    def link(self) -> ConsoleLink:
        """The Console link this opener opens."""

    @abc.abstractmethod
    def open(self, timeout: float) -> LinkStream:
        """Open the link as a stream whose reads give up after ``timeout`` s.

        Raises:
            ConnectionError: If the link cannot be opened, with a message
                fit to show the operator.
        """

    @abc.abstractmethod
    def at_baud(self, baud_rate: int) -> LinkOpener:
        """The opener to reopen through after the receiver's baud changed.

        A kind whose host side has no baud rate returns itself.
        """

    def close(self) -> None:  # noqa: B027 - deliberately a no-op by default
        """Release what the link holds for a whole connected session.

        Called once, when the driver disconnects; never between the
        reopens of one session. Nothing to release for a serial link.
        """


class SerialLinkOpener(LinkOpener):
    """Opens a host serial device, exclusively, at the link's baud rate."""

    def __init__(self, link: SerialLink) -> None:
        self._link = link

    @property
    def link(self) -> SerialLink:
        return self._link

    def open(self, timeout: float) -> LinkStream:
        port = self._link.port
        try:
            stream = serial.Serial(
                port=port,
                baudrate=self._link.baud_rate,
                timeout=timeout,
                exclusive=True,  # TIOCEXCL: the kernel refuses other opens
            )
        except serial.SerialException as exc:
            raise ConnectionError(f"Failed to open {port}: {exc}") from exc
        # Advisory lock: a clear error if another process sneaks in.
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise ConnectionError(
                f"Serial port {port} is locked by another process"
            ) from exc
        return stream  # type: ignore[no-any-return]

    def at_baud(self, baud_rate: int) -> SerialLinkOpener:
        return SerialLinkOpener(self._link.model_copy(update={"baud_rate": baud_rate}))
