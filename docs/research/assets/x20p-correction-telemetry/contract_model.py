#!/usr/bin/env python3
"""Reference semantics for the proposed X20P correction-use telemetry contract.

Research-only, stdlib-only. NOT an implementation and imports nothing from
the application package. It:

1. reads the baseline sources with ``git show <baseline>:<path>`` and executes
   only stdlib-safe pieces: ``tools/probe_correction_delivery.py:verdict`` in
   full, and the AST-selected ``_fresh`` / ``_stall_reason`` functions and
   threshold constants of ``survey_service.py``; it AST-extracts the driver's
   ``_CORRECTION_AGE_S`` table and the identity string ``_WatchedReader``
   compares against;
2. evaluates synthetic, hand-written evidence scenarios through (a) what the
   baseline would conclude on an X20P, where RXM-COR is never tallied, and
   (b) the proposed evidence ladder / tri-state verdict;
3. writes ``contract-scenarios.json`` beside this script.

Scenario inputs are synthetic and several encode receiver behaviour that is
*hypothetical until captured*; each scenario says so.

Usage:
    python3 contract_model.py <repo-root> [baseline-sha]
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

BASELINE = "8854c750f06ff321be29c9d29f9874484ce92b97"
MSM = range(1071, 1138)
REFERENCE_POSITION = (1005, 1006)


# --------------------------------------------------------------------------
# Baseline extraction (executed, not transcribed)
# --------------------------------------------------------------------------

def _git_show(repo: Path, sha: str, path: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{path}"],
                          check=True, capture_output=True, text=True).stdout


def load_baseline(repo: Path, sha: str) -> dict[str, Any]:
    probe_src = _git_show(repo, sha, "tools/probe_correction_delivery.py")
    probe_ns: dict[str, Any] = {"__name__": "baseline_probe"}
    exec(compile(probe_src, "probe_correction_delivery.py", "exec"), probe_ns)  # stdlib-only module

    survey_src = _git_show(repo, sha, "src/sp_rtk_base/services/survey_service.py")
    tree = ast.parse(survey_src)
    keep: list[ast.stmt] = [ast.parse("from __future__ import annotations").body[0]]
    consts: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id in {"MAX_CORRECTION_AGE_S", "FIXED_SETTLE_S", "STALL_ABORT_S"}:
            consts[node.target.id] = ast.literal_eval(node.value)  # type: ignore[arg-type]
            keep.append(node)
        if isinstance(node, ast.FunctionDef) and node.name in {"_fresh", "_stall_reason"}:
            keep.append(node)
    survey_ns: dict[str, Any] = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "survey_service.py[selected]", "exec"), survey_ns)

    driver_src = _git_show(repo, sha, "src/sp_rtk_base/services/drivers/ublox.py")
    dtree = ast.parse(driver_src)
    age_table: dict[int, float] | None = None
    watched_identity: str | None = None
    for node in ast.walk(dtree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "_CORRECTION_AGE_S":
            age_table = ast.literal_eval(node.value)  # type: ignore[arg-type]
        if isinstance(node, ast.ClassDef) and node.name == "_WatchedReader":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Compare) and sub.comparators and isinstance(sub.comparators[0], ast.Constant):
                    watched_identity = sub.comparators[0].value
    verify_src = _git_show(repo, sha, "src/sp_rtk_base/services/correction_verification.py")
    reference_window = next(ast.literal_eval(n.value) for n in ast.parse(verify_src).body
                            if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "REFERENCE_WINDOW_SECONDS")
    return {
        "verdict": probe_ns["verdict"],
        "MIN_USED_RATIO": probe_ns["MIN_USED_RATIO"],
        "MIN_RECEIVED_RATIO": probe_ns["MIN_RECEIVED_RATIO"],
        "fresh": survey_ns["_fresh"],
        "stall_reason": survey_ns["_stall_reason"],
        "consts": consts,
        "age_table": age_table,
        "watched_identity": watched_identity,
        "REFERENCE_WINDOW_SECONDS": reference_window,
    }


def baseline_correction_age(pvt: dict[str, int], table: dict[int, float]) -> float | None:
    """Mirror of UbloxDriver.get_survey_position's 4-line age rule
    (ublox.py L2018-L2022), using the extracted table."""
    if pvt.get("diffSoln", 0):
        return table.get(pvt.get("lastCorrectionAge", 0))
    return None


# --------------------------------------------------------------------------
# Proposed contract: normalised report + evidence ladder
# --------------------------------------------------------------------------

PROTOCOL = {0: "unknown", 1: "rtcm3", 2: "spartn", 5: "has", 29: "pmp_spartn", 30: "qzssl6"}
PROTOCOL_MIN_FW = {5: "2.10"}  # HAS value first documented in HPG 2.10 / 2.11 tables
ERR = {0: "unknown", 1: "error_free", 2: "erroneous"}
USE = {0: "unknown", 1: "not_used", 2: "used"}
CORR_SOURCE = {0: "none", 1: "sbas", 2: "beidou", 3: "rtcm2", 4: "rtcm3_osr", 5: "rtcm3_ssr",
               6: "qzss_slas", 7: "spartn", 9: "clas", 10: "lpp_osr", 11: "lpp_ssr", 12: "gal_has"}
CARR = {0: "none", 1: "float", 2: "fixed"}


def normalise_cor(raw: dict[str, int], payload_len: int = 12, firmware: str = "2.11") -> dict[str, Any]:
    """RXM-COR -> CorrectionUseReport (proposed). Enum values are compared
    exactly; anything undocumented becomes 'unknown' with the raw kept."""
    if payload_len != 12 or raw.get("version") != 1:
        return {"valid": False, "reason": "malformed" if payload_len != 12 else "unsupported_version", "raw": raw}
    proto = raw.get("protocol", 0)
    name = PROTOCOL.get(proto, "undocumented")
    if proto in PROTOCOL_MIN_FW and firmware < PROTOCOL_MIN_FW[proto]:
        name = "undocumented"
    cid = raw.get("correctionId", 0xFFFF)
    return {
        "valid": True,
        "telemetry": "rxm-cor",
        "protocol": name,
        "msg_type": raw.get("msgType") if raw.get("msgTypeValid") else None,
        "msg_subtype": raw.get("msgSubType") if raw.get("msgSubTypeValid") else None,
        "station_id": cid if name == "rtcm3" and cid <= 4095 else None,
        "integrity": ERR.get(raw.get("errStatus", 0), "unknown"),
        "use": USE.get(raw.get("msgUsed", 0), "unknown"),
        "input_handled": bool(raw.get("msgInputHandle", 0)),
        "raw": raw,
    }


def normalise_rxm_rtcm(raw: dict[str, int]) -> dict[str, Any]:
    """F9P RXM-RTCM -> the same report shape; preserves today's semantics."""
    return {"valid": True, "telemetry": "rxm-rtcm", "protocol": "rtcm3", "msg_type": raw["msgType"],
            "msg_subtype": raw.get("subType"), "station_id": raw.get("refStation"),
            "integrity": "erroneous" if raw.get("crcFailed") else "error_free",
            "use": USE.get(raw.get("msgUsed", 0), "unknown"), "input_handled": None, "raw": raw}


