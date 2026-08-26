# Research: Identifying the Connected u-blox Receiver

Status: research (throwaway branch `research/hardware-identity-detection`)
Date: 2026-08-26
Scope: how `sp-rtk-base` can identify the connected u-blox receiver
(model + hardware revision + feature set) so a future **profiles** feature
can default to / filter compatible profiles per hardware.

Goal: answer how the app can tell **ZED-F9P** vs **ZED-X20P** (and other
models) apart, so a profile's `hardware` field can match against a
well-defined "hardware target". The mechanism must be general, not
X20P-specific.

Method note: this research is read-only. The driver source in this repo
(`src/sp_rtk_base/services/drivers/ublox.py`) was read in full, and
u-blox documentation / community data (MON-VER outputs for F9P and X20P)
was gathered from the web. Live receiver probing on the vaio-base F9P
(192.168.0.27) was **not** performed because interactive write access to
the receiver and to the Pi were declined; the worked F9P example below is
therefore reconstructed from the operator-reported bring-up output plus
u-blox community captures of the same firmware generation, and is flagged
as such.

---

## 0. TL;DR (answers up front)

1. **The app already reads exactly one identification message on connect:
   `MON-VER` (UBX class 0x0A, msgID 0x0A)**, via `_poll_mon_ver()`. It parses
   `swVersion`, `hwVersion`, and the `extension_*` strings, extracting
   `FWVER=`, `PROTVER=`, and `MOD=`.
2. **`MON-VER` DOES carry a model string** — the `extension` field
   `MOD=<MODEL>` — and also the hardware-revision field `hwVersion`
   (a 4-byte u32LE `boardId`, e.g. `00190000`). The earlier bring-up
   symptom `FW: HPG 1.51 HW: ?` was a **display gap, not a data gap**:
   the `hwVersion` field for an F9P is `00190000` (a non-printable binary
   board ID, not the literal string `?`), and the *model* comes from the
   `MOD=` extension, which is a separate field the old display line did not
   surface.
3. **The authoritative identification command is `MON-VER`**:
   - (a) specific receiver **model** → `extension` `MOD=<MODEL>` (e.g.
     `MOD=ZED-F9P`, `MOD=ZED-X20P`), with `swVersion` prefix (`EXT HPG`,
     `EXT ADR`, `EXT M10`) as a family cross-check;
   - (b) **hardware revision / board ID** → `hwVersion` (u32LE, e.g.
     `00190000`);
   - (c) **supported feature set** → the `extension` **GNSS constellation
     string** (`GPS;GLO;GAL;BDS` / `SBAS;QZSS` / `NAVIC;LBAND`), the
     `FWVER`/`PROTVER` values, and (for config-key capability) a probe
     `CFG-VALGET` of representative keys — not a single "feature-flag"
     poll.
4. **Cleanest identity → hardware-target mapping:** use the `MOD=` value as
   the canonical, stable model identifier (map `MOD` → internal hardware
   enum), fall back to the `swVersion` family prefix, and treat the
   `hwVersion` board-ID as a secondary discriminator. Keep a per-model
   table of (MOD, sw-prefix, hwVersion, PROTVER, feature-set) so unknown
   receivers degrade to a family-level or "unknown" target rather than a
   crash.
5. **Fallback when identity is unknown:** accept the receiver, expose
   `hardware="unknown"` (or a `u-blox-gen9+`/`u-blox-gen10` family token
   derived from `PROTVER`), and let the profile filter treat "unknown" as
   "only profiles marked compatible with unknown/any". Never hard-gate a
   connect on identity.

---

## Q1. What does the app already read on connect? What does MON-VER return for a ZED-F9P HPG 1.51?

### What the driver does today

`UbloxDriver.connect()` → `_poll_mon_ver()`
(`src/sp_rtk_base/services/drivers/ublox.py`, line 1588). It:

