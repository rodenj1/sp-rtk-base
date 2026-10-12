# X20P correction-use telemetry contract (proposed)

Research for [#254](https://github.com/rodenj1/sp-rtk-base/issues/254), under [map #247](https://github.com/rodenj1/sp-rtk-base/issues/247), 2026-10-12. Application baseline **8854c750f06ff321be29c9d29f9874484ce92b97 (v0.10.0)**. Target: ArduSimple simpleRTK4 Optimum / ZED-X20P, base and rover roles.

This is a **proposed contract**, not an implementation. All parser and scenario evidence uses **synthetic bytes**. No receiver, serial endpoint, host setting or network port was accessed. The actual receiver model, ROM and firmware are still unknown.

## Outcomes

1. **On X20P today the app sees no receiver correction-use evidence at all.** The driver counts only messages whose pyubx2 identity is `RXM-RTCM` ([ublox.py L333][watched]). The X20 interface descriptions document `UBX-RXM-COR` (class/ID 0x02 0x34) instead, and none of them contains RXM-RTCM. As a result the `rtcm` tally stays empty. With an empty tally, the bench probe's `verdict()` returns no reasons, which means **green** ([probe L51–87][probe]).
2. **On X20P, the current correction setup is likely to fail before any corrections are written.** `begin_correction_input` reads and then writes, in a single batch, RTCM3 input/output keys plus `CFG_MSGOUT_UBX_RXM_RTCM_<port>` ([L1636–1663][begin]). None of the four X20 catalogs contains an RXM_RTCM output key (0 occurrences in every extraction). The manual says a VALGET or VALSET containing an unknown key is NAKed **with nothing applied** (key-parser contract §4). So enabling RTCM3 input would be rejected together with the report key. This is a documented-behaviour inference that still needs a capture to confirm. The X20 equivalent keys are `CFG_MSGOUT_UBX_RXM_COR_{UART1,UART2,USB}` = 0x209106B7/B8/B9 (U1). They are present in all four catalogs and in locked pyubx2 1.3.0.
3. **RXM-COR is not a drop-in replacement for RXM-RTCM.** It carries no CRC-failed flag. It has a three-valued `errStatus` and a three-valued `msgUsed`. `correctionId` is valid only for some messages, and the message has no timestamp and no port field. The proposed mapping below keeps the F9P RXM-RTCM path unchanged and adds an X20 path chosen by firmware identity.
4. **Corrected survey's freshness test can be satisfied by non-RTCM corrections.** `_fresh` accepts any NAV-PVT with `diffSoln=1` and a correction-age bucket ≤10 s ([survey L168–171][fresh], [driver L1993–2030][survpos]). The integration manual says SBAS also sets diffSoln, and that on 2.11 Galileo HAS can take over when the RTCM source is lost. If HAS refreshes `lastCorrectionAge`, a lost RTCM source would be reported as "only Float" (`no_fixed`) rather than "no corrections". **Whether HAS refreshes that age is unconfirmed and needs a capture.**
5. **The proposed contract changes 13 of 20 modelled scenarios** from baseline-green to red, inconclusive or not-applicable. The main rule: missing, drained or unknown evidence is **inconclusive, never green**.

## Evidence and reproduction

Assets live in [`assets/x20p-correction-telemetry/`](assets/x20p-correction-telemetry/):

| File | What it is |
|---|---|
| `manual_layouts.py` → `manual-layouts.json` | Extracts RXM-COR, NAV-PVT, NAV-SAT and NAV-SIG field tables, bitfields and enums from the four cached full interface-description extractions. Records document IDs, SHA-256 hashes and printed pages. |
| `offline_parse.py` → `locked-results.json`, `candidate-results.json` | Builds synthetic frames from those layouts. Parses them with the real pyubx2 `UBXReader`, re-serializes them, walks every bitfield, checks reserved bits and negative cases, and runs an F9P RXM-RTCM schema check. |
| `contract_model.py` → `contract-scenarios.json` | Loads the baseline's own constants, watched identity and probe `verdict()` from the pinned source by AST (it does not import the app). Runs the baseline and the proposed classifier over 20 synthetic scenarios. |
| `aggregate.py` → `summary.json` | Asserts the layouts against values hand-transcribed from printed pages of I11, asserts the exact expected parser failures, dedupes and counts. |
| `run_all.sh` → `run.log` | Runs all of the above. Exits non-zero on any failure. |

Run from the repo root: `bash docs/research/assets/x20p-correction-telemetry/run_all.sh [MANUAL_DIR] [LOCKED_PY] [CANDIDATE_PY]`. The defaults are this host's scratch paths. Proprietary manuals are not committed; their hashes are in `manual-layouts.json`. Reproducing elsewhere needs fresh full extractions of the same revisions.

Results (also in `run.log` and `summary.json`):

- Manual sources: I00 UBXDOC-304424225-19888 R01 (HPG 2.00), I02 -19967 R03 (2.02), I10 -21263 R02 (2.10), I11 -21617 R01 (2.11). RXM-COR, NAV-PVT, NAV-SAT and NAV-SIG were found in all four. For 2.11 the printed pages are RXM-COR pp.152–153, NAV-PVT pp.110–112, NAV-SAT p.115 and NAV-SIG pp.118–119.
- The four messages have **identical field offsets, types and bit positions** in all four revisions. The enum differences are: RXM-COR `protocol=5` (HAS) appears only from 2.10, and NAV-SIG `ionoModel` gains values across revisions (5 → 6 → 9 entries).
- **pyubx2 1.3.0 (locked) and 1.3.8 (candidate): 263 cases each, 261 passed**, 7,737 decoded fields checked, 54 distinct bitfields walked. Both versions fail the same two cases, and both failures are expected:
  - `neg:cor-truncated-11-bytes`: an 11-byte RXM-COR payload parses **without error** into corrupted fields (`msgSubType=4`, `correctionId=0`). Consumers must check that the payload length is exactly 12 bytes themselves.
  - `pvt:fixed-age-bucket3`: parsing is exact, but re-serializing latitude 47.35 produces raw 473499999 (float rounding). This affects only frames built for tests, not decoding.
- Locked pyubx2 already exposes every RXM-COR field: `protocol`, `errStatus`, `msgUsed`, `correctionId`, `msgTypeValid`, `msgSubTypeValid`, `msgInputHandle`, `msgEncrypted`, `msgDecrypted`, `msgType`, `msgSubType` and `ebno`. Parsed RXM-COR has **no `crcFailed` attribute**. **No dependency upgrade is needed** for this contract.

## 1. Baseline model (what exists)

| Layer | Baseline | Source |
|---|---|---|
| NTRIP source verification | Connects to the caster and scans RTCM frames in a read window. This is source-side only and says nothing about receiver use. | [correction_verification.py L120–226][srcverify] |
| Host writes | `write_corrections` plus counters for written, dropped and failed frames. | [L1665–1704][write] |
| Receiver port count | MON-COMMS RTCM3 message and byte counters, polled every 30 s during corrected survey. | [survey L60–75, L149–165][survconst] |
| Receiver per-message use | RXM-RTCM tally per `msgType`: received, crc_failed, used (`msgUsed==2`), not_used (`msgUsed==1`). | [L316–334][watched], [L1589–1607][tally] |
| Survey acceptance | NAV-PVT `carrSoln=2`, fixed for 30 s, diffSoln and age ≤10 s. `_stall_reason` gives no_corrections / no_fixed / fixed_unsettled. | [survey L168–185][fresh], [L1976–2030][survpos] |
| Bench verdict | Red only on positive evidence of a problem. An empty RXM tally gives no reasons, which reads as green. | [probe L51–87][probe] |

Age table: the driver maps `lastCorrectionAge` code 12 → 120 s and codes 13–15 → no age ([L1976–1991][survpos]). The manual defines code ≥12 as "≥120 s". The current mapping is safe, since it stays far above the 10 s threshold. A future mapping should still treat codes 12–15 as **stale ≥120 s** rather than *unknown*.

## 2. RXM-COR field contract (all four revisions)

12-byte payload, version 1. `statusInfo` is X4 at offset 4. [I11 §3.16.1 pp.152–153; I00 p.118; I02 p.125; I10 pp.119–120]

| Field (bits) | Values | Proposed handling |
|---|---|---|
| `protocol` (0–4) | 0 unknown, 1 RTCM3, 2 SPARTN, 5 HAS (from 2.10), 29 PMP/SPARTN, 30 QZSS L6 | Only `1` counts toward the RTCM ladder. Other documented values are recorded as other-source activity. Values undocumented for the identified firmware (e.g. 5 on 2.02) are recorded with an `undocumented` warning and never counted. |
| `errStatus` (5–6) | 0 unknown, 1 error-free, 2 erroneous | Compare exact values. `2` is a red reason. `0` or out-of-range is unknown. |
| `msgUsed` (7–8) | 0 unknown, 1 not used, 2 used | Compare exact values, never truthiness. `3` is unknown. |
| `correctionId` (9–24) | RTCM reference station ID when the message type carries one, otherwise 0xFFFF | Station attribution only when ≤4095 **and** `msgTypeValid`. 0xFFFF means no attribution. |
| `msgTypeValid` (25), `msgSubTypeValid` (26) | 1 when msgType/msgSubType is meaningful | Ignore msgType when not valid. Count by RTCM type only when valid. |
| `msgInputHandle` (27) | 1 = input handling exists | Per the manual, this is **explicitly not evidence of use**. Diagnostic only. |
| `msgEncrypted` / `msgDecrypted` (28–31) | 0 unknown / 1 no / 2 yes | Relevant to SPARTN only. Ignore for RTCM. |
| Length ≠ 12 | — | Drop and count as `malformed_reports` (warning). Never parse into the tally. |

There is no timestamp and no port. Freshness has to be measured with the **host monotonic clock** at receipt. The input port can't be told from the report, so source attribution must use protocol plus station ID plus the app's own knowledge of which port it is writing to.

## 3. Proposed evidence ladder

The verdict can be green only at the top of the ladder. Every level records **why** it stopped.

| Level | Evidence | Source |
|---|---|---|
| L0 | Source verified (NTRIP/TCP frames scanned, station ID from 1005/1006) | existing source verification |
| L1 | Host wrote frames with no drops or write failures | existing counters |
| L2 | Receiver port counted RTCM3 (MON-COMMS) | existing survey counter |
| L3 | Receiver parsed: RXM-COR with `protocol=1`, error-free | **new X20 path**; F9P keeps RXM-RTCM `crcFailed==0` |
| L4 | Receiver used MSM (≥ MIN_USED_RATIO of judged) **and** used 1005/1006, all from one station matching L0 | X20: `msgUsed==2`; F9P: unchanged |
| L5 | NAV-SIG shows RTCM3 OSR applied: `corrSource=4` with `prCorrUsed`/`crCorrUsed` on used signals | NAV-SIG [I11 pp.118–119] |
| L6 | NAV-PVT of the **same iTOW** as L5: `gnssFixOk`, `carrSoln` 1/2, `diffSoln=1`, age bucket ≤4 (<10 s) | NAV-PVT [I11 pp.110–112] |

Classification rules (encoded in `contract_model.py classify()`):

- **Red** only on positive contrary evidence: frames not written; source interrupted; `errStatus=2`; MSM or reference position reported *not used*; a station mismatch; stale host-measured report age or receiver age bucket; or only other sources (HAS/SBAS) applied.
- **Inconclusive** whenever a level's evidence is missing or unknown: no use telemetry for this firmware; reports drained or absent; all-unknown `msgUsed`; no reference-position report; NAV-SIG not read; NAV-SIG and NAV-PVT epochs that don't match.
- **Not applicable** when the receiver is in base role (TMODE active). Correction input to a base is reported, not judged.
- **F9P is unchanged.** When identity is F9P/HPG 1.x, the RXM-RTCM tally and the existing `verdict()` logic stay exactly as they are. Model scenario S13 verifies this.
- **Malformed reports** are dropped with a warning and don't change the verdict on their own (S15). **Undocumented protocol values** for the identified firmware give a warning and are never counted (S20).

## 4. Source classes: RTCM OSR, SPARTN, HAS, SBAS

- Per NAV-SIG `corrSource`: 0 none, 1 SBAS, 2 BeiDou, 3 RTCM2, **4 RTCM3 OSR**, 5 RTCM3 SSR, 6 QZSS SLAS, 7 SPARTN, 9 CLAS, 10/11 LPP OSR/SSR, 12 GAL HAS ([I11 NAV-SIG]). Only 4 satisfies L5 for this app's RTCM base/rover workflows.
- SPARTN reported alongside RTCM (S12) is recorded, not penalised, provided the RTCM ladder is satisfied on its own.
- **HAS and SBAS can produce `diffSoln=1` and float positions without any OSR RTK.** A solution counts only when L5 attributes it to `corrSource=4`. A survey's freshness should likewise come from RTCM3 RXM-COR receipt age, not from `diffSoln` plus the age bucket alone. S07 and S08 are marked HYPOTHETICAL until a capture shows how HAS/SBAS affect `lastCorrectionAge` and `carrSoln`.
- Document discrepancy to resolve: the integration manual dates HAS configuration from 2.11, while the 2.10 release note announces HAS in 2.10.

## 5. Stale, missing and interrupted

| Condition | Detection | Verdict |
|---|---|---|
| Source interrupted | No new source frames within its expected interval (1005 interval × N) | Red `source_interrupted` |
| Reports stale | Host-measured age of the last RTCM3 RXM-COR > MAX_CORRECTION_AGE_S (10 s) | Red `stale` |
| Receiver age stale | Age bucket ≥5 (≥10 s) or code ≥12 | Red `receiver_correction_age_stale` |
| Reports absent or drained | Frames written but zero RTCM3 reports in the window | Inconclusive `use_reports_missing` |
| Firmware has no use telemetry or identity unknown | Capability lookup | Inconclusive `use_telemetry_unavailable` |

Draining risk: the driver calls `reset_input_buffer()` before many polls (e.g. [L1923][drain1], [L1961][drain2], [L2134][drain3]). Any unsolicited RXM-COR reports sitting in the buffer are discarded. The `_WatchedReader` only sees reports read during a poll. The **loss rate is unmeasured**, which is why absence is inconclusive rather than red.

## 6. Scenario results (synthetic)

From `contract-scenarios.json`. Proposed verdicts: 5 green, 7 red, 7 inconclusive, 1 not applicable.

| Scenario | Baseline probe / survey stall | Proposed |
|---|---|---|
| S01 healthy RTCM fixed | green / fixed_unsettled | green (L6) |
| S02 X20, no use telemetry, fixed | green | **inconclusive** |
| S03 MSM not used | green | **red** |
| S04 use unknown | green | **inconclusive** |
| S05 station mismatch | green | **red** |
| S06 no reference-position report | green | **inconclusive** |
| S07 HAS fallback after source loss (hypothetical) | green / no_fixed | **red** |
| S08 SBAS only (hypothetical) | red | red |
| S09 stale RTCM, receiver still fixed | green / no_corrections | **red** |
| S10 source interrupted | green | **red** |
| S11 erroneous | green | **red** |
| S12 SPARTN alongside RTCM | green | green |
| S13 F9P RXM-RTCM preserved | green | green |
| S14 reports drained or absent | green | **inconclusive** |
| S15 truncated COR (warning) | green | green |
| S16 undocumented enums | green | **inconclusive** |
| S17 base role, unexpected input | red | not applicable |
| S18 fixed without NAV-SIG | green | **inconclusive** |
| S19 epoch mismatch | green | **inconclusive** |
| S20 HAS value on 2.02 (warning) | green | green |

These are outputs of a research model, not app behaviour. The thresholds (MIN_USED_RATIO 0.9, 10 s) come from the baseline.

## 7. Requires real capture (authorization needed)

1. RXM-COR rate and which RTCM types get `msgUsed=2` (MSM4/MSM7, 1005/1006, 1230) on the installed firmware.
2. Whether `errStatus=2` ever occurs for RTCM, or whether CRC-failed frames produce no report at all.
3. `correctionId` behaviour on a station change and with a station-ID filter configured.
4. `msgInputHandle` values for supported versus unsupported RTCM types.
5. Correlation of NAV-SIG/NAV-SAT correction flags with `carrSoln` and `diffSoln`.
6. Whether HAS/SBAS refresh `lastCorrectionAge` or yield `carrSoln=1`, and 2.11 HAS fallback when RTCM is interrupted.
7. How many reports are lost to the driver's `reset_input_buffer()` before polls.
8. F9P RXM-RTCM `refStation` semantics, for symmetric station attribution.
9. Behaviour when correction input arrives while the receiver is in base (TMODE) mode.
10. Confirmation that a VALSET batch containing RXM_RTCM keys is NAKed on X20 with nothing applied (Outcome 2).

## 8. Boundary

This defines the **telemetry contract** that would inform design. It does not choose between a shared or a separate driver, add X20 support, or change the bench probe. The architecture decision remains open.

[watched]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L316-L334
[tally]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1589-L1607
[begin]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1636-L1663
[write]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1665-L1704
[survpos]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1976-L2030
[drain1]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1923
[drain2]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1961
[drain3]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2134
[probe]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/tools/probe_correction_delivery.py#L51-L87
[fresh]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/survey_service.py#L168-L185
[survconst]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/survey_service.py#L60-L75
[srcverify]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/correction_verification.py#L120-L226
