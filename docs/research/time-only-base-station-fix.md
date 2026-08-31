# Research: is `time_only` (fixType=5) + PDOP=99.99 the EXPECTED state for a fixed-mode u-blox F9P RTK base, or a broken acquisition?

> **Resolved: 2026-08-31** · Ticket [#83](https://github.com/rodenj1/sp-rtk-base/issues/83) · Map [#43](https://github.com/rodenj1/sp-rtk-base/issues/43) · Type: research (AFK)
> **Verdict: `time_only` (fixType=5) on a correctly-configured fixed-mode F9P base is the EXPECTED, documented, by-design state. It is not a broken acquisition.** PDOP=99.99 is the u-blox sentinel for "no position solution computed" and is likewise expected. The correct base-health observables are RTCM-stream-based, not fixType-based.

---

## 1. Question (from the ticket)

When a u-blox ZED-F9P is correctly configured as a fixed-mode RTK base station
(`CFG-TMODE-MODE=2`, stored survey-in coordinates), what GNSS solution / fix status
is actually expected from the receiver itself — and is
`NAV-PVT.fixType = 5 (time only)` with `PDOP = 99.99` the correct, expected state,
or a symptom of a broken acquisition?

Observed state at vaio-base (2026-08-31): `fix_type=time_only`, `num_satellites=32`,
`pdop=99.99`, `rtk_status=none`, position = stored fixed-base coordinates,
TMODE=mode 2 (Fixed), and **84 valid RTCM3 frames / 10 s (~705 B/s), 6 msg types** —
corrections ARE flowing.

---

## 2. Definitive answer — primary sources

### 2.1 u-blox ZED-F9P Integration Manual (UBX-18010802, R16, 30-Oct-2024) — PRIMARY

This is the vendor's own documentation for the exact receiver and the HPG firmware
family in play (covers HPG 1.12 → 1.51). Section **3.1.5.5 "Stationary base
operation"** states (emphasis added):

> "any stationary reference position messages are output only when the base station
> position has been initialized and is operating in **time mode**.
> **Time mode sets the receiver to operate as a stationary base station in fixed
> position and only time is estimated.**"

And §3.1.5.5.1 (survey-in):

> "Survey-in is a procedure that is carried out **prior to entering time mode**.
> … Survey-in ends when both requirements are successfully met. **The receiver
> begins operation in time mode** and can output a base position message if
> configured."

So the vendor documents a three-phase lifecycle for a static base:

1. **Disabled / rover** — normal position solving.
2. **Survey-in** (TMODE mode 1) — the receiver *does* compute 3D fixes and
   averages them to estimate its own position. This is the only phase where a
   3D fix from the base is meaningful.
3. **Time mode / Fixed** (TMODE mode 2, whether entered via completed survey-in
   or via manually entered coordinates) — **the receiver stops estimating
   position and only estimates time.** The stored ARP position becomes the
   reference point for RTCM generation.

Appendix **C.1 "Base configuration with u-center"** shows the end state of a
properly configured base in u-center and captions it:

> "Once surveyed in correctly, it will indicate a **TIME solution mode** in the
> u-center Data view." — *Figure 62: Base station: u-center data view in TIME
> mode*

u-center renders `NAV-PVT.fixType` in the Data view; the documented healthy
end-state for a working base is therefore literally the "TIME" fix type, i.e.
`fixType = 5`.

### 2.2 u-blox F9 HPG 1.51 Interface Description (UBXDOC-963802114-13124, R01) — PRIMARY

- **`UBX-NAV-PVT.fixType`** field definition (field 20 of NAV-PVT):
  `0 = no fix, 1 = dead reckoning only, 2 = 2D-fix, 3 = 3D-fix,
  4 = GNSS + dead reckoning combined, **5 = time only fix**`.
  Same value set on the HP variant (`UBX-NAV-PVT` in the HPG section, and in the
  `UBX-NAV-CLOCK`/`UBX-RELPOSNED`-adjacent table: `0x05 = Time only fix`).
- **NMEA PUBX,00 `navStat`** uses the same classification: `TT = Time only
  solution` alongside `G2/G3` (2D/3D stand-alone), `D2/D3` (differential),
  `RK` (dead reckoning). So "time only" is a **first-class, documented solution
  class** — not an error code.
- **`CFG-TMODE`** is itself titled **"Time mode configuration"**
  (§6.9.28: "Configuration for operation of the receiver in **Time mode**"),
  and the coordinate items `CFG-TMODE-ECEF_*` / `-LAT/-LON/-HEIGHT` are "only
  used if `CFG-TMODE-MODE=FIXED`". The vendor's naming is unambiguous: TMODE *is*
  time mode.
- `NAV-PVT.flags.bit0 = gnssFixOK` — "1 = valid fix (i.e. within DOP & accuracy
  masks)". In time-only mode the receiver is **GNSS-locked** (time locked to the
  constellation) but does not produce a position fix; the integration manual
  itself directs users to check `gnssFixOK` rather than `fixType` for "is the fix
  valid".