1. Writes a single UBX poll: `UBXMessage("MON", "MON-VER", POLL)`
   (class `0x0A`, msgID `0x0A`).
2. Reads until a parsed message with `identity == "MON-VER"` arrives
   (wall-clock `CONNECT_TIMEOUT = 10 s`, cancel-aware).
3. Parses, per field:
   - `swVersion` (bytes, 30-char zero-terminated) → `sw_version_str`
   - `hwVersion` (bytes, 10-char zero-terminated) → `hardware`
   - up to 30 `extension_XX` fields (pyubx2 names them `extension_01` …;
     the code also tries `extension_00`). For each it:
     - `FWVER=`  → `fwver`  (e.g. `HPG 1.32`)
     - `PROTVER=`→ `protocol` (e.g. `27.31`)
     - `MOD=`    → `model`  (e.g. `ZED-F9P`)
     - else, if it contains `ZED-` / `NEO-` / `MAX-` / `SAM-` / `LEA-` →
       `model`
   - `firmware = fwver or sw_version_str`
   - **Fallback** model inference (only if `MOD=` absent):
     - `hardware in _hw_model_map` → map board-ID to model
       (`00190000→ZED-F9P`, `001B0000→ZED-F9R`, `00180000→NEO-M9N`)
     - else `firmware` contains `HPG` → `ZED-F9P`; `ADR` → `ZED-F9R`
4. Returns a `DeviceInfo(vendor="u-blox", model=..., firmware_version=...,
   protocol_version=..., hardware_version=...)`.

`DeviceInfo` is defined in
`src/sp_rtk_base/models/device_models.py` (line 58) with fields
`vendor, model, firmware_version, protocol_version, hardware_version,
serial_number`. Note: there is currently **no `hardware_target` / family
enum** field — the raw `model` string is what downstream would match on.

So the app already extracts the model from `MOD=` when present. The known
pain point is the *display* (`FW: HPG 1.51 HW: ?`) and the *fallbacks*,
not the absence of the data.

### What MON-VER actually returns for a ZED-F9P (HPG 1.x generation)

MON-VER layout (u-blox UBX-13003228 / `ublox_msgs::MonVER`):

| Field | Type | Meaning |
|---|---|---|
| `swVersion` | char[30] | Zero-terminated software version string |
| `hwVersion` | char[10] | Zero-terminated hardware version string (binary board ID on F9/X20) |
| `extension[16..30]` | char[30] each | Repeated extension strings (ROM BASE, FWVER, PROTVER, MOD, GNSS constellations) |

Representative MON-VER payloads (captured from u-blox community / ubxtool):

**ZED-F9P, HPG 1.13 (HPG L1/L5 generation — same `MOD=`/`PROTVER` shape as 1.51):**

```
swVersion  : "EXT CORE 1.00 (f10c36)"      -> family prefix "EXT CORE"/"EXT HPG"
hwVersion  : 00190000                       (u32LE boardId for the F9P)
extension  : "ROM BASE 0x118B2060"
extension  : "FWVER=HPG 1.13"
extension  : "PROTVER=27.12"
extension  : "MOD=ZED-F9P"
extension  : "GPS;GLO;GAL;BDS"
extension  : "SBAS;QZSS"
```

**ZED-X20P, HPG 2.10 (X20 platform):**

```
swVersion  : "EXT HPG 2.10 (b0eda3)"
hwVersion  : 000B0000                        (u32LE boardId for the X20P)
extension  : "ROM BASE 0x00A9D329"
extension  : "FWVER=HPG 2.10"
extension  : "PROTVER=50.11"
extension  : "MOD=ZED-X20P"
extension  : "GPS;GLO;GAL;BDS"
extension  : "SBAS;QZSS"
extension  : "NAVIC;LBAND"
```

Key observations:

- **The `MOD=` extension is the model identifier.** Both the F9P and the
  X20P report a clean, human-readable model name (`ZED-F9P`, `ZED-X20P`).
  This is the single most reliable field for Q1/Q3.
