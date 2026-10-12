#!/usr/bin/env python3
"""Aggregate and assert the offline correction-telemetry evidence.

Stdlib-only. Reads manual-layouts.json, locked-results.json,
candidate-results.json and contract-scenarios.json; asserts the extracted
manual layouts against values hand-transcribed from the printed pages of the
u-blox X20 HPG 2.11 interface description (UBXDOC-304424225-21617 R01) and the
per-revision differences; asserts the exact expected parser failures; dedupes
and counts; writes summary.json. Exit status 1 if any assertion fails.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
VERSIONS = ("2.00", "2.02", "2.10", "2.11")

# Hand-transcribed from printed pages (I11 pp.152-154, 118-120, 115-116, 110-113).
EXPECT_2_11 = {
    "RXM-COR": {
        "pages": [152, 154], "length": "12",
        "fields": [("0", "U1", "version"), ("1", "U1", "ebno"), ("2", "U1", "reserved0"),
                   ("3", "U1", "reserved1"), ("4", "X4", "statusInfo"), ("8", "U2", "msgType"),
                   ("10", "U2", "msgSubType")],
        "bits": [(0, 5, "protocol"), (5, 2, "errStatus"), (7, 2, "msgUsed"), (9, 16, "correctionId"),
                 (25, 1, "msgTypeValid"), (26, 1, "msgSubTypeValid"), (27, 1, "msgInputHandle"),
                 (28, 2, "msgEncrypted"), (30, 2, "msgDecrypted")],
        "enums": {"protocol": ["0", "1", "2", "5", "29", "30"], "errStatus": ["0", "1", "2"],
                  "msgUsed": ["0", "1", "2"]},
    },
    "NAV-SIG": {
        "pages": [118, 120], "length": "8 + numSigs·16",
        "bits": [(0, 2, "health"), (2, 1, "prSmoothed"), (3, 1, "prUsed"), (4, 1, "crUsed"), (5, 1, "doUsed"),
                 (6, 1, "prCorrUsed"), (7, 1, "crCorrUsed"), (8, 1, "doCorrUsed"), (9, 1, "authStatus")],
        "enums": {"corrSource": ["0", "1", "2", "3", "4", "5", "6", "7", "9", "10", "11", "12"]},
        "offsets": {"corrSource": "16 + n·16", "sigFlags": "18 + n·16", "sigId": "10 + n·16"},
    },
    "NAV-SAT": {
        "pages": [115, 116], "length": "8 + numSvs·12",
        "bits": [(3, 1, "svUsed"), (6, 1, "diffCorr"), (16, 1, "sbasCorrUsed"), (17, 1, "rtcmCorrUsed"),
                 (18, 1, "slasCorrUsed"), (19, 1, "spartnCorrUsed"), (20, 1, "prCorrUsed"), (21, 1, "crCorrUsed"),
                 (22, 1, "doCorrUsed"), (23, 1, "clasCorrUsed"), (24, 1, "lppCorrUsed"), (25, 1, "hasCorrUsed")],
        "offsets": {"flags": "16 + n·12"},
    },
    "NAV-PVT": {
        "pages": [110, 113], "length": "92",
        "bits": [(0, 1, "gnssFixOK"), (1, 1, "diffSoln"), (6, 2, "carrSoln"), (1, 4, "lastCorrectionAge")],
        "enums": {"carrSoln": ["0", "1", "2"],
                  "lastCorrectionAge": [str(i) for i in range(12)] + [">=12"]},
        "offsets": {"flags": "21", "flags3": "78", "fixType": "20"},
    },
}
# Per-revision enum differences that must be present (and nothing else).
EXPECT_ENUM_DIFFS = {
    ("RXM-COR", "protocol"): {"2.00": ["0", "1", "2", "29", "30"], "2.02": ["0", "1", "2", "29", "30"]},
    ("NAV-SIG", "ionoModel"): {"2.00": ["0", "1", "2", "3", "8"], "2.02": ["0", "1", "2", "3", "4", "8"]},
}
# Failures we expect and explain, identical in both parser versions.
EXPECTED_FAILURES = {
    "pvt:fixed-age-bucket3": "pyubx2 serializer truncates scaled lat 47.35 to raw 473499999 (parse is exact)",
    "neg:cor-truncated-11-bytes": "pyubx2 parses an 11-byte RXM-COR payload without error; msgSubType=4 not 0x0304",
}

errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        errors.append(msg)


def main() -> int:
    layouts = json.loads((HERE / "manual-layouts.json").read_text())
    msgs = layouts["messages"]
    for message, exp in EXPECT_2_11.items():
        info = msgs[message]["2.11"]
        check(info["printed_pages"] == exp["pages"], f"{message} pages {info['printed_pages']} != {exp['pages']}")
        check(info["length"] == exp["length"], f"{message} length {info['length']!r}")
        if "fields" in exp:
            got = [(f["offset"], f["type"], f["name"]) for f in info["fields"]]
            check(got == exp["fields"], f"{message} fields {got}")
        bits = {(b["lsb"], b["width"], b["name"]) for b in info["bits"]}
        for b in exp["bits"]:
            check(b in bits, f"{message} missing bit {b}")
        for name, values in exp.get("enums", {}).items():
            got = [v for v, _ in info["enums"].get(name, [])]
            check(got == values, f"{message}.{name} enum {got} != {values}")
        offsets = {f["name"]: f["offset"] for f in info["fields"]}
        for name, off in exp.get("offsets", {}).items():
            check(offsets.get(name) == off, f"{message}.{name} offset {offsets.get(name)} != {off}")
    # Cross-revision: identical fields/bits; enum values identical except listed diffs.
    revision_diffs = []
    for message, per in msgs.items():
        ref = per["2.11"]
        for v in VERSIONS:
            info = per[v]
            check([(f["offset"], f["type"], f["name"]) for f in info["fields"]]
                  == [(f["offset"], f["type"], f["name"]) for f in ref["fields"]], f"{message} {v} fields differ")
            check([(b["lsb"], b["width"], b["name"]) for b in info["bits"]]
                  == [(b["lsb"], b["width"], b["name"]) for b in ref["bits"]], f"{message} {v} bits differ")
            for name in set(info["enums"]) | set(ref["enums"]):
                a = [x for x, _ in info["enums"].get(name, [])]
                b = [x for x, _ in ref["enums"].get(name, [])]
                if a != b:
                    revision_diffs.append({"message": message, "field": name, "version": v, "values": a,
                                           "values_2.11": b})
                    want = EXPECT_ENUM_DIFFS.get((message, name), {}).get(v)
                    check(want == a, f"unexpected enum diff {message}.{name} {v}: {a}")
    for (message, name), per in EXPECT_ENUM_DIFFS.items():
        for v in per:
            check(any(d["message"] == message and d["field"] == name and d["version"] == v
                      for d in revision_diffs), f"expected enum diff not found {message}.{name} {v}")

    runs = {label: json.loads((HERE / f"{label}-results.json").read_text()) for label in ("locked", "candidate")}
    per_run = {}
    for label, run in runs.items():
        ids = [c["id"] for c in run["cases"]]
        check(len(ids) == len(set(ids)), f"{label}: duplicate ids")
        failed = sorted(c["id"] for c in run["cases"] if not c["ok"])
        check(set(failed) == set(EXPECTED_FAILURES), f"{label}: failures {failed}")
        kinds = Counter(c["id"].split(":")[0] for c in run["cases"])
        per_run[label] = {"pyubx2": run["pyubx2"], "python": run["python"], "total": len(ids),
                          "unique_ids": len(set(ids)), "ok": len(ids) - len(failed), "failed": failed,
                          "by_kind": dict(sorted(kinds.items())),
                          "fields_checked": sum(c.get("fields_checked", 0) for c in run["cases"])}
    locked = {c["id"]: c for c in runs["locked"]["cases"]}
    cand = {c["id"]: c for c in runs["candidate"]["cases"]}
    check(set(locked) == set(cand), "case sets differ between parsers")
    frame_diffs = sorted(i for i in locked if locked[i].get("frame_hex") != cand[i].get("frame_hex"))
    outcome_diffs = sorted(i for i in locked if locked[i]["ok"] != cand[i]["ok"])
    check(not frame_diffs and not outcome_diffs, f"parser differences {frame_diffs} {outcome_diffs}")
    trunc = locked["neg:cor-truncated-11-bytes"].get("parsed", "")
    check("msgSubType=4)" in trunc, f"truncated parse detail changed: {trunc}")
    pvt = locked["pvt:fixed-age-bucket3"]
    check(pvt.get("parse_mismatches") == {} and pvt.get("serializer_equal") is False,
          "pvt serializer finding changed")
    walking = [c for c in locked.values() if c["id"].startswith("walk:")]
    unique_bitfields = {c["id"].split(":", 3)[1] + ":" + c["id"].split(":", 3)[3] for c in walking}

    scen = json.loads((HERE / "contract-scenarios.json").read_text())
    verdicts = Counter(s["proposed"]["verdict"] for s in scen["scenarios"].values())
    base_green_but_not = sorted(k for k, s in scen["scenarios"].items()
                                if s["baseline_on_x20"]["probe_verdict"] == "green"
                                and s["proposed"]["verdict"] != "green")
    check(scen["baseline_probe_with_no_receiver_telemetry"] == [], "baseline probe no-telemetry result changed")

    summary = {
        "manual_documents": layouts["documents"],
        "manual_pages": {m: {v: per[v]["printed_pages"] for v in VERSIONS} for m, per in msgs.items()},
        "manual_revision_enum_diffs": revision_diffs,
        "parser_runs": per_run,
        "unique_bitfields_walked": len(unique_bitfields),
        "expected_failures": EXPECTED_FAILURES,
        "parser_frame_or_outcome_differences": frame_diffs + outcome_diffs,
        "contract_scenarios": {"total": len(scen["scenarios"]), "proposed_verdicts": dict(sorted(verdicts.items())),
                               "baseline_probe_green_but_proposed_not_green": base_green_but_not},
        "baseline_extracted": scen["baseline_extracted"],
        "assertion_errors": errors,
    }
    (HERE / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({k: summary[k] for k in ("parser_runs", "unique_bitfields_walked", "contract_scenarios",
                                                "assertion_errors")}, indent=1, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
