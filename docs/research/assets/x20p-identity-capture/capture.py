#!/usr/bin/env python3
"""Read-only X20P identity capture (research only; issue #255).

Audited standalone client. It never imports the sp-rtk-base app, and never
uses the app's Connect (which can delete BBR keys). Every outgoing frame
passes through `send()`. That function refuses anything outside a poll
allowlist and logs the exact bytes to the transmit ledger. Allowed are
zero-payload polls of MON-VER, MON-COMMS, NAV-PVT, NAV-SAT and NAV-SIG, and
CFG-VALGET (version 0, RAM layer, position 0, one key per request). No
VALSET, VALDEL, CFG-CFG, CFG-RST or correction bytes can be sent.

Usage (needs read/write access to the tty, e.g. via sudo):
  python capture.py /dev/ttyACM0 OUTDIR KEYS.json [passive_seconds]
"""
from __future__ import annotations

import json
import struct
import sys
import time
from pathlib import Path

import serial
from pyubx2 import UBXReader

ALLOW = {
    (0x0A, 0x04): "MON-VER",
    (0x0A, 0x36): "MON-COMMS",
    (0x01, 0x07): "NAV-PVT",
    (0x01, 0x35): "NAV-SAT",
    (0x01, 0x43): "NAV-SIG",
    (0x06, 0x8B): "CFG-VALGET",
}
SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}  # key-ID bits 28-30 -> value bytes


def frame(cls: int, mid: int, payload: bytes = b"") -> bytes:
    body = bytes([cls, mid]) + struct.pack("<H", len(payload)) + payload
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return b"\xb5\x62" + body + bytes([a, b])


class Session:
    def __init__(self, port: str, out: Path) -> None:
        self.out = out
        self.ser = serial.Serial(port, 38400, timeout=0.2)
        self.raw = (out / "rx-raw.ubx").open("wb")
        self.rxlog: list[dict] = []
        self.ledger: list[dict] = []
        self.t0 = time.monotonic()

    def t(self) -> float:
        return round(time.monotonic() - self.t0, 4)

    def send(self, data: bytes) -> None:
        # Audit gate: refuse anything that isn't an allowlisted poll.
        assert data[:2] == b"\xb5\x62", "not a UBX frame"
        cls, mid = data[2], data[3]
        (plen,) = struct.unpack("<H", data[4:6])
        name = ALLOW.get((cls, mid))
        assert name is not None, f"class/id {cls:#04x}/{mid:#04x} not allowlisted"
        if name == "CFG-VALGET":
            p = data[6:6 + plen]
            assert plen == 8 and p[0] == 0 and p[1] == 0 and p[2:4] == b"\0\0", "VALGET must be v0/RAM/pos0/1 key"
        else:
            assert plen == 0, f"{name} poll must have empty payload"
        self.ser.write(data)
        self.ledger.append({"t": self.t(), "msg": name, "hex": data.hex()})

    def pump(self, seconds: float, want: tuple[int, int] | None = None) -> list[tuple[bytes, object]]:
        """Read and log everything for `seconds`. Stop early once `want` arrives (plus its ACK for VALGET)."""
        got: list[tuple[bytes, object]] = []
        reader = UBXReader(self.ser, protfilter=7, quitonerror=0)
        end = time.monotonic() + seconds
        seen_want = False
        while time.monotonic() < end:
            try:
                raw, parsed = reader.read()
            except Exception as exc:  # malformed bytes: keep going, note it
                self.rxlog.append({"t": self.t(), "error": repr(exc)})
                continue
            if raw is None:
                continue
            self.raw.write(raw)
            ident = getattr(parsed, "identity", None) or ("RTCM/NMEA/other" if parsed is None else type(parsed).__name__)
            self.rxlog.append({"t": self.t(), "identity": ident, "len": len(raw), "hex": raw.hex() if len(raw) <= 600 else raw[:600].hex() + "..."})
            got.append((raw, parsed))
            if want and raw[:2] == b"\xb5\x62":
                if (raw[2], raw[3]) == want:
                    seen_want = True
                    if want != (0x06, 0x8B):
                        break
                if raw[2] == 0x05 and raw[4 + 2:].__len__() >= 2 and raw[6] == want[0] and raw[7] == want[1]:
                    break  # ACK/NAK for our request
        return got

    def close(self) -> None:
        self.raw.close()
        self.ser.close()