- **`hwVersion` is a binary board ID, not a version string.** `00190000`
  (F9P) and `000B0000` (X20P) are u32LE values. Decoding as ASCII yields
  non-printable bytes, which is why the old display showed `?`. This is the
  u-blox "boardId" — a stable hardware-revision discriminator per module.
- **`FWVER` and `PROTVER` differ by generation** (`HPG 1.x` / `27.x` for
  F9; `HPG 2.x` / `50.x` for X20). `PROTVER` is a clean *generation*
  signal: 27.x ≈ F9/M9 class, 50.x ≈ X20/M10 class.
- **The GNSS constellation extension encodes the feature set** (e.g. X20P
  adds `LBAND`/`NAVIC`). This is the "supported feature set" signal, more
  directly than any separate feature-flag poll.

> Worked example (vaio-base F9P, HPG 1.51). The operator reported the
> bring-up line `FW: HPG 1.51 HW: ?`. Based on the F9P MON-VER shape above,
> the full MON-VER for that unit would be expected to be approximately:
>
> ```
> swVersion  : "EXT HPG 1.51 (<build>)"     (or "EXT CORE ...")
> hwVersion  : 00190000                      (F9P boardId — displays as "?")
> extension  : "FWVER=HPG 1.51"
> extension  : "PROTVER=27.31"
> extension  : "MOD=ZED-F9P"
> extension  : "GPS;GLO;GAL;BDS"
> extension  : "SBAS;QZSS"
> ```
>
> The `HW: ?` was the ASCII-decoded `00190000` boardId (non-printable).
> The model *was* available in `MOD=ZED-F9P` — the old UI line just didn't
> render it. **Live re-capture on the actual 1.51 unit was not performed
> (receiver/Pi write access declined); the `MOD=`/`PROTVER=27.x`/
> `hwVersion=00190000` shape is from same-generation F9P captures.**

**Conclusion for Q1:** MON-VER *does* carry both a model string (`MOD=`)
and a hardware-revision field (`hwVersion` boardId). The app already
extracts `MOD=`; the gap is that (a) the UI surfaced only `HW:` and not
`MOD:`, and (b) the fallback chain (`_hw_model_map`, `HPG→ZED-F9P`)
assumes the F9 and does not yet know X20.

---

## Q2. Authoritative u-blox identification commands

There is **no single "receiver type / board type" enumeration command** in
the UBX protocol. Identification is composed from a small set of standard
messages. Ranked by usefulness for this app:

### (1) MON-VER (0x0A 0x0A) — primary

- **Model:** `extension` `MOD=<MODEL>` (e.g. `ZED-F9P`, `ZED-X20P`).
- **Hardware revision / board ID:** `hwVersion` (u32LE boardId).
- **Software/firmware family + version:** `swVersion` prefix +
  `extension` `FWVER=<...>` (e.g. `HPG 1.51`, `ADR 1.x`, `M10 2.x`).
- **Protocol version (generation):** `extension` `PROTVER=<maj.min>`.
- **Feature set (constellations/bands):** `extension` GNSS strings
  (`GPS;GLO;GAL;BDS`, `SBAS;QZSS`, `NAVIC;LBAND`).

This is the one command the app already polls on connect; it covers (a) and
(b) directly and (c) partially.

### (2) MON-HW (0x0A 0x0C) — hardware state (read-only)

`UBX-MON-HW` returns `boardId` (u32), `recvAntStatus`, `mainAntStatus`,
`usbReplug`, etc. Useful to **cross-check the boardId** and antenna/board
health, but it is *state*, not *model identity* — same boardId as
`hwVersion` in MON-VER. Not a model-name source. Optional hardening, not a
primary identifier.

### (3) CFG-VALGET (0x06 0x42) — config-key / feature-set capability poll