@dataclass
class Evidence:
    """One evaluation window (synthetic)."""
    firmware: str = "2.11"
    role: str = "rover"                     # rover | corrected_survey | base
    source_connected: bool = True
    frames_written: int = 120
    write_failures: int = 0
    dropped: int = 0
    port_rtcm3_received: int | None = 120   # MON-COMMS delta, None if unknown
    use_telemetry: str | None = "rxm-cor"   # rxm-cor | rxm-rtcm | None (not configured/available)
    reports: list[dict[str, Any]] = field(default_factory=list)       # normalised reports
    last_rtcm3_used_age_s: float | None = 0.5                          # host clock
    pvt: dict[str, int] | None = None
    sig: list[dict[str, int]] | None = None
    sig_itow: int | None = None
    pvt_itow: int | None = None
    expected_station: int | None = None     # from the source (e.g. a Verification's 1005)
    note: str = ""


STALE_S = 10.0  # = baseline MAX_CORRECTION_AGE_S; asserted below
AGE_BUCKET_FRESH_MAX = 4  # [5,10) s, the largest bucket whose upper bound <= 10 s


def classify(ev: Evidence, min_used_ratio: float) -> dict[str, Any]:
    """Proposed tri-state verdict over the evidence ladder."""
    if ev.role == "base":
        rtcm_in = [r for r in ev.reports if r.get("valid") and r["protocol"] == "rtcm3"]
        return {"verdict": "not_applicable", "ladder_level": None, "solution": None, "rtk_fixed_evidence": False,
                "red": [], "missing": [], "osr_signals": 0,
                "warnings": ["unexpected_correction_input_on_base"] if rtcm_in else []}
    reasons_red: list[str] = []
    missing: list[str] = []
    warnings: list[str] = []
    level = 0
    # L1 host delivery (existing feed), L2 port reception (existing MON-COMMS).
    if not ev.source_connected:
        reasons_red.append("source_interrupted")
    if ev.frames_written == 0:
        reasons_red.append("not_written")
    if ev.write_failures or ev.dropped:
        reasons_red.append("delivery_loss")
    if not reasons_red:
        level = 1
    if ev.port_rtcm3_received is None:
        missing.append("port_counters_unavailable")
    elif ev.frames_written and ev.port_rtcm3_received < 0.9 * ev.frames_written:
        reasons_red.append("not_received_by_port")
    elif level == 1:
        level = 2
    # Base role: correction input is unexpected.
    rtcm = [r for r in ev.reports if r.get("valid") and r["protocol"] == "rtcm3"]
    others = sorted({r["protocol"] for r in ev.reports if r.get("valid") and r["protocol"] not in ("rtcm3",)
                     and r["use"] == "used"})
    malformed = [r for r in ev.reports if not r.get("valid")]
    if malformed:
        warnings.append(f"malformed_reports:{len(malformed)}")
    # L3 parsed / L4 used.
    if ev.use_telemetry is None:
        missing.append("use_telemetry_unavailable")
    elif not rtcm:
        missing.append("use_reports_missing")  # drained/rate/none parsed: never 'not used'
    else:
        if level >= 2:
            level = 3
        erroneous = sum(r["integrity"] == "erroneous" for r in rtcm)
        if erroneous:
            reasons_red.append(f"erroneous:{erroneous}")
        msm = [r for r in rtcm if r["msg_type"] in MSM]
        judged = [r for r in msm if r["use"] in ("used", "not_used")]
        used = [r for r in judged if r["use"] == "used"]
        unknown = [r for r in msm if r["use"] == "unknown"]
        if unknown and not judged:
            missing.append("use_unknown")
        if judged and len(used) < min_used_ratio * len(judged):
            reasons_red.append(f"msm_not_used:{len(judged) - len(used)}/{len(judged)}")
        refs = [r for r in rtcm if r["msg_type"] in REFERENCE_POSITION]
        if not refs:
            missing.append("reference_position_report_missing")
        elif not any(r["use"] == "used" for r in refs):
            if any(r["use"] == "not_used" for r in refs):
                reasons_red.append("reference_position_not_used")
            else:
                missing.append("reference_position_use_unknown")
        stations = {r["station_id"] for r in rtcm if r["station_id"] is not None
                    and (r["msg_type"] in MSM or r["msg_type"] in REFERENCE_POSITION)}
        if len(stations) > 1:
            reasons_red.append("station_mismatch:" + ",".join(map(str, sorted(stations))))
        if ev.expected_station is not None and stations and stations != {ev.expected_station}:
            reasons_red.append("station_not_from_source")
        if ev.last_rtcm3_used_age_s is not None and ev.last_rtcm3_used_age_s > STALE_S:
            reasons_red.append("stale")
        if used and refs and any(r["use"] == "used" for r in refs) and level >= 3 and not any(
                x.startswith(("msm_not_used", "station", "stale", "erroneous")) for x in reasons_red):
            level = 4
    if others:
        warnings.append("other_protocol_used:" + ",".join(others))
    # L5 applied (NAV-SIG) and L6 solution (NAV-PVT), same epoch only.
    if ev.sig is None:
        missing.append("nav_sig_unavailable")
    if ev.pvt is None:
        missing.append("nav_pvt_unavailable")
    same_epoch = ev.sig is not None and ev.pvt is not None and ev.sig_itow == ev.pvt_itow
    if ev.sig is not None and ev.pvt is not None and not same_epoch:
        missing.append("epoch_mismatch")
    osr = carrier = 0
    sources: set[str] = set()
    if ev.sig is not None:
        for s in ev.sig:
            src = CORR_SOURCE.get(s.get("corrSource", 0), "undocumented")
            if src != "none":
                sources.add(src)
            if src == "rtcm3_osr" and (s.get("prCorrUsed") or s.get("crCorrUsed")):
                osr += 1
                carrier += bool(s.get("crCorrUsed"))
        if not osr and sources:
            reasons_red.append("other_source_only:" + ",".join(sorted(sources)))
        elif not osr:
            missing.append("no_signal_uses_rtcm3_osr")
    solution = None
    if ev.pvt is not None:
        p = ev.pvt
        solution = CARR.get(p.get("carrSoln", 0), "unknown")
        if solution == "unknown":
            missing.append("carrSoln_undocumented")
        age_ok = bool(p.get("diffSoln")) and 1 <= p.get("lastCorrectionAge", 0) <= AGE_BUCKET_FRESH_MAX
        if p.get("lastCorrectionAge", 0) > AGE_BUCKET_FRESH_MAX and p.get("diffSoln"):
            reasons_red.append("receiver_correction_age_stale")
        if level >= 4 and same_epoch and osr and p.get("gnssFixOK") and solution in ("float", "fixed") and age_ok:
            level = 6
        elif level >= 4 and same_epoch and osr:
            level = 5
    if reasons_red:
        verdict = "red"
    elif level >= 6:
        verdict = "green"
    else:
        verdict = "inconclusive"
    return {"verdict": verdict, "ladder_level": level, "solution": solution, "rtk_fixed_evidence":
            verdict == "green" and solution == "fixed" and carrier > 0, "red": reasons_red,
            "missing": sorted(set(missing)), "warnings": warnings, "osr_signals": osr}