def decode_valget(raw: bytes) -> dict:
    p = raw[6:-2]
    out = {"version": p[0], "layer": p[1], "position": struct.unpack("<H", p[2:4])[0], "values": {}}
    i = 4
    while i + 4 <= len(p):
        (kid,) = struct.unpack("<I", p[i:i + 4]); i += 4
        n = SIZE.get((kid >> 28) & 0x7, 0)
        val = int.from_bytes(p[i:i + n], "little", signed=False); i += n
        out["values"][f"{kid:#010x}"] = val
    return out


def main() -> int:
    port, outdir, keyfile = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    passive = float(sys.argv[4]) if len(sys.argv) > 4 else 60.0
    outdir.mkdir(parents=True, exist_ok=True)
    keys: dict[str, str] = json.loads(keyfile.read_text())  # name -> hex id
    s = Session(port, outdir)
    result: dict = {"port": port, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        # 1. Passive: nothing transmitted.
        passive_msgs = s.pump(passive)
        counts: dict[str, int] = {}
        for raw, parsed in passive_msgs:
            k = getattr(parsed, "identity", None) or "unparsed"
            counts[k] = counts.get(k, 0) + 1
        result["passive"] = {"seconds": passive, "frames": len(passive_msgs), "by_identity": counts}

        # 2. Message polls.
        polls = {}
        for (cls, mid), name in ALLOW.items():
            if name == "CFG-VALGET":
                continue
            s.send(frame(cls, mid))
            got = s.pump(3.0, want=(cls, mid))
            hit = [(r, p) for r, p in got if r[:2] == b"\xb5\x62" and (r[2], r[3]) == (cls, mid)]
            polls[name] = {"answered": bool(hit), "parsed": str(hit[0][1]) if hit else None, "hex": hit[0][0].hex() if hit else None}
        result["polls"] = polls

        # 3. CFG-VALGET, RAM, one key per request.
        cfg = {}
        for name, hexid in keys.items():
            kid = int(hexid, 16)
            s.send(frame(0x06, 0x8B, bytes([0, 0, 0, 0]) + struct.pack("<I", kid)))
            got = s.pump(2.0, want=(0x06, 0x8B))
            resp = [r for r, _ in got if r[:4] == b"\xb5\x62\x06\x8b"]
            ack = [r for r, _ in got if r[:3] == b"\xb5\x62\x05" and len(r) >= 10 and r[6:8] == b"\x06\x8b"]
            status = "ack" if ack and ack[-1][3] == 0x01 else "nak" if ack else "timeout"
            entry = {"id": hexid, "status": status}
            if resp:
                d = decode_valget(resp[-1])
                entry["value"] = d["values"].get(f"{kid:#010x}")
            cfg[name] = entry
        result["valget_ram"] = cfg
    finally:
        s.close()
        result["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (outdir / "capture.json").write_text(json.dumps(result, indent=1, default=str) + "\n")
        (outdir / "rx-log.json").write_text(json.dumps(s.rxlog, indent=0) + "\n")
        (outdir / "tx-ledger.json").write_text(json.dumps(s.ledger, indent=1) + "\n")
    # Ledger self-check: every sent frame is an allowlisted poll.
    sent = {e["msg"] for e in s.ledger}
    assert sent <= set(ALLOW.values()), sent
    print(json.dumps({"passive": result["passive"], "polls": {k: v["answered"] for k, v in result["polls"].items()},
                      "valget": {st: sum(1 for e in result["valget_ram"].values() if e["status"] == st) for st in ("ack", "nak", "timeout")},
                      "tx_frames": len(s.ledger), "tx_types": sorted(sent)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