To determine the **supported config-key set** (i.e. which keys a profile
may write to), poll representative keys with a layer-0 (RAM, read-only)
`CFG-VALGET`:

- `CFG_NAV_PVT`, `CFG_POS_DYN` — present on F9/X20.
- `CFG_TMODE_MODE`, `CFG_TMODE_SVIN_*`, `CFG_TMODE_FIXED_POS_ACC` — survey-in /
  fixed-base keys the app already relies on.
- `CFG_MSGOUT_RTCM_3X_TYPE<id>_<port>` — RTCM message select (the
  `_RTCM_KEY_BASES` table in the driver).
- A model-specific "probe key" (e.g. an X20-only key) to confirm platform
  support before applying an X20 profile.

A NAK / "unknown key" response is the capability signal: if a key the
profile needs is not supported, the profile is incompatible. This is how a
profile's feature requirements can be *verified* against the live device
after (not instead of) the MON-VER model match.

### (4) No dedicated "receiver type" / "board type" enum

The u-blox documentation (Receiver Manager 5 manual, ZED-F9P Integration
Manual UBX-18010802, ZED-F9P/X20P datasheets & interface descriptions) does
**not** define a UBX command that returns a "receiver type" or "board type"
enumeration. The model is conveyed by the `MOD=` extension in MON-VER, and
the board revision by the `hwVersion` boardId. (The u-blox **JSON interface
description** repos per firmware — e.g.
`u-blox-X20-interface-description-json/HPG/ZED-X20P/u-blox-X20-HPG-2.10.json`
— enumerate the message/key set per product, but they are *host-side
reference data*, not a device query.)

**Recommendation:** use **MON-VER as the single primary identification
command** (it is already polled, is read-only, and covers model + board +
family + constellations), **optionally cross-check boardId via MON-HW**, and
**verify feature/config compatibility via a read-only CFG-VALGET probe** of
the specific keys the profile needs. Do not rely on any single "board type"
command, because none exists.

---

## Q3. Mapping identity → "hardware target"

### What a stable identifier exists

| Signal | Value (F9P) | Value (X20P) | Stability |
|---|---|---|---|
| `MOD=` (extension) | `ZED-F9P` | `ZED-X20P` | Stable per model; vendor-controlled |
| `swVersion`/`FWVER` family | `EXT HPG 1.51` | `EXT HPG 2.10` | Stable per family |
| `hwVersion` boardId (u32LE) | `00190000` | `000B0000` | Stable per board revision |
| `PROTVER` | `27.x` | `50.x` | Stable per protocol generation |
| GNSS constellation ext | `GPS;GLO;GAL;BDS`… | `GPS;GLO;GAL;BDS`+`LBAND` | Feature set |

**There is a stable, vendor-published model identifier: the `MOD=` value.**
`ZED-F9P`, `ZED-F9R`, `ZED-X20P`, etc. are the canonical model names. This
is the cleanest thing a profile's `hardware` field can match against — no
SW-version + feature-flag heuristic is required for the primary match.

### Recommended representation

Define an internal **hardware target enum** and derive it from MON-VER with
a layered fallback:

```python
class HardwareTarget(str, Enum):
    F9P  = "f9p"          # ZED-F9P  (F9 L1/L5)
    F9R  = "f9r"          # ZED-F9R  (F9 + INS)
    M9N  = "m9n"          # NEO-M9N
    X20P = "x20p"         # ZED-X20P (X20 platform)
    X20FAMILY = "x20"     # X20 platform, specific module unknown
    F9_FAM  = "f9"        # F9 platform, specific module unknown
    UBX_GEN9  = "ubx-gen9"   # generic F9/M9-class (PROTVER 2x)
    UBX_GEN10 = "ubx-gen10"  # generic X20/M10-class (PROTVER 5x)
    UNKNOWN = "unknown"
```

Resolution order (first non-empty wins):

