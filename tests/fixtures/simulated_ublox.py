"""A simulated u-blox receiver behind a fake serial port, for driver tests.

It answers the UBX the ``UbloxDriver`` sends, and models the part of a
ZED-F9P that the driver's persistence decisions depend on: the four
configuration layers (sp-rtk-base#221). The rules follow the F9 HPG 1.51
Interface Description (``docs/research/ubx-config-layers-and-tmode-
persistence.md`` on the ``research/ubx-config-layers-tmode`` branch):

- CFG-VALSET takes a layer *bitmask* (1 RAM, 2 BBR, 4 Flash).
- CFG-VALGET reads one layer by *number* (0 RAM, 1 BBR, 2 Flash, 7 Default);
  a key not stored in BBR or Flash is simply absent from the answer.
- CFG-VALDEL deletes keys from BBR (2) and/or Flash (4).
- A reset rebuilds RAM key by key with priority BBR > Flash > Default.
- CFG-CFG (protocol > 23.01): any mask bit means everything; ``save``
  copies RAM to the selected devices, ``load`` rebuilds RAM. A present but
  all-zero ``deviceMask`` is undefined in the spec and modelled as saving
  nowhere, which is what the bench suggests.
- ``NAV-SVIN.dur`` grows while RAM ``CFG_TMODE_MODE`` is 1 and is cleared
  only by a hardware reset (``resetMode`` 0x00), as the driver's comments
  record from the bench.
- ``MON-COMMS`` answers only when the test says which port the host is
  attached to (``console_port_id``); that port counts every UBX message
  it receives, so the driver can identify the Console port (ADR 0003).

Not modelled: timing, the TMODE edge-trigger, other message classes.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any

from pyubx2 import GET, UBX_CONFIG_DATABASE, UBXMessage

RAM, BBR, FLASH, DEFAULT = "ram", "bbr", "flash", "default"
_VALGET_LAYER = {0: RAM, 1: BBR, 2: FLASH, 7: DEFAULT}
_KEY_BY_ID = {kid: (name, typ) for name, (kid, typ) in UBX_CONFIG_DATABASE.items()}

#: MON-COMMS ``portId``s (Integration Manual Table 27): UART1, UART2, USB.
UART1_PORT_ID, UART2_PORT_ID, USB_PORT_ID = 0x0100, 0x0201, 0x0300

#: Defaults for the keys the tests touch; every other known key defaults to 0.
DEFAULTS: dict[str, int] = {"CFG_RATE_MEAS": 1000, "CFG_UART1_BAUDRATE": 38400}


def _size(typ: str) -> int:
    return int(typ[1:])


def _fmt(typ: str) -> str:
    size = _size(typ)
    if typ[0] == "R":
        return "<f" if size == 4 else "<d"
    code = {1: "b", 2: "h", 4: "i", 8: "q"}[size]
    return "<" + (code if typ[0] == "I" else code.upper())


def _frame(cls: int, mid: int, payload: bytes) -> bytes:
    body = bytes([cls, mid]) + struct.pack("<H", len(payload)) + payload
    a = b = 0
    for byte in body:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return b"\xb5\x62" + body + bytes([a, b])


class SimulatedUblox:
    """The receiver: its layers survive the serial port being reopened."""

    def __init__(self) -> None:
        self.layers: dict[str, dict[str, int]] = {RAM: {}, BBR: {}, FLASH: {}}
        self.svin_dur = 0
        self.resets: list[int] = []
        #: When set, the survey-in engine never starts (``dur`` stays put).
        self.survey_engine_stalls = False
        #: When set, writes ACK but never reach Flash (a failing flash).
        self.flash_ignores_writes = False
        #: The MON-COMMS ``portId`` the host is attached to. ``None``: the
        #: receiver NAKs MON-COMMS, so the Console port stays unknown.
        self.console_port_id: int | None = None
        #: Called before each MON-COMMS answer, e.g. to hold it back.
        self.before_mon_comms: Callable[[], None] | None = None
        #: Per port: (bytes received, UBX messages received).
        self._rx_counts: dict[int, tuple[int, int]] = {}
        self._rebuild_ram()

    # -- the test's view -------------------------------------------------

    def value(self, key: str, layer: str = RAM) -> int | None:
        """``key`` as stored in ``layer``; ``None`` if BBR/Flash lacks it."""
        if layer == DEFAULT:
            return DEFAULTS.get(key, 0)
        return self.layers[layer].get(key)

    def store(self, layer: str, **values: int) -> None:
        """Put values straight into a layer, as an earlier session left them."""
        self.layers[layer].update(values)

    def power_cycle(self, *, backup_battery: bool = True) -> None:
        """Lose RAM (and BBR without V_BCKP), then rebuild RAM."""
        if not backup_battery:
            self.layers[BBR].clear()
        self.svin_dur = 0
        self._rebuild_ram()

    # -- the receiver's own behaviour ------------------------------------

    def _rebuild_ram(self) -> None:
        keys = set(self.layers[BBR]) | set(self.layers[FLASH]) | set(DEFAULTS)
        ram = {k: DEFAULTS.get(k, 0) for k in keys}
        ram.update(self.layers[FLASH])
        ram.update(self.layers[BBR])
        self.layers[RAM] = ram

    def _ram(self, key: str) -> int:
        return self.layers[RAM].get(key, DEFAULTS.get(key, 0))

    def handle(self, cls: int, mid: int, payload: bytes) -> bytes:
        """Answer one complete UBX message from the host."""
        if self.console_port_id is not None:
            rx_bytes, ubx_msgs = self._rx_counts.get(self.console_port_id, (0, 0))
            self._rx_counts[self.console_port_id] = (
                rx_bytes + len(payload) + 8,
                ubx_msgs + 1,
            )
        if (cls, mid) == (0x0A, 0x36) and not payload:
            if self.before_mon_comms is not None:
                self.before_mon_comms()
            return self._mon_comms()
        if (cls, mid) == (0x06, 0x8A):
            return self._valset(payload)
        if (cls, mid) == (0x06, 0x8B):
            return self._valget(payload)
        if (cls, mid) == (0x06, 0x8C):
            return self._valdel(payload)
        if (cls, mid) == (0x06, 0x09):
            return self._cfg_cfg(payload)
        if (cls, mid) == (0x06, 0x04):
            reset_mode = payload[2]
            self.resets.append(reset_mode)
            if reset_mode == 0x00:
                self.svin_dur = 0
            if reset_mode in (0x00, 0x01, 0x04):
                self._rebuild_ram()
            return b""  # CFG-RST is never acknowledged
        if (cls, mid) == (0x0A, 0x04) and not payload:
            return self._mon_ver()
        if (cls, mid) == (0x01, 0x3B) and not payload:
            return self._nav_svin()
        return _frame(0x05, 0x00, bytes([cls, mid]))  # NAK anything else

    @staticmethod
    def _ack(cls: int, mid: int) -> bytes:
        return _frame(0x05, 0x01, bytes([cls, mid]))

    def _valset(self, payload: bytes) -> bytes:
        layers_mask = payload[1]
        values: dict[str, int] = {}
        pos = 4
        while pos < len(payload):
            (key_id,) = struct.unpack_from("<I", payload, pos)
            name, typ = _KEY_BY_ID[key_id]
            (val,) = struct.unpack_from(_fmt(typ), payload, pos + 4)
            values[name] = int(val)
            pos += 4 + _size(typ)
        for bit, layer in ((1, RAM), (2, BBR), (4, FLASH)):
            if layers_mask & bit and not (layer == FLASH and self.flash_ignores_writes):
                self.layers[layer].update(values)
        return self._ack(0x06, 0x8A)

    def _valget(self, payload: bytes) -> bytes:
        layer_num = payload[1]
        layer = _VALGET_LAYER[layer_num]
        out = bytearray([1, layer_num, 0, 0])
        found = False
        for pos in range(4, len(payload), 4):
            (key_id,) = struct.unpack_from("<I", payload, pos)
            name, typ = _KEY_BY_ID[key_id]
            val = self._ram(name) if layer == RAM else self.value(name, layer)
            if val is None:
                continue
            found = True
            out += struct.pack("<I", key_id) + struct.pack(_fmt(typ), val)
        if not found:
            return _frame(0x05, 0x00, b"\x06\x8b")  # nothing stored: NAK
        return _frame(0x06, 0x8B, bytes(out))

    def _valdel(self, payload: bytes) -> bytes:
        layers_mask = payload[1]
        for pos in range(4, len(payload), 4):
            (key_id,) = struct.unpack_from("<I", payload, pos)
            name, _ = _KEY_BY_ID[key_id]
            for bit, layer in ((2, BBR), (4, FLASH)):
                if layers_mask & bit:
                    self.layers[layer].pop(name, None)
        return self._ack(0x06, 0x8C)

    def _cfg_cfg(self, payload: bytes) -> bytes:
        clear, save, load = struct.unpack_from("<III", payload, 0)
        if len(payload) > 12:
            dev = payload[12]
            targets = [layer for bit, layer in ((1, BBR), (2, FLASH)) if dev & bit]
        else:
            targets = [BBR, FLASH]
        for layer in targets:
            if clear:
                self.layers[layer].clear()
            if save:
                self.layers[layer].update(self.layers[RAM])
        if load:
            self._rebuild_ram()
        return self._ack(0x06, 0x09)

    @staticmethod
    def _mon_ver() -> bytes:
        def pad(text: str, size: int) -> bytes:
            return text.encode().ljust(size, b"\x00")

        payload = pad("EXT CORE 1.00 (9e1716)", 30) + pad("00190000", 10)
        for ext in ("FWVER=HPG 1.51", "PROTVER=27.50", "MOD=ZED-F9P"):
            payload += pad(ext, 30)
        return _frame(0x0A, 0x04, payload)

    def _mon_comms(self) -> bytes:
        if self.console_port_id is None:
            return _frame(0x05, 0x00, b"\x0a\x36")
        fields: dict[str, int] = {"nPorts": 3, "protIds_01": 0, "protIds_02": 1}
        fields |= {"protIds_03": 5, "protIds_04": 0xFF}
        for n, port_id in enumerate((UART1_PORT_ID, UART2_PORT_ID, USB_PORT_ID), 1):
            rx_bytes, ubx_msgs = self._rx_counts.get(port_id, (0, 0))
            fields |= {
                f"portId_{n:02d}": port_id,
                f"rxBytes_{n:02d}": rx_bytes,
                f"msgs_{n:02d}_01": ubx_msgs,
            }
        return UBXMessage("MON", "MON-COMMS", GET, **fields).serialize()  # type: ignore[no-any-return]

    def _nav_svin(self) -> bytes:
        active = self._ram("CFG_TMODE_MODE") == 1
        if active and not self.survey_engine_stalls:
            self.svin_dur += 1
        payload = struct.pack(
            "<B3xIIiiibbbxIIBB2x",
            0,
            0,
            self.svin_dur,
            0,
            0,
            0,
            0,
            0,
            0,
            50000,
            self.svin_dur,
            0,
            int(active),
        )
        return _frame(0x01, 0x3B, payload)


class SimulatedSerial:
    """The ``serial.Serial`` the driver opens; a new one after each reset."""

    def __init__(self, receiver: SimulatedUblox, **_: Any) -> None:
        self.receiver = receiver
        self.is_open = True
        self._rx = bytearray()
        self._tx = bytearray()

    def write(self, data: bytes) -> int:
        self._tx += data
        while True:
            start = self._tx.find(b"\xb5\x62")
            if start < 0 or len(self._tx) < start + 6:
                break
            (length,) = struct.unpack_from("<H", self._tx, start + 4)
            end = start + 6 + length + 2
            if len(self._tx) < end:
                break
            cls, mid = self._tx[start + 2], self._tx[start + 3]
            payload = bytes(self._tx[start + 6 : start + 6 + length])
            del self._tx[:end]
            self._rx += self.receiver.handle(cls, mid, payload)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        out = bytes(self._rx[:size])
        del self._rx[:size]
        return out

    def readline(self) -> bytes:
        return self.read(len(self._rx))

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    @property
    def out_waiting(self) -> int:
        return 0

    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        self.is_open = False
