"""A simulated u-blox receiver link for driver tests.

Stands in for the serial port and the UBX reader the UbloxDriver holds. It
answers CFG-VALSET (ACK, storing RAM values) and CFG-VALGET polls (the RAM
values), and records every write: the UBX messages it parsed, and any
non-UBX bytes (e.g. RTCM 3 correction Frames) as they were written.
"""

from __future__ import annotations

import time
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
_NAV_PVT = b"\x01\x07"
_NAV_HPPOSECEF = b"\x01\x13"
# A Fixed, corrected NAV-PVT and its high-precision ECEF companion.
_NAV_REPLIES: dict[bytes, Any] = {
    _NAV_PVT: SimpleNamespace(
        identity="NAV-PVT",
        fixType=3,
        gnssFixOk=1,
        carrSoln=2,
        diffSoln=1,
        lastCorrectionAge=2,
        lat=32.7329015,
        lon=-117.2362788,
        height=27940,
        hMSL=-5060,
        hAcc=14,
        vAcc=21,
        numSV=24,
        gSpeed=0,
        headMot=0.0,
        pDOP=0.8,
    ),
    _NAV_HPPOSECEF: SimpleNamespace(
        identity="NAV-HPPOSECEF",
        ecefX=-246_041_234.5,
        ecefY=-477_939_123.4,
        ecefZ=342_890_456.7,
        pAcc=12.0,
        invalidEcef=0,
    ),
}


class SimReceiver:
    """The serial side and the reader side of one simulated receiver."""

    def __init__(self, ram: dict[str, int] | None = None) -> None:
        self.ram: dict[str, int] = dict(ram or {})
        self.valsets: list[Any] = []  # parsed CFG-VALSET messages, in order
        self.raw_writes: list[bytes] = []  # non-UBX bytes, as written
        # What MON-COMMS reports: {"protIds": [4 ids], "ports": {portId: {...}}}
        self.mon_comms: dict[str, Any] | None = None
        # Messages the receiver sends on its own (e.g. RXM-RTCM), read out
        # ahead of the next reply.
        self.unsolicited: list[Any] = []
        self.out_waiting = 0  # bytes the host still has to send
        self.nav_polls: list[bytes] = []  # NAV poll message ids, in order
        self.read_delay_s = 0.0  # how long each read waits (a slow link)
        self.writes: list[bytes] = []  # every write, in order, as written
        # When set, each write goes out a few bytes at a time (like a busy
        # UART), so two unguarded writers' bytes could interleave on ``wire``.
        self.chunked = False
        self.wire = bytearray()
        self.is_open = True
        self._replies: deque[Any] = deque()

    # ---- serial side ----
    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        if self.chunked:
            for i in range(0, len(data), 4):
                self.wire += data[i : i + 4]
                time.sleep(0.0005)
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
        elif msg_id in _NAV_REPLIES:
            self.nav_polls.append(msg_id)
            self._replies.append(_NAV_REPLIES[msg_id])
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
        if self.read_delay_s:
            time.sleep(self.read_delay_s)
        if self.unsolicited:
            return b"", self.unsolicited.pop(0)
        if self._replies:
            return b"", self._replies.popleft()
        return None, None


def connected_driver(sim: SimReceiver) -> UbloxDriver:
    """A UbloxDriver whose link is ``sim``."""
    driver = UbloxDriver()
    driver._serial = driver._guard_writes(sim)  # pyright: ignore[reportPrivateUsage]
    driver._reader = driver._watch_reader(sim)  # pyright: ignore[reportPrivateUsage]
    return driver


def rxm_rtcm(msg_type: int, used: int = 2, crc_failed: int = 0) -> SimpleNamespace:
    """A UBX-RXM-RTCM report (msgUsed: 1 = not used, 2 = used)."""
    return SimpleNamespace(
        identity="RXM-RTCM", msgType=msg_type, msgUsed=used, crcFailed=crc_failed
    )
