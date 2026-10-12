#!/usr/bin/env python3
"""Offline parse/serialize checks for RXM-COR, NAV-SIG, NAV-SAT and NAV-PVT.

Research-only. Run with each scratch interpreter; it imports only pyubx2 and
the standard library, opens no port and contacts no receiver. Every frame is
**synthetic**: built here from the field tables that ``manual_layouts.py``
extracted from the printed u-blox X20 interface descriptions, never captured.

For each case it (1) encodes an independent frame from the manual's offsets,
types and bit ranges, (2) parses it with pyubx2 (checksum validated), (3)
compares every documented, non-reserved field with the raw value (scaled
where the manual declares a scale), (4) builds the same message with
pyubx2's own serializer and compares bytes. It also walks every bitfield at
its maximum value, sets reserved bits, and runs a few deliberate negatives.

Usage:
    <venv>/bin/python offline_parse.py <label>     # writes <label>-results.json
"""

from __future__ import annotations

import json
import math
import platform
import struct
import sys
import traceback
from pathlib import Path
from typing import Any

import pyubx2
from pyubx2 import GET, UBXMessage, UBXReader
from pyubx2.ubxtypes_core import VALCKSUM

HERE = Path(__file__).parent
LAYOUTS = json.loads((HERE / "manual-layouts.json").read_text())["messages"]
VERSIONS = ("2.00", "2.02", "2.10", "2.11")
CLASS_ID = {"RXM-COR": (0x02, 0x34), "NAV-PVT": (0x01, 0x07),
            "NAV-SAT": (0x01, 0x35), "NAV-SIG": (0x01, 0x43)}
GROUP_COUNT = {"NAV-SAT": "numSvs", "NAV-SIG": "numSigs"}
# Manual spelling -> pyubx2 attribute name, where they differ.
RENAME = {"gnssFixOK": "gnssFixOk", "sec": "second"}
# Manual scale factors (printed "Scale" column) for fields exercised here.
SCALE = {("RXM-COR", "ebno"): 0.125, ("NAV-SIG", "prRes"): 0.1, ("NAV-SAT", "prRes"): 0.1,
         ("NAV-PVT", "lon"): 1e-7, ("NAV-PVT", "lat"): 1e-7, ("NAV-PVT", "headMot"): 1e-5,
         ("NAV-PVT", "headAcc"): 1e-5, ("NAV-PVT", "pDOP"): 0.01, ("NAV-PVT", "headVeh"): 1e-5,
         ("NAV-PVT", "magDec"): 0.01, ("NAV-PVT", "magAcc"): 0.01}
FMT = {"U1": "B", "U2": "H", "U4": "I", "I1": "b", "I2": "h", "I4": "i",
       "X1": "B", "X2": "H", "X4": "I"}


def _is_reserved(name: str) -> bool:
    return name.startswith("reserved")


def _layout(message: str, version: str) -> tuple[list[dict], list[dict], list[dict], int]:
    info = LAYOUTS[message][version]
    top = [f for f in info["fields"] if "n·" not in f["offset"]]
    grp = [f for f in info["fields"] if "n·" in f["offset"]]
    stride = int(grp[0]["offset"].split("n·")[1]) if grp else 0
    return top, grp, info["bits"], stride


def _size(typ: str) -> int:
    if "[" in typ:
        return int(typ.split("[")[1].rstrip("]")) * _size(typ.split("[")[0])
    return struct.calcsize("<" + FMT[typ])


def _pack_field(buf: bytearray, offset: int, field: dict, values: dict, bits: list[dict]) -> None:
    name, typ = field["name"], field["type"]
    if "[" in typ:
        raw = values.get(name, 0)
        buf[offset:offset + _size(typ)] = (raw if isinstance(raw, bytes) else bytes(_size(typ)))
        return
    sub = [b for b in bits if b["parent"] == name]
    if sub:  # a bitfield: assemble it from its documented members
        word = values.get("__raw_" + name, 0)
        for b in sub:
            v = values.get(b["name"], 0)
            if v >> b["width"]:
                raise ValueError(f"{b['name']}={v} exceeds {b['width']} bits")
            word |= v << b["lsb"]
        value = word
    else:
        value = values.get(name, 0)
    struct.pack_into("<" + FMT[typ], buf, offset, value)