1. **`MOD=` extension** → direct map
   `{"ZED-F9P": F9P, "ZED-F9R": F9R, "NEO-M9N": M9N, "ZED-X20P": X20P}`.
   This is the authoritative path.
2. **`hwVersion` boardId** → map known board IDs
   (`00190000→F9P`, `001B0000→F9R`, `00180000→M9N`, `000B0000→X20P`) —
   extends the existing `_hw_model_map` and adds X20P.
3. **`swVersion`/`FWVER` family prefix + `PROTVER`** → platform family
   (`HPG`+`27.x`→`F9_FAM`/`UBX_GEN9`, `HPG`+`50.x`→`X20FAMILY`/`UBX_GEN10`,
   `ADR`→`F9R`, `M10`→`UBX_GEN10`).
4. **Otherwise** → `UNKNOWN`.

Store on `DeviceInfo`:

```python
hardware_target: HardwareTarget = HardwareTarget.UNKNOWN
```

A profile's `hardware` field would then be either a specific target
(`"f9p"`, `"x20p"`) or a family token (`"f9"`, `"x20"`, `"ubx-gen9"`), and
compatibility = the device's `hardware_target` equals the profile's value
**or** the profile's value is a family the device belongs to **or** the
profile value is `"any"`/empty.

### Tradeoffs

- **Matching on `MOD=` (recommended):** clean, stable, exactly what the
  operator means by "F9P vs X20P". Risk: u-blox could in theory change the
  `MOD=` string on a new revision; mitigate by keeping the `hwVersion`
  boardId fallback (step 2) and treating unmatched `MOD=` as
  `<family>`-not-`unknown`.
- **Matching on SW-version + feature-flags only (rejected as primary):**
  fragile — `FWVER` changes with every firmware update; constellation
  strings change with feature enablement. Keep them only as *secondary*
  family/feature signals (steps 3 and the CFG-VALGET capability check).
- **Matching on `hwVersion` boardId only (rejected as primary):** opaque
  (non-printable), and a new module revision changes it; good fallback,
  bad primary.

**Bottom line:** a **stable enum derived primarily from the `MOD=` value**,
with boardId and family/PROTVER fallbacks, is the cleanest identity→
hardware-target mapping. No SW+feature-flag heuristic is needed for the
primary path.

---

## Q4. Fallback behavior when hardware can't be identified

Constraints that shape the answer:

- The app *can* always get **some** MON-VER (it's the connect handshake;
  `connect()` already treats no-MON-VER as a `TimeoutError`).
- The app *cannot* rely on `MOD=` being present: some firmware builds or
  third-party reflashed units may omit or change it.
- The app *cannot* rely on `hwVersion` being a known boardId (new hardware).
- The app *cannot* assume `PROTVER` is one of the two known majors.
- **No command reliably reports the model when the receiver is a brand-new
  / unknown u-blox**, so the fallback must handle that without failing the
  connect or silently applying the wrong profile.

Recommended fallback policy:

1. **Never gate connect on identity.** A receiver that answers MON-VER is a
   connectable u-blox; identity uncertainty must not block opening the port
   or applying a conservative default profile.
2. **Resolve to the most specific known target; else a family; else
   `UNKNOWN`.** Use the Q3 resolution order. If only `PROTVER` is known,
   return `UBX_GEN9` / `UBX_GEN10`. If nothing is known, return `UNKNOWN`.
   Keep the raw `model`/`hardware_version` strings on `DeviceInfo` so the
   UI can show what *was* actually reported (e.g. "ZED-X20P (board
   000B0000)" or "unknown (FWVER=… PROTVER=…)").
3. **Profile filtering under `UNKNOWN`:** show only profiles whose
   `hardware` is `unknown`-compatible — i.e. marked `hardware: "any"` or
   explicitly including the family/`unknown`. Do **not** default to a
   specific model's profile (e.g. don't auto-apply the F9P profile to an
   unknown unit), because applying an X20-incompatible key set to an F9 (or
   vice versa) is the dangerous case. Prefer a minimal, well-supported
   base profile (the keys every Gen9+ receiver shares: `CFG_TMODE_*`,
   `CFG_MSGOUT_RTCM_3X_*`, `CFG_NAV_PVT`) for `UNKNOWN`.