def baseline_outcome(ev: Evidence, base: dict[str, Any]) -> dict[str, Any]:
    """What the baseline concludes from the same window on an X20P: COR is
    never tallied (identity filter), so ``rtcm`` stays empty unless the
    telemetry is F9P RXM-RTCM."""
    rtcm: dict[str, dict[str, int]] = {}
    if ev.use_telemetry == "rxm-rtcm":
        for r in ev.reports:
            u = rtcm.setdefault(str(r["msg_type"]), {"received": 0, "used": 0, "not_used": 0, "crc_failed": 0})
            u["received"] += 1
            u["used"] += r["use"] == "used"
            u["not_used"] += r["use"] == "not_used"
            u["crc_failed"] += r["integrity"] == "erroneous"
    progress = {"corrections_written": ev.frames_written, "corrections_dropped": ev.dropped,
                "correction_write_failures": ev.write_failures,
                "receiver_rtcm3_messages": ev.port_rtcm3_received or 0,
                "diagnostics": {"written_at_receiver_read": ev.frames_written, "driver": {"rtcm": rtcm}}}
    reasons = base["verdict"](progress)
    age = baseline_correction_age(ev.pvt, base["age_table"]) if ev.pvt else None
    fix_ok = bool(ev.pvt and ev.pvt.get("gnssFixOK") and ev.pvt.get("fixType", 3) in (3, 4))
    position = SimpleNamespace(correction_age_s=age, fix_ok=fix_ok,
                               rtk_status=CARR.get((ev.pvt or {}).get("carrSoln", 0), "none"))
    return {"probe_verdict": "green" if not reasons else "red", "probe_reasons": reasons,
            "survey_correction_age_s": age, "survey_fresh": base["fresh"](position),
            "survey_stall_reason": base["stall_reason"](position),
            "rtcm_tally_keys": sorted(rtcm)}