def encode(message: str, version: str, values: dict, group: list[dict] | None = None) -> bytes:
    """An independent UBX frame from the manual's table (no pyubx2 involved)."""
    top, grp, bits, stride = _layout(message, version)
    group = group or []
    if message in GROUP_COUNT:
        values = {**values, GROUP_COUNT[message]: len(group)}
    base = max(int(f["offset"]) + _size(f["type"]) for f in top)
    buf = bytearray(base + stride * len(group))
    for f in top:
        _pack_field(buf, int(f["offset"]), f, values, bits)
    for n, item in enumerate(group):
        for f in grp:
            start = int(f["offset"].split(" + ")[0]) + n * stride
            _pack_field(buf, start, f, item, bits)
    cls, mid = CLASS_ID[message]
    body = bytes([cls, mid]) + struct.pack("<H", len(buf)) + bytes(buf)
    ck_a = ck_b = 0
    for byte in body:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return b"\xb5\x62" + body + bytes([ck_a, ck_b])


def _documented(message: str, version: str) -> tuple[list[str], list[str]]:
    """Documented non-reserved scalar names (top, group), bitfield members
    replacing their container."""
    top, grp, bits, _ = _layout(message, version)
    parents = {b["parent"] for b in bits}

    def names(fields: list[dict]) -> list[str]:
        out: list[str] = []
        for f in fields:
            if _is_reserved(f["name"]):
                continue
            if f["name"] in parents:
                out += [b["name"] for b in bits if b["parent"] == f["name"] and not _is_reserved(b["name"])]
            else:
                out.append(f["name"])
        return out

    return names(top), names(grp)


def expected_attrs(message: str, version: str, values: dict, group: list[dict]) -> dict[str, float]:
    top, grp = _documented(message, version)
    if message in GROUP_COUNT:
        values = {**values, GROUP_COUNT[message]: len(group)}
    out: dict[str, float] = {}
    for name in top:
        out[RENAME.get(name, name)] = values.get(name, 0) * SCALE.get((message, name), 1)
    for n, item in enumerate(group, start=1):
        for name in grp:
            out[f"{RENAME.get(name, name)}_{n:02d}"] = item.get(name, 0) * SCALE.get((message, name), 1)
    return out