4. **Verify before applying a profile with a specific `hardware` target.**
   If the device resolved to `F9P` but the operator selects an `X20P`
   profile, run a read-only `CFG-VALGET` probe of the profile's keys first;
   if a required key NAKs / is unsupported, surface "profile not compatible
   with this receiver (missing key X)" rather than blindly writing.
5. **Surface a clear, non-blocking warning** in the UI when identity is
   `UNKNOWN` or fell back past the primary `MOD=` path: "Receiver identity
   uncertain — matching family profiles only. Update firmware or confirm
   model to unlock model-specific profiles." This turns the data point
   (`HW: ?`) into an actionable hint instead of a silent mis-identification.
6. **Persist the resolved `hardware_target` per port** so the next connect
   can pre-filter profiles and re-confirm (not re-discover) identity; a
   boardId change across connects is itself a "hardware swapped" signal.

**What the app can rely on at `UNKNOWN`:** it's a u-blox that speaks the
UBX config-DB protocol (CFG-VALSET/VALGET, MON-VER) at the negotiated
baud. It can rely on the *protocol generation* if `PROTVER` was read. It
cannot rely on a specific model, a specific board revision, or a specific
feature set.

**What it cannot rely on:** the model name, the boardId, the exact feature
set, or that a particular profile's keys exist. Hence: family-level or
`any` profiles only, plus a pre-apply `CFG-VALGET` capability check for
anything more specific.

---

## Evidence / sources

- Driver source (read in full): `src/sp_rtk_base/services/drivers/ublox.py`
  — `_poll_mon_ver()` (line 1588), `connect()` (line 166),
  `_hw_model_map` (line 1609), `UBXMessage.config_poll` usage (line 1505).
- `DeviceInfo` model: `src/sp_rtk_base/models/device_models.py` (line 58).
- u-blox MON-VER message definition (`ublox_msgs::MonVER`, UBX-13003228):
  `swVersion char[30]`, `hwVersion char[10]`, repeated `extension char[30]`.
- F9P MON-VER capture (HPG 1.13, same `MOD=`/`PROTVER=27.x`/
  `hwVersion=00190000` shape as the 1.51 unit under study): u-blox
  community (ZED-F9P MON-RF thread) — `hwVersion 00190000`,
  `FWVER=HPG 1.13`, `PROTVER=27.12`, `MOD=ZED-F9P`, `GPS;GLO;GAL;BDS`,
  `SBAS;QZSS`.
- X20P MON-VER capture (HPG 2.10): blog.mayer.tv (2026-03-09) —
  `swVersion EXT HPG 2.10 (b0eda3)`, `hwVersion 000B0000`,
  `FWVER=HPG 2.10`, `PROTVER=50.11`, `MOD=ZED-X20P`, `NAVIC;LBAND`.
- u-blox X20 interface-description JSON (host-side reference of the
  per-product message/key set):
  `u-blox/u-blox-X20-interface-description-json/HPG/ZED-X20P/...`.

## Caveats

- The vaio-base F9P (HPG 1.51) was **not live-probed** (receiver and Pi
  write access declined). Its MON-VER is reconstructed from same-generation
  F9P captures; the `MOD=ZED-F9P` / `PROTVER=27.x` / `hwVersion=00190000`
  shape is expected to hold, but a live `MOD=` capture on the exact 1.51
  unit is recommended before shipping the profile filter to confirm the
  `MOD=` string and the exact PROTVER.
- `hwVersion` boardId values cited are from community captures; treat the
  boardId→model map as *observed*, not vendor-guaranteed, and keep it as a
  fallback only.
