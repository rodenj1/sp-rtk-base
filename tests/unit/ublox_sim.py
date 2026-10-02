"""A simulated u-blox receiver link for driver tests.

Stands in for the serial port and the UBX reader the UbloxDriver holds. It
answers CFG-VALSET (ACK, storing RAM values) and CFG-VALGET polls (the RAM
values), and records every write: the UBX messages it parsed, and any
non-UBX bytes (e.g. RTCM 3 correction Frames) as they were written.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any

from pyubx2 import POLL, SET, UBXReader
from pyubx2.ubxtypes_configdb import UBX_CONFIG_DATABASE

from sp_rtk_base.services.drivers.ublox import UbloxDriver

_KEY_NAMES = {keyid: name for name, (keyid, _) in UBX_CONFIG_DATABASE.items()}
_UBX_SYNC = b"\xb5\x62"
_VALSET = b"\x06\x8a"
_VALGET = b"\x06\x8b"


class SimReceiver:
    """The serial side and the reader side of one simulated receiver."""

    def __init__(self, ram: dict[str, int] | None = None) -> None:
        self.ram: dict[str, int] = dict(ram or {})
        self.valsets: list[Any] = []  # parsed CFG-VALSET messages, in order
        self.raw_writes: list[bytes] = []  # non-UBX bytes, as written
        self.is_open = True
        self._replies: deque[Any] = deque()

    # ---- serial side ----
    def write(self, data: bytes) -> int:
        if not data.startswith(_UBX_SYNC):
            self.raw_writes.append(bytes(data))
            return len(data)
        msg_id = data[2:4]
        if msg_id == _VALSET:
            message = UBXReader.parse(data, msgmode=SET)
            self.valsets.append(message)
            if message.ram:
                for name in UBX_CONFIG_DATABASE:
                    value = getattr(message, name, None)
                    if value is not None:
                        self.ram[name] = int(value)
            self._replies.append(SimpleNamespace(identity="ACK-ACK"))
        elif msg_id == _VALGET:
            poll = UBXReader.parse(data, msgmode=POLL)
            values: dict[str, int] = {}
            index = 1
            while (keyid := getattr(poll, f"keys_{index:02d}", None)) is not None:
                name = _KEY_NAMES[keyid]
                if name in self.ram:
                    values[name] = self.ram[name]
                index += 1
            self._replies.append(SimpleNamespace(identity="CFG-VALGET", **values))
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        pass

    # ---- reader side ----
    def read(self) -> tuple[bytes | None, Any]:
        if self._replies:
            return b"", self._replies.popleft()
        return None, None


def connected_driver(sim: SimReceiver) -> UbloxDriver:
    """A UbloxDriver whose link is ``sim``."""
    driver = UbloxDriver()
    driver._serial = sim  # pyright: ignore[reportPrivateUsage]
    driver._reader = sim  # pyright: ignore[reportPrivateUsage]
    return driver