def cor(t: int, use: int = 2, err: int = 1, station: int = 42, proto: int = 1, **kw: int) -> dict[str, Any]:
    raw = {"version": 1, "protocol": proto, "errStatus": err, "msgUsed": use, "correctionId": station,
           "msgTypeValid": 1 if t else 0, "msgInputHandle": 1, "msgType": t, **kw}
    return normalise_cor(raw)


def osr_sig(n: int = 12) -> list[dict[str, int]]:
    return [{"corrSource": 4, "prCorrUsed": 1, "crCorrUsed": 1} for _ in range(n)]


FIXED = {"fixType": 3, "gnssFixOK": 1, "diffSoln": 1, "carrSoln": 2, "lastCorrectionAge": 2}


def scenarios() -> dict[str, Evidence]:
    healthy = [cor(1077), cor(1097), cor(1127), cor(1005), cor(1230)]
    return {
        "S01_healthy_rtcm_fixed": Evidence(reports=healthy, pvt=FIXED, sig=osr_sig(), sig_itow=1, pvt_itow=1,
                                           expected_station=42),
        "S02_x20_no_use_telemetry_but_fixed": Evidence(use_telemetry=None, reports=[], pvt=FIXED, sig=osr_sig(),
                                                       sig_itow=1, pvt_itow=1),
        "S03_msm_not_used": Evidence(reports=[cor(1077, use=1), cor(1097, use=1), cor(1005, use=1)],
                                     pvt={"fixType": 3, "gnssFixOK": 1}, sig=[], sig_itow=1, pvt_itow=1),
        "S04_use_unknown": Evidence(reports=[cor(1077, use=0), cor(1005, use=0)], pvt=FIXED, sig=osr_sig(),
                                    sig_itow=1, pvt_itow=1),
        "S05_station_mismatch": Evidence(reports=[cor(1077), cor(1005, station=43)],
                                         pvt={"fixType": 3, "gnssFixOK": 1, "diffSoln": 1, "carrSoln": 1,
                                              "lastCorrectionAge": 1}, sig=osr_sig(), sig_itow=1, pvt_itow=1),
        "S06_no_reference_position_report": Evidence(reports=[cor(1077), cor(1097)],
                                                     pvt={"fixType": 3, "gnssFixOK": 1, "diffSoln": 1,
                                                          "carrSoln": 1, "lastCorrectionAge": 1},
                                                     sig=osr_sig(), sig_itow=1, pvt_itow=1),
        "S07_has_fallback_after_source_loss_HYPOTHETICAL": Evidence(
            source_connected=False, frames_written=40, port_rtcm3_received=40,
            reports=[cor(0, proto=5, station=0xFFFF)], last_rtcm3_used_age_s=None,
            pvt={"fixType": 3, "gnssFixOK": 1, "diffSoln": 1, "carrSoln": 1, "lastCorrectionAge": 1},
            sig=[{"corrSource": 12, "prCorrUsed": 1, "crCorrUsed": 1}] * 8, sig_itow=1, pvt_itow=1,
            note="2.11 HAS fallback; whether HAS refreshes lastCorrectionAge or yields carrSoln=1 needs capture"),
        "S08_sbas_only_HYPOTHETICAL": Evidence(
            frames_written=0, port_rtcm3_received=0, reports=[], last_rtcm3_used_age_s=None,
            pvt={"fixType": 3, "gnssFixOK": 1, "diffSoln": 1, "carrSoln": 0, "lastCorrectionAge": 1},
            sig=[{"corrSource": 1, "prCorrUsed": 1}] * 8, sig_itow=1, pvt_itow=1,
            note="IM: SBAS sets diffSoln; whether it sets lastCorrectionAge needs capture"),
        "S09_stale_rtcm_receiver_still_fixed": Evidence(
            reports=healthy, last_rtcm3_used_age_s=25.0,
            pvt={**FIXED, "lastCorrectionAge": 7}, sig=osr_sig(), sig_itow=1, pvt_itow=1,
            note="receiver keeps RTCM up to CFG-NAVSPG-CONSTR_DGNSSTO (60 s default)"),
        "S10_source_interrupted": Evidence(source_connected=False, frames_written=30, port_rtcm3_received=30,
                                           reports=healthy, last_rtcm3_used_age_s=14.0,
                                           pvt={**FIXED, "lastCorrectionAge": 5}, sig=osr_sig(),
                                           sig_itow=1, pvt_itow=1),
        "S11_erroneous": Evidence(reports=[cor(1077, err=2, use=1), cor(1005)], pvt=FIXED, sig=osr_sig(),
                                  sig_itow=1, pvt_itow=1),
        "S12_spartn_alongside_rtcm": Evidence(reports=healthy + [cor(1, proto=2, station=0xFFFF)], pvt=FIXED,
                                              sig=osr_sig(), sig_itow=1, pvt_itow=1),
        "S13_f9p_rxm_rtcm_preserved": Evidence(
            firmware="F9 HPG 1.51", use_telemetry="rxm-rtcm",
            reports=[normalise_rxm_rtcm({"msgType": t, "msgUsed": 2, "refStation": 42, "crcFailed": 0})
                     for t in (1077, 1087, 1097, 1127, 1005, 1230)],
            pvt=FIXED, sig=osr_sig(), sig_itow=1, pvt_itow=1),
        "S14_reports_drained_or_absent": Evidence(reports=[], pvt=FIXED, sig=osr_sig(), sig_itow=1, pvt_itow=1),
        "S15_truncated_cor": Evidence(reports=[normalise_cor({"version": 1, "protocol": 1, "msgUsed": 2,
                                                              "msgType": 1077, "msgTypeValid": 1},
                                                             payload_len=11)] + healthy,
                                      pvt=FIXED, sig=osr_sig(), sig_itow=1, pvt_itow=1),
        "S16_undocumented_enums": Evidence(reports=[cor(1077, use=3, err=3), cor(0, proto=7, station=0xFFFF),
                                                    cor(1005, use=3)],
                                           pvt=FIXED, sig=osr_sig(), sig_itow=1, pvt_itow=1),
        "S17_base_role_unexpected_input": Evidence(role="base", frames_written=0, port_rtcm3_received=0,
                                                   reports=[cor(1077)], pvt={"fixType": 5, "gnssFixOK": 1},
                                                   sig=[], sig_itow=1, pvt_itow=1),
        "S18_fixed_without_nav_sig": Evidence(reports=healthy, pvt=FIXED, sig=None, pvt_itow=1),
        "S19_epoch_mismatch": Evidence(reports=healthy, pvt=FIXED, sig=osr_sig(), sig_itow=1, pvt_itow=2),
        "S20_has_on_2_02_is_undocumented": Evidence(firmware="2.02", reports=healthy, pvt=FIXED, sig=osr_sig(),
                                                    sig_itow=1, pvt_itow=1),
    }