def _close(a: Any, b: float) -> bool:
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def run_case(case: dict) -> dict:
    message, version = case["message"], case.get("version", "2.11")
    values, group = case.get("values", {}), case.get("group", [])
    record: dict[str, Any] = {"id": case["id"], "message": message, "layout_version": version,
                              "synthetic": True, "purpose": case.get("purpose", "")}
    try:
        frame = encode(message, version, values, group)
        record["frame_hex"] = frame.hex()
        # The same values must encode identically under every manual revision
        # whose table is identical (layout stability across firmware).
        record["encodes_identically_in"] = [v for v in VERSIONS if encode(message, v, values, group) == frame]
        parsed = UBXReader.parse(frame, msgmode=GET, validate=VALCKSUM)
        record["identity"] = parsed.identity
        want = expected_attrs(message, version, values, group)
        mismatches = {k: [getattr(parsed, k, "<absent>"), v] for k, v in want.items()
                      if not _close(getattr(parsed, k, None), v)}
        record["fields_checked"] = len(want)
        record["parse_mismatches"] = mismatches
        record["decoded"] = {k: getattr(parsed, k, "<absent>") for k in case.get("show", [])}
        record["has_crcFailed_attr"] = hasattr(parsed, "crcFailed")
        # pyubx2's own serializer, from the documented names.
        kwargs = {k: v for k, v in want.items()}
        built = UBXMessage(*parsed.identity.split("-", 1)[:1], parsed.identity, GET, **kwargs).serialize()
        record["serializer_equal"] = built == frame
        if built != frame:
            record["serializer_hex"] = built.hex()
        record["ok"] = not mismatches and (record["serializer_equal"]
                                           or not case.get("serializer_must_match", True))
    except Exception as exc:  # recorded, never hidden
        record["ok"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc(limit=3)
    if case.get("expect_error"):
        record["expected_error"] = case["expect_error"]
        record["ok"] = "error" in record and case["expect_error"] in record["error"]
    return record


def walking_bits() -> list[dict]:
    """Every documented bitfield at its maximum value, alone."""
    cases: list[dict] = []
    for message in CLASS_ID:
        for version in VERSIONS:
            _, grp, bits, _ = _layout(message, version)
            grp_names = {f["name"] for f in grp}
            for b in bits:
                if _is_reserved(b["name"]):
                    continue
                v = (1 << b["width"]) - 1
                in_group = b["parent"] in grp_names
                case = {"id": f"walk:{message}:{version}:{b['parent']}.{b['name']}",
                        "message": message, "version": version,
                        "purpose": f"bit {b['lsb']}..{b['lsb'] + b['width'] - 1} isolation"}
                if message == "RXM-COR":
                    case["values"] = {"version": 1, b["name"]: v}
                elif in_group:
                    case["values"], case["group"] = {"version": 1 if message == "NAV-SAT" else 0}, [{b["name"]: v}]
                else:
                    case["values"] = {b["name"]: v}
                cases.append(case)
    return cases


def reserved_bits() -> list[dict]:
    """Reserved/undocumented bits set: documented members must stay zero."""
    out = []
    for message, version in ((m, v) for m in CLASS_ID for v in VERSIONS):
        top, grp, bits, _ = _layout(message, version)
        for f in top + grp:
            sub = [b for b in bits if b["parent"] == f["name"]]
            if not sub:
                continue
            used = 0
            for b in sub:
                used |= ((1 << b["width"]) - 1) << b["lsb"]
            spare = ((1 << (8 * _size(f["type"]))) - 1) & ~used
            if not spare:
                continue
            item = {"__raw_" + f["name"]: spare}
            case = {"id": f"reserved:{message}:{version}:{f['name']}:0x{spare:x}", "message": message,
                    "version": version, "serializer_must_match": False,
                    "purpose": "undocumented bits must not leak into documented members"}
            if f in grp:
                case["values"], case["group"] = {"version": 1 if message == "NAV-SAT" else 0}, [item]
            else:
                case["values"] = {"version": 1, **item} if message == "RXM-COR" else item
            out.append(case)
    return out


def scenario_cases() -> list[dict]:
    show_cor = ["protocol", "errStatus", "msgUsed", "correctionId", "msgTypeValid", "msgSubTypeValid",
                "msgInputHandle", "msgEncrypted", "msgDecrypted", "msgType", "msgSubType", "ebno"]
    cor = lambda **kw: {"version": 1, **kw}  # noqa: E731
    cases = [
        {"id": "cor:rtcm3-1077-used", "message": "RXM-COR", "show": show_cor,
         "values": cor(protocol=1, errStatus=1, msgUsed=2, correctionId=42, msgTypeValid=1, msgInputHandle=1,
                       msgEncrypted=1, msgDecrypted=1, msgType=1077)},
        {"id": "cor:rtcm3-1005-not-used-station-4095", "message": "RXM-COR", "show": show_cor,
         "values": cor(protocol=1, errStatus=1, msgUsed=1, correctionId=4095, msgTypeValid=1, msgInputHandle=1,
                       msgType=1005)},
        {"id": "cor:rtcm3-status-unknown", "message": "RXM-COR", "show": show_cor,
         "values": cor(protocol=1, errStatus=0, msgUsed=0, correctionId=42, msgTypeValid=1, msgType=1127)},
        {"id": "cor:rtcm3-erroneous", "message": "RXM-COR", "show": show_cor,
         "values": cor(protocol=1, errStatus=2, msgUsed=1, correctionId=42, msgTypeValid=1, msgInputHandle=1,
                       msgType=1097)},
        {"id": "cor:rtcm3-no-df003-0xffff", "message": "RXM-COR", "show": show_cor,
         "values": cor(protocol=1, errStatus=1, msgUsed=1, correctionId=0xFFFF, msgTypeValid=1, msgType=1013)},
        {"id": "cor:rtcm3-4072-subtype0", "message": "RXM-COR", "show": show_cor,
         "values": cor(protocol=1, errStatus=1, msgUsed=2, correctionId=7, msgTypeValid=1, msgSubTypeValid=1,
                       msgInputHandle=1, msgType=4072, msgSubType=0)},
        {"id": "cor:rtcm3-unsupported-no-handler", "message": "RXM-COR", "show": show_cor,
         "values": cor(protocol=1, errStatus=1, msgUsed=1, correctionId=42, msgTypeValid=1, msgInputHandle=0,
                       msgType=1019)},
        {"id": "cor:spartn-encrypted-decrypted", "message": "RXM-COR", "show": show_cor,
         "values": cor(protocol=2, errStatus=1, msgUsed=2, correctionId=0xFFFF, msgTypeValid=1, msgSubTypeValid=1,
                       msgInputHandle=1, msgEncrypted=2, msgDecrypted=2, msgType=1, msgSubType=2)},
        {"id": "cor:has-2.10plus", "message": "RXM-COR", "version": "2.10", "show": show_cor,
         "values": cor(protocol=5, errStatus=1, msgUsed=2, correctionId=0xFFFF, msgTypeValid=0, msgInputHandle=1)},
        {"id": "cor:pmp-ebno", "message": "RXM-COR", "show": show_cor,
         "values": cor(ebno=80, protocol=29, errStatus=1, msgUsed=2, correctionId=0xFFFF, msgInputHandle=1)},
        {"id": "cor:qzssl6", "message": "RXM-COR", "show": show_cor,
         "values": cor(protocol=30, errStatus=1, msgUsed=1, correctionId=0xFFFF, msgInputHandle=1)},
        {"id": "cor:protocol-undocumented-7", "message": "RXM-COR", "show": show_cor,
         "values": cor(protocol=7, errStatus=1, msgUsed=2, correctionId=0xFFFF)},
        {"id": "cor:msgUsed-reserved-3", "message": "RXM-COR", "show": show_cor,
         "values": cor(protocol=1, errStatus=3, msgUsed=3, correctionId=42, msgTypeValid=1, msgType=1077)},
        {"id": "cor:version-2-unknown-layout", "message": "RXM-COR", "show": ["version", "msgUsed"],
         "values": {"version": 2, "protocol": 1, "msgUsed": 2, "msgType": 1077, "msgTypeValid": 1}},
        # NAV-SIG: OSR RTCM3 carrier+code corrections, HAS, SPARTN, none, gap value 8.
        {"id": "sig:mixed-sources", "message": "NAV-SIG",
         "show": ["numSigs", "corrSource_01", "crCorrUsed_01", "corrSource_02", "corrSource_05", "authStatus_04"],
         "values": {"iTOW": 345600000, "version": 0},
         "group": [
             {"gnssId": 0, "svId": 5, "sigId": 0, "cno": 45, "qualityInd": 7, "corrSource": 4, "ionoModel": 8,
              "health": 1, "prUsed": 1, "crUsed": 1, "doUsed": 1, "prCorrUsed": 1, "crCorrUsed": 1, "prRes": -12},
             {"gnssId": 2, "svId": 11, "sigId": 8, "cno": 40, "qualityInd": 7, "corrSource": 12, "ionoModel": 9,
              "health": 1, "prUsed": 1, "crUsed": 1, "prCorrUsed": 1, "crCorrUsed": 1},
             {"gnssId": 3, "svId": 30, "sigId": 7, "cno": 38, "qualityInd": 5, "corrSource": 7, "health": 1,
              "prUsed": 1, "prCorrUsed": 1},
             {"gnssId": 2, "svId": 12, "sigId": 0, "cno": 41, "qualityInd": 7, "corrSource": 0, "health": 1,
              "prUsed": 1, "crUsed": 1, "authStatus": 1},
             {"gnssId": 0, "svId": 6, "sigId": 3, "cno": 30, "qualityInd": 4, "corrSource": 8, "health": 0}]},
        {"id": "sig:zero-signals", "message": "NAV-SIG", "show": ["numSigs"],
         "values": {"iTOW": 1000, "version": 0}, "group": []},
        # NAV-SAT: RTCM used vs available-only vs HAS/SPARTN.
        {"id": "sat:correction-flags", "message": "NAV-SAT",
         "show": ["numSvs", "rtcmCorrUsed_01", "crCorrUsed_01", "diffCorr_02", "rtcmCorrUsed_02",
                  "hasCorrUsed_03", "spartnCorrUsed_04"],
         "values": {"iTOW": 345600000, "version": 1},
         "group": [
             {"gnssId": 0, "svId": 5, "cno": 45, "elev": 60, "azim": 120, "qualityInd": 7, "svUsed": 1,
              "health": 1, "diffCorr": 1, "orbitSource": 1, "ephAvail": 1, "rtcmCorrUsed": 1,
              "prCorrUsed": 1, "crCorrUsed": 1},
             {"gnssId": 6, "svId": 3, "cno": 33, "elev": 12, "azim": 300, "qualityInd": 7, "svUsed": 1,
              "health": 1, "diffCorr": 1, "orbitSource": 1, "ephAvail": 1},
             {"gnssId": 2, "svId": 11, "cno": 40, "elev": 45, "azim": 10, "qualityInd": 7, "svUsed": 1,
              "health": 1, "diffCorr": 1, "orbitSource": 1, "hasCorrUsed": 1, "prCorrUsed": 1, "crCorrUsed": 1},
             {"gnssId": 3, "svId": 30, "cno": 38, "elev": -100, "azim": 0, "qualityInd": 5, "svUsed": 1,
              "spartnCorrUsed": 1, "prCorrUsed": 1}]},
        # NAV-PVT: fixed/fresh, float/>=120 s, reserved carrSoln 3, codes 13..15, diffSoln=0 with age.
        {"id": "pvt:fixed-age-bucket3", "message": "NAV-PVT",
         "show": ["fixType", "gnssFixOk", "diffSoln", "carrSoln", "lastCorrectionAge", "lat", "lon"],
         "values": {"iTOW": 345600000, "year": 2026, "month": 10, "day": 12, "hour": 12, "validDate": 1,
                    "validTime": 1, "fixType": 3, "gnssFixOK": 1, "diffSoln": 1, "carrSoln": 2, "numSV": 31,
                    "lon": -1222419000, "lat": 473500000, "height": 100000, "hMSL": 80000, "hAcc": 14,
                    "vAcc": 20, "pDOP": 95, "lastCorrectionAge": 3}},
        {"id": "pvt:float-age-ge120", "message": "NAV-PVT",
         "show": ["carrSoln", "diffSoln", "lastCorrectionAge"],
         "values": {"fixType": 3, "gnssFixOK": 1, "diffSoln": 1, "carrSoln": 1, "lastCorrectionAge": 12}},
        {"id": "pvt:carrSoln-reserved-3", "message": "NAV-PVT", "show": ["carrSoln"],
         "values": {"fixType": 3, "gnssFixOK": 1, "diffSoln": 1, "carrSoln": 3, "lastCorrectionAge": 1}},
        {"id": "pvt:age-code-15", "message": "NAV-PVT", "show": ["lastCorrectionAge"],
         "values": {"fixType": 3, "gnssFixOK": 1, "diffSoln": 1, "carrSoln": 2, "lastCorrectionAge": 15}},
        {"id": "pvt:age-without-diffSoln", "message": "NAV-PVT", "show": ["diffSoln", "lastCorrectionAge"],
         "values": {"fixType": 3, "gnssFixOK": 1, "diffSoln": 0, "carrSoln": 0, "lastCorrectionAge": 4}},
        {"id": "pvt:no-age-no-fix", "message": "NAV-PVT", "show": ["fixType", "lastCorrectionAge"],
         "values": {"fixType": 0}},
        # Deliberate negatives.
        {"id": "neg:cor-truncated-11-bytes", "message": "RXM-COR",
         "values": cor(protocol=1, msgUsed=2, msgTypeValid=1, msgSubTypeValid=1, msgType=4072, msgSubType=0x0304),
         "truncate": 11, "expect_error": "Error"},
        {"id": "neg:cor-bad-checksum", "message": "RXM-COR", "values": cor(protocol=1),
         "corrupt_checksum": True, "expect_error": "checksum"},
        {"id": "neg:sat-overwidth-bitfield", "message": "NAV-SAT", "values": {"version": 1},
         "group": [{"qualityInd": 8}], "expect_error": "exceeds"},
    ]
    return cases


def run_negative(case: dict) -> dict:
    """Frames deliberately damaged after encoding."""
    if not (case.get("truncate") or case.get("corrupt_checksum")):
        return run_case(case)
    record: dict[str, Any] = {"id": case["id"], "message": case["message"], "synthetic": True,
                              "expected_error": case["expect_error"]}
    frame = encode(case["message"], "2.11", case.get("values", {}), case.get("group"))
    if case.get("truncate"):
        n = case["truncate"]
        cls, mid = CLASS_ID[case["message"]]
        body = bytes([cls, mid]) + struct.pack("<H", n) + frame[6:6 + n]
        ck_a = ck_b = 0
        for byte in body:
            ck_a = (ck_a + byte) & 0xFF
            ck_b = (ck_b + ck_a) & 0xFF
        frame = b"\xb5\x62" + body + bytes([ck_a, ck_b])
    if case.get("corrupt_checksum"):
        frame = frame[:-1] + bytes([frame[-1] ^ 0xFF])
    record["frame_hex"] = frame.hex()
    try:
        parsed = UBXReader.parse(frame, msgmode=GET, validate=VALCKSUM)
        record["parsed"] = str(parsed)
        record["ok"] = False
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["ok"] = case["expect_error"].lower() in record["error"].lower()
    return record


def legacy_rxm_rtcm() -> dict:
    """F9P UBX-RXM-RTCM (0x02 0x32), from pyubx2's own schema: shows the
    baseline consumer's identity and fields are distinct from RXM-COR's.
    Layout NOT re-verified against an F9 manual in this task."""
    msg = UBXMessage("RXM", "RXM-RTCM", GET, version=2, crcFailed=0, msgUsed=2, subType=0,
                     refStation=42, msgType=1077)
    raw = msg.serialize()
    parsed = UBXReader.parse(raw, msgmode=GET, validate=VALCKSUM)
    return {"id": "f9p:rxm-rtcm-schema", "frame_hex": raw.hex(), "identity": parsed.identity,
            "fields": {k: getattr(parsed, k) for k in ("version", "crcFailed", "msgUsed", "subType",
                                                     "refStation", "msgType")},
            "schema_source": "pyubx2 UBX_PAYLOADS_GET['RXM-RTCM']", "synthetic": True,
            "ok": parsed.identity == "RXM-RTCM"}


def main() -> int:
    label = sys.argv[1]
    cases = scenario_cases() + walking_bits() + reserved_bits()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    results = [run_negative(c) for c in cases] + [legacy_rxm_rtcm()]
    out = {
        "label": label,
        "python": platform.python_version(),
        "pyubx2": pyubx2.version,
        "cases": results,
        "summary": {"total": len(results), "ok": sum(r["ok"] for r in results),
                    "failed": [r["id"] for r in results if not r["ok"]]},
    }
    path = HERE / f"{label}-results.json"
    path.write_text(json.dumps(out, indent=1, default=str) + "\n")
    print(json.dumps({"label": label, "pyubx2": pyubx2.version, **out["summary"]}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
