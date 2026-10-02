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
_MON_COMMS = b"\x0a\x36"


class SimReceiver:
    """The serial side and the reader side of one simulated receiver."""

    def __init__(self, ram: dict[str, int] | None = None) -> None:
        self.ram: dict[str, int] = dict(ram or {})
        self.valsets: list[Any] = []  # parsed CFG-VALSET messages, in order
        self.raw_writes: list[bytes] = []  # non-UBX bytes, as written
        # What MON-COMMS reports: {"protIds": [4 ids], "ports": {portId: {...}}}
        self.mon_comms: dict[str, Any] | None = None
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
        elif msg_id == _MON_COMMS and self.mon_comms is not None:
            self._replies.append(self._mon_comms_reply(self.mon_comms))
        return len(data)

    @staticmethod
    def _mon_comms_reply(spec: dict[str, Any]) -> SimpleNamespace:
        fields: dict[str, Any] = {"identity": "MON-COMMS", "nPorts": len(spec["ports"])}
        for slot, prot in enumerate(spec["protIds"], start=1):
            fields[f"protIds_{slot:02d}"] = prot
        for i, (port_id, port) in enumerate(spec["ports"].items(), start=1):
            n = f"{i:02d}"
            fields[f"portId_{n}"] = port_id
            fields[f"rxBytes_{n}"] = port["rxBytes"]
            fields[f"skipped_{n}"] = port["skipped"]
            fields[f"overrunErrs_{n}"] = port["overrunErrs"]
            for slot, count in enumerate(port["msgs"], start=1):
                fields[f"msgs_{n}_{slot:02d}"] = count
        return SimpleNamespace(**fields)

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