def main() -> int:
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    sha = sys.argv[2] if len(sys.argv) > 2 else BASELINE
    base = load_baseline(repo, sha)
    assert base["consts"]["MAX_CORRECTION_AGE_S"] == STALE_S, base["consts"]
    assert base["watched_identity"] == "RXM-RTCM", base["watched_identity"]
    # Extra check for S20: the HAS protocol value on 2.02 normalises to undocumented.
    s20_has = normalise_cor({"version": 1, "protocol": 5, "msgUsed": 2, "errStatus": 1,
                             "correctionId": 0xFFFF}, firmware="2.02")
    results = {}
    for name, ev in scenarios().items():
        if name.startswith("S20"):
            ev.reports = ev.reports + [s20_has]
        results[name] = {"note": ev.note, "firmware": ev.firmware, "role": ev.role,
                         "baseline_on_x20": baseline_outcome(ev, base),
                         "proposed": classify(ev, base["MIN_USED_RATIO"])}
    age_codes = {str(c): base["age_table"].get(c) for c in range(16)}
    out = {
        "baseline_sha": sha,
        "baseline_extracted": {"MIN_USED_RATIO": base["MIN_USED_RATIO"],
                               "MIN_RECEIVED_RATIO": base["MIN_RECEIVED_RATIO"],
                               "survey_consts": base["consts"], "watched_identity": base["watched_identity"],
                               "REFERENCE_WINDOW_SECONDS": base["REFERENCE_WINDOW_SECONDS"],
                               "lastCorrectionAge_code_to_upper_bound_s": age_codes},
        "baseline_probe_with_no_receiver_telemetry": base["verdict"]({"corrections_written": 1}),
        "scenarios": results,
    }
    path = Path(__file__).with_name("contract-scenarios.json")
    path.write_text(json.dumps(out, indent=1, default=str) + "\n")
    for name, r in results.items():
        b, p = r["baseline_on_x20"], r["proposed"]
        print(f"{name:48} baseline probe={b['probe_verdict']:5} stall={b['survey_stall_reason']:15} "
              f"| proposed={p["verdict"]:14} L{p["ladder_level"]} red={p['red']} missing={p['missing']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
