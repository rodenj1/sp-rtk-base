"""Bench probe for map #150's hardware go/no-go (issue #153).

Answers one question on a real receiver: can UBX-MON-COMMS tell us which
of the receiver's ports this serial link is attached to?

Method (map #150 decision 2, as amended by the research ticket #151):
poll MON-COMMS, write N bytes of well-formed UBX polls, poll MON-COMMS
again. The console's port is the one Table 27 port whose ``rxBytes`` advanced by *at
least* N and whose UBX message count advanced by at least the frames sent — never "exactly N", because the MON-COMMS polls are themselves
received on that port and their ordering against the snapshot is
undocumented.

Traps this script deliberately handles (from ``docs/research/
mon-comms-port-identification.md`` on ``research/mon-comms``):

- UART2 is ``0x0201``, not ``0x0200``. An unrecognised ``portId`` is
  reported as UNRECOGNISED — a decode bug, not a hardware "no".
- ``txErrors.outputPort`` is a protVer 27.50 field that pyubx2 decodes
  anyway. It is printed for the record and **never used**.
- Ports are keyed on ``portId``, never on their position in the group.

Usage — ``--port`` has no default on purpose: this opens whatever it is
given, and the first enumerated device is not necessarily a receiver::

    uv run python tools/probe_console_port.py --port /dev/ttyUSB0 --baud 57600
    uv run python tools/probe_console_port.py --port /dev/ttyACM0 --baud 57600 --runs 5

Makes no configuration writes and leaves no state on the receiver.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass

#: Integration Manual UBX-18010802 R16 §3.8 Table 27, vendor-confirmed
#: against ubxlib (``port << 8``, ``+1`` for UART2).
PORT_NAMES: dict[int, str] = {
    0x0000: "I2C",
    0x0100: "UART1",
    0x0201: "UART2",
    0x0300: "USB",
    0x0400: "SPI",
}

#: A MON-COMMS or NAV-STATUS poll is an 8-byte frame.
_POLL_FRAME_BYTES = 8


@dataclass(frozen=True)
class PortCounters:
    port_id: int
    rx_bytes: int
    tx_bytes: int
    ubx_msgs: int  # msgs[0] is the UBX protocol slot per protIds


@dataclass(frozen=True)
class Attribution:
    verdict: str  # IDENTIFIED | AMBIGUOUS | NONE_ADVANCED | UNRECOGNISED
    port_id: int | None
    port_name: str | None
    rx_deltas: dict[str, int]
    detail: str


def port_name(port_id: int) -> str:
    return PORT_NAMES.get(port_id, f"UNRECOGNISED(0x{port_id:04x})")


def attribute(
    before: dict[int, PortCounters],
    after: dict[int, PortCounters],
    bytes_written: int,
    frames_written: int,
) -> Attribution:
    """Decide which port received the probe. Pure, so it is testable
    without a receiver.

    Amended on the bench (issue #153): ``rxBytes >= N`` alone is not
    enough. HPG 1.51 reports two ports Table 27 calls *Reserved*
    (``0x0101``, ``0x0200``) and both carry heavy internal traffic, so
    they clear any byte threshold on their own. The rule is therefore:

    - only Table 27 ports are candidates, and
    - a candidate must show **both** ``rxBytes >= N`` **and** a UBX
      message delta ``>= frames written`` — the fingerprint that proved
      exact and repeatable on hardware.

    Keyed on ``portId`` throughout; deltas taken modulo 2**32 (U4).
    """
    rx: dict[int, int] = {}
    ubx: dict[int, int] = {}
    for pid, b in before.items():
        a = after.get(pid)
        if a is None:
            continue
        rx[pid] = (a.rx_bytes - b.rx_bytes) % (2**32)
        ubx[pid] = (a.ubx_msgs - b.ubx_msgs) % (2**16)

    named = {port_name(pid): d for pid, d in sorted(rx.items())}

    def fingerprinted(pid: int) -> bool:
        return rx[pid] >= bytes_written and ubx[pid] >= frames_written

    known = [pid for pid in rx if pid in PORT_NAMES and fingerprinted(pid)]
    unknown = [pid for pid in rx if pid not in PORT_NAMES and fingerprinted(pid)]

    if len(known) == 1:
        pid = known[0]
        return Attribution("IDENTIFIED", pid, PORT_NAMES[pid], named,
                           f"exactly one Table 27 port carries the probe fingerprint "
                           f"(rx >= {bytes_written}, ubx >= {frames_written})")
    if len(known) > 1:
        return Attribution("AMBIGUOUS", None, None, named,
                           f"{len(known)} Table 27 ports carry the fingerprint")
    if len(unknown) == 1:
        pid = unknown[0]
        return Attribution("UNRECOGNISED", pid, port_name(pid), named,
                           "only an unrecognised portId carries the fingerprint — a "
                           "decode problem, NOT a hardware no")
    return Attribution("NONE_ADVANCED", None, None, named,
                       "no port carries the probe fingerprint")


# ---------------------------------------------------------------------------
# Hardware I/O — only below this line touches a serial port
# ---------------------------------------------------------------------------


def _counters(msg: object) -> dict[int, PortCounters]:
    n = int(getattr(msg, "nPorts"))
    out: dict[int, PortCounters] = {}
    for i in range(1, n + 1):
        s = f"{i:02d}"
        pid = int(getattr(msg, f"portId_{s}"))
        out[pid] = PortCounters(
            port_id=pid,
            rx_bytes=int(getattr(msg, f"rxBytes_{s}")),
            tx_bytes=int(getattr(msg, f"txBytes_{s}")),
            ubx_msgs=int(getattr(msg, f"msgs_{s}_01", 0)),
        )
    return out


def _poll(ser: object, reader: object, identity: str, timeout: float) -> object:
    from pyubx2 import POLL, UBXMessage

    cls, mid = identity.split("-")
    ser.reset_input_buffer()  # type: ignore[attr-defined]
    ser.write(UBXMessage(cls, identity, POLL).serialize())  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _, parsed = reader.read()  # type: ignore[attr-defined]
        except Exception:
            continue
        if parsed is not None and getattr(parsed, "identity", "") == identity:
            return parsed
    raise TimeoutError(f"no {identity} reply within {timeout}s")


def run(port: str, baud: int, probe_frames: int, runs: int, timeout: float) -> dict[str, object]:
    import serial  # type: ignore[import-untyped]
    from pyubx2 import POLL, UBXMessage, UBXReader

    report: dict[str, object] = {"port": port, "baud": baud, "runs": []}
    with serial.Serial(port, baud, timeout=0.5) as ser:
        reader = UBXReader(ser, protfilter=7, quitonerror=0)

        ver = _poll(ser, reader, "MON-VER", timeout)
        exts = [str(getattr(ver, f"extension_{i:02d}", "") or "") for i in range(1, 12)]
        report["mon_ver"] = [e.strip("\x00 ") for e in exts if e]

        probe = UBXMessage("NAV", "NAV-STATUS", POLL).serialize() * probe_frames
        written = len(probe)

        for r in range(runs):
            t0 = time.monotonic()
            before_msg = _poll(ser, reader, "MON-COMMS", timeout)
            ser.write(probe)
            ser.flush()
            time.sleep(0.2)  # let the receiver consume the probe
            after_msg = _poll(ser, reader, "MON-COMMS", timeout)
            elapsed = time.monotonic() - t0

            before, after = _counters(before_msg), _counters(after_msg)
            att = attribute(before, after, written, probe_frames)
            report["runs"].append({  # type: ignore[union-attr]
                "run": r + 1,
                "elapsed_s": round(elapsed, 3),
                "bytes_written": written,
                "nPorts": int(getattr(after_msg, "nPorts")),
                "ports_seen": [port_name(p) for p in sorted(after)],
                "ubx_msg_deltas": {port_name(p): after[p].ubx_msgs - before[p].ubx_msgs
                                   for p in after if p in before},
                "attribution": asdict(att),
                # Recorded, never used — protVer 27.50 field (research #151).
                "outputPort_DO_NOT_TRUST": int(getattr(after_msg, "outputPort", -1)),
            })
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", required=True, help="serial device (no default, on purpose)")
    ap.add_argument("--baud", type=int, default=57600)
    ap.add_argument("--probe-frames", type=int, default=16,
                    help=f"NAV-STATUS polls to send ({_POLL_FRAME_BYTES} bytes each)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=3.0)
    args = ap.parse_args()

    try:
        report = run(args.port, args.baud, args.probe_frames, args.runs, args.timeout)
    except Exception as exc:  # report, don't traceback, at the bench
        print(json.dumps({"port": args.port, "baud": args.baud, "error": repr(exc)}, indent=2))
        return 2

    print(json.dumps(report, indent=2))
    verdicts = {r["attribution"]["verdict"] for r in report["runs"]}  # type: ignore[index,union-attr]
    names = {r["attribution"]["port_name"] for r in report["runs"]}  # type: ignore[index,union-attr]
    print(f"\nSUMMARY: verdicts={sorted(verdicts)} port={sorted(n for n in names if n)}",
          file=sys.stderr)
    return 0 if verdicts == {"IDENTIFIED"} and len(names) == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