### 2.3 SparkFun — official F9P vendor partner — SECONDARY

- **SparkFun Learn, "Setting up a Rover Base RTK System"**
  (https://learn.sparkfun.com/tutorials/setting-up-a-rover-base-rtk-system/all):
  the base "will begin to look at the satellites zooming overhead and calculate
  its position… Because the base knows it is motionless, it can determine the
  disturbances in the ionosphere and troposphere … and begin to calculate the
  values needed to correct the location" — i.e. the base's job is correction
  generation, not position reporting.
- **SparkFun Community** (https://community.sparkfun.com/t/zed-f9p-to-zed-f9p-rtk-correction-questions/44003),
  SparkFun support (PaulZC, June 2022), in direct answer to a user observing
  exactly the state in this ticket — base reporting **fix type 5, "Time fix
  only"** after survey-in:

  > "When the base has completed its **survey-in**, it **goes into 'Time' mode**.
  > It is then **only calculating time** from the satellite signals.
  > **It stops calculating position.** It uses the location it calculated during
  > the survey-in to calculate the error in the satellite signals. It generates
  > RTCM corrections for each satellite it can 'see'."

  This is word-for-word the same behaviour described in the u-blox manual and the
  same state vaio-base is showing.

### 2.4 ArduSimple — SECONDARY

- **ArduSimple documentation / F9P base configuration**
  (https://www.ardusimple.com/how-to-configure-ublox-zed-f9p/): the official
  **Base** configuration profile (FW 1.51:
  `simpleRTK2B_FW151_Base-01.txt`) does **survey-in** (target accuracy 2.5 m),
  sets UART2 to 115200, and enables the standard RTCM set **1005, 1074, 1084,
  1094, 1230** — the same set the reference receiver carries. Nothing in the
  ArduSimple base workflow expects or checks a 3D fix on the base after
  survey-in; the base is a black box that must produce the RTCM stream.
- ArduSimple's base health is therefore gated on **RTCM output**, consistent with
  the expected-observable recommendation below.

### 2.5 Third-party corroborating source

**greenforge-labs/oxide_gnss** (ROS 2 GNSS driver, F9P base/rover support),
`docs/CONFIGURATION.md`:

> "a ghost TMODE3 would otherwise produce **`fixType = 5` ("Time Only") and no
> RTCM output** on the next run" — in the *broken* case.

This confirms two things: (a) `fixType = 5` is the documented F9P fixed-mode
signature even to third-party integrators, and (b) **time-only alone is not the
defect signature — time-only *with no RTCM output* is.** The defect pair is
"time-only AND no corrections"; "time-only WITH corrections flowing" is the
healthy base.

---

## 3. Mechanism — what a fixed base is and isn't solving

A fixed-mode RTK base does **not** solve for its own position. It:

1. **Locks GNSS time/frequency** from the constellation (this is the "time only"
   solution — `fixType = 5`). Time lock is what makes the phase measurements
   coherent across epochs; without it the carrier-phase corrections would be
   meaningless.
2. **Uses the stored ARP position** (from survey-in or manual entry, in
   `CFG-TMODE-ECEF_*` / `-LAT/-LON/-HEIGHT`) as the known reference point.
3. **Emits RTCM corrections** anchored to that stored position: per-satellite
   phase/code observations (MSM messages 10x4/11x4), the ARP position itself
   (1005/1006), and constellation-specific extras (1230 GLONASS biases).

Because the base never computes a *position solution*, the `NAV-PVT` fields that
describe position solutions carry no meaningful value:

| Field | Expected value on a healthy fixed base | Why |
|---|---|---|
| `fixType` | **5 (time only)** | The documented solution class for time mode |
| `flags.gnssFixOK` | 1 | Time is locked to GNSS |
| `pdop` / `hdop` / `vdop` | **99.99 (sentinel)** | No position solution ⇒ no position DOP; the receiver reports the "no value" sentinel |
| `hAcc` / `vAcc` | Not meaningful (may still show the last survey-in residual or the configured `FIXED_POS_ACC`) | Not a solved position |
| `lat/lon/alt` | The stored ARP / survey-in estimate, constant | Not live-solved |
| `numSv` | Normal (≥ 4) | The receiver is still tracking all satellites — time-only mode is *not* a loss of satellite lock |
| `carrSoln` / `diffSoln` | 0 / 0 | The base is not a rover; it applies no corrections |

The vaio-base observation — `fixType=5`, 32 SVs, `pdop=99.99`, RTCM streaming at
~705 B/s with 6 message types — is therefore **exactly the documented healthy
state**. The horizontal/vertical accuracy values seen (0.487 / 0.344 m) are the
survey-in residual / `FIXED_POS_ACC` carried in the report, not a live solution.

**When would `time_only` be a defect?** Only if the RTCM stream is *not* flowing
(no 1005/4072.0, no MSM messages, ~0 B/s) — i.e. the ghost-TMODE3 case in
oxide_gnss's docs — or if the base lost GNSS time lock (`flags.gnssFixOK = 0`,
`fixType = 0`). Neither applies here: 84 valid RTCM3 frames in 10 s.

---

## 4. Expected observable — what the app should check for base health

**Do not gate base health on `NAV-PVT.fixType == 3`.** That is the rover
expectation; on a fixed base it is permanently false by design and would flag
every healthy base as broken.

For a receiver in **base role** (TMODE mode ∈ {survey-in, fixed}), the correct
health checks are:

1. **RTCM stream health (primary):** valid RTCM3 frames being emitted on the
   data-link port(s) — e.g. ≥ 1 frame/s, and **RTCM 1005 (or 4072.0) present**
   plus the enabled MSM set. The app already has this (wire/RTCM counters).
2. **GNSS lock (secondary):** `NAV-PVT.flags.gnssFixOK == 1` and
   `numSv ≥ 4` (time-locked and seeing satellites). A time-only fix with
   `gnssFixOK=1` is healthy; `fixType = 0` with no lock is not.
3. **TMODE consistency (context):** `CFG-TMODE-MODE = 2` + non-zero stored
   coordinates + `NAV-SVIN` not still accumulating. The "Base station position
   seems incorrect" `INF-WARNING` is the vendor's own positional-drift alarm
   (manual §3.1.5.5.2) — worth surfacing if it fires.
4. **`fixType = 5` on a base = expected, render as such.** Do not treat it as a
   warning color.

---

## 5. Is the app's current display misleading?

Yes, mildly, but it is **display** not **logic**:

- `GpsFixType.TIME_ONLY` is already modeled and rendered as a distinct badge
  ("Time Only", blue-grey) in `src/sp_rtk_base/ui/pages/survey.py` — the app
  does *not* crash or treat it as no-fix. Good.
- However, nothing on the base/survey pages tells the operator that **"Time Only
  is the normal, correct state for a fixed base"** — so an operator (or a future
  health monitor) will read `Time Only` + `PDOP 99.99` as a fault. The fix is
  contextual, not structural: when the receiver is in base mode (TMODE fixed),
  label the fix badge as e.g. **"Time Only (expected for fixed base)"** and
  suppress the PDOP/accuracy concern; keep the RTCM-stream status as the primary
  "is the base working" indicator.
- No code path was found that **gates** base behaviour on `fixType == 3`, so no
  functional change is required — only a display/UX alignment and (if health
  monitoring is added later) a role-aware health check.

---

## 6. Evidence index

| # | Source | Type | What it establishes |
|---|---|---|---|
| 1 | u-blox ZED-F9P Integration Manual, UBX-18010802 R16, §3.1.5.5 + App. C.1 (Figure 62) | **Primary (vendor)** | "Time mode sets the receiver to operate as a stationary base station in fixed position and only time is estimated"; healthy configured base shows "TIME solution mode" in u-center |
| 2 | u-blox F9 HPG 1.51 Interface Description, UBXDOC-963802114-13124 (NAV-PVT.fixType; CFG-TMODE "Time mode configuration") | **Primary (vendor)** | `fixType=5 = time only fix` is a first-class solution class; TMODE = time mode |
| 3 | SparkFun Learn: "Setting up a Rover Base RTK System" | Secondary (vendor partner) | Base's role is correction generation; base knows it is motionless |
| 4 | SparkFun Community thread #44003 (support answer, 2022-06) | Secondary (vendor partner) | Post-survey-in base "goes into Time mode… stops calculating position" — same observed state |
| 5 | ArduSimple "How to configure u-blox ZED-F9P" + official Base config file (FW 1.51) | Secondary | Base workflow = survey-in + RTCM set (1005/1074/1084/1094/1230); no 3D-fix-on-base expectation |
| 6 | greenforge-labs/oxide_gnss CONFIGURATION.md | Third-party | `fixType=5` = time-only signature; defect is time-only **+ no RTCM output** |
| 7 | Local: `src/sp_rtk_base/models/device_models.py`, `services/drivers/ublox.py`, `ui/pages/survey.py` | Local | App models `TIME_ONLY`, renders it distinctly, gates nothing on `fixType==3` |
| 8 | Local: `docs/zed-f9p-base-station-config-reference.md` | Local | Reference base audit: TMODE=2 fixed + 32 config changes; consistent with time-mode expectation |

*Sources 1–2 were read in full (extracted text retained locally). The u-blox
portal forum (portal.u-blox.com) was JS-gated and could not be extracted; the
vendor-primary evidence above does not depend on it.*
