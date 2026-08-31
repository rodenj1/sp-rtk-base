# Research: apply-config drops elevation_mask_deg / bds_b2_enabled / spi_enabled / BeiDou — is it a firmware design change or a code bug?

> **Resolved: 2026-08-31** · Ticket [#82](https://github.com/rodenj1/sp-rtk-base/issues/82) · Map [#43](https://github.com/rodenj1/sp-rtk-base/issues/43) · Type: research (AFK)
> **Verdict: NOT a firmware design change, and not a missing write in the apply/driver path — for three of the four fields it is a deliberate UI-layer scoping decision.** The UI's `_apply()` builds the `ReceiverConfig` it POSTs *without the hardware "extras"* (elevation mask, BDS B2, SPI, constellations), so those profile values are never sent to the receiver even though the service and driver fully implement their writes (with read-back verify). The BeiDou constellation is a separate, genuine driver-capability limitation: `configure_gnss` can only toggle blocks the firmware already reports, so it cannot add a BeiDou block the device does not enumerate. Nothing here is firmware-version-caused: the unit runs the newest firmware (HPG 1.51) and all three VALSET keys are documented, unchanged, and settable in it.

---

## 1. Question (from the ticket)

Live audit of vaio-base (2026-08-31, HPG 1.51, dev main @ `f7680d9`) after applying the built-in
`ublox-f9p-base-standard` profile + re-survey shows four keys at **factory defaults** instead of the
profile's targets:

| Key | Profile target | Live vaio |
|---|---|---|
| `CFG_NAVSPG_INFIL_MINELEV` | 15 (`elevation_mask_deg: 15`) | 10 (factory) |
| `CFG_SIGNAL_BDS_B2_ENA` | 0 (`bds_b2_enabled: false`) | 1 (factory) |
| `CFG_SPI_ENABLED` | 1 (`spi_enabled: true`) | 0 (factory) |
| Constellations (BeiDou) | all 6 incl. `beidou` | no BeiDou block present (GPS/SBAS/Galileo/GLONASS/IMES/QZSS) |

Everything else on the unit verified at layer=0 (flash): TMODE=2 fixed + valid ECEF + SVIN,
DYN_MODEL=2, RTCM-only port profile on UART1/UART2, baud 57600/115200, full RTCM matrix,
84 valid RTCM3 frames on the wire. The operator's read: "not sure it is actually a bug yet — more a
research task."

The question this research settles: for each of the four, is it (a) a missing write in the apply
path, (b) a firmware design change/limitation, or (c) a live-block reconciliation gap — and what is
the correct fix?

---

## 2. The three optimisation fields (elevation / BDS B2 / SPI): dropped at the UI layer, not the driver

### 2.1 The writes DO exist in the service and driver — with read-back verify

`DeviceService.apply_receiver_config` (device_service.py, `main @ f7680d9`) runs the sequence
documented in its docstring — *guards → measurement rate → ports → constellations →
**optimisations** → role fields → RTCM matrix → baud → reopen → read-back verify* — and calls:

```python
await asyncio.to_thread(
    driver.configure_optimisations,
    config.elevation_mask_deg,
    config.bds_b2_enabled,
    config.spi_enabled,
)
```

`UbloxDriver.configure_optimisations` (ublox.py) writes each non-`None` field as a `CFG-VALSET` at
**layer=5** (RAM+Flash) through `_write_and_verify_locked` — the same write-then-`CFG-VALGET`
read-back-and-retry-once helper that guards every other durable write:

```python
cfg_data: list[tuple[str, int]] = []
if elevation_mask_deg is not None:
    cfg_data.append(("CFG_NAVSPG_INFIL_MINELEV", elevation_mask_deg))
if bds_b2_enabled is not None:
    cfg_data.append(("CFG_SIGNAL_BDS_B2_ENA", int(bds_b2_enabled)))
if spi_enabled is not None:
    cfg_data.append(("CFG_SPI_ENABLED", int(spi_enabled)))
...
self._write_and_verify_locked(cfg_data, layer=5, label="Optimisation settings")
```

Unit tests (`tests/unit/test_ublox_driver.py`) assert all three keys are emitted and read back. So
hypothesis 1 as framed in the ticket ("no `CFG-VALSET` exists anywhere in the apply path") is
**refuted** — the write exists, has been since the apply-config endpoint shipped (`c6157bf`, #61),
and is self-verifying.

### 2.2 What actually drops them: `_apply()` never puts them in the request

The UI's Apply button handler (`gps_config.py::_apply`) builds the config **without**
`form_extras`:

```python
# gps_config.py — _apply()
config = build_apply_config(form_matrix, form_data_link_ports)   # no extras arg
result = await svc.apply_receiver_config(config)
```

`build_apply_config` defaults `extras` to an all-`None` `FormExtras` when none is passed:

```python
def build_apply_config(matrix, data_link_port, extras: FormExtras | None = None) -> ReceiverConfig:
    """*extras* defaults to an all-``None`` :class:`FormExtras` — nothing
    but the matrix and data-link ports set. ``_apply()`` always uses that
    default (Apply stays scoped to the matrix + data-link ports, per #65
    — the other fields have no live read-back to verify a write against).
    ..."""
    extras = extras or FormExtras()
```

`_apply()`'s own docstring is explicit about this being deliberate:

> "Deliberately excludes ``form_extras`` (issue #66 review): those fields have no live
> read-back, so a profile-populated value pushed through Apply could never be verified by the
> read-back-diff below, unlike the matrix. Extending Apply to the full hardware section is out
> of #66's scope …"

`git log -L` confirms: `build_apply_config(form_matrix, form_data_link_ports)` (no extras) has been
the exact call at `_apply()` since `eec4d6f` (the #64 live-seeded form) and was **re-asserted**
in `4984281` (#66). The only call site that *does* pass extras is `_current_form_config()`
(line ~1153), which feeds the in-form "modified from profile" comparison — never the receiver.

**Consequence chain (all at the app layer):**

1. Operator picks the built-in profile (or leaves the form pre-filled with its values) and hits **Apply**.
2. The POSTed `ReceiverConfig` has `elevation_mask_deg / bds_b2_enabled / spi_enabled = None`
   ("leave untouched" semantics), because `FormExtras()` was used.
3. The service sees all-`None` and `configure_optimisations` builds an **empty** `cfg_data` and
   returns without writing anything.
4. The read-back verify that *would* catch a dropped field (hypothesis 3 in the ticket) is moot:
   the endpoint-level verify only covers the RTCM matrix anyway, and the per-write
   `_write_and_verify_locked` only runs when there is something to write.
5. Result: `status="ok"` ("Applied and verified ✓") while the three keys sit at factory defaults —
   exactly what the live audit observed.

**This also explains the "no error, clean success" observation:** the write path is
verify-and-raise (`_write_and_verify_locked` raises `RuntimeError` on a mismatched read-back; the
service would propagate it and the UI would show "Apply failed"). If the writes *had* been sent and
the firmware had refused them, the operator would have seen a failure — not a silent success. The
absence of an error is therefore positive evidence that nothing was written, consistent with the
UI-layer drop, and independent evidence that the firmware side is not refusing anything.

### 2.3 Root cause, classified

- **Mechanism:** UI scoping decision (#65/#66) — Apply is "only the RTCM matrix and the
  data-link ports." The three optimisation fields (and `constellations`, `ports`, `baud`,
  `dyn_model`, `tmode_mode`) are *displayed* and *used for Save-as / modified-from-profile
  comparison* but never pushed by the Apply button.
- **Defect or expected?** As-implemented behavior is expected (documented in-code as deliberate).
  As a *profile experience* it is a gap: the built-in profile **asserts all three fields**
  (`ublox-f9p-base-standard.yaml`: `elevation_mask_deg: 15`, `bds_b2_enabled: false`,
  `spi_enabled: true`), the map's destination says "pick the profile → apply → correctly configured
  base station," and the T1 spec's apply sequence includes the optimisation step. So the profile
  contract and the UI's Apply scope disagree.
- **Why it is NOT a firmware issue (all three fields):** the u-blox **F9 HPG 1.51 Interface
  Description (UBXDOC-963802114-13124, R01, 08-Nov-2024)** — the exact firmware on vaio-base
  (HPG 1.51, PROTVER 27.50) — lists all three keys in its configuration database with factory
  defaults matching the live audit:
  - `CFG-NAVSPG-INFIL_MINELEV` `0x201100a4` `I1`, unit **deg**, default **10** (§6 config DB +
    defaults table)
  - `CFG-SIGNAL-BDS_B2_ENA` `0x1031000e` `L` ("BeiDou B2I"), default **1 (true)**
  - `CFG-SPI-ENABLED` `0x10640006` `L` ("Flag to indicate if the SPI interface should be [enabled]"),
    default **0 (false)**

  And the **ZED-F9P FW 1.00 HPG 1.51 Release Note (UBXDOC-963802114-13110)** reports **no** new,
  modified, or removed configuration items vs HPG 1.50 (all three 3.5.x tables are empty) — and the
  HPG 1.50 RN's new/modified lists likewise never touch these keys. There is no firmware
  version-to-version behavior change that would explain the drops.

### 2.4 Recommendation (elevation / BDS B2 / SPI)

**(a) Push the extras through Apply.** Pass `form_extras` into `_apply()`'s
`build_apply_config` call (one-line change). The service/driver plumbing already exists and is
self-verifying end to end. This is the natural completion of the #65/#66 scope that #82's live
validation has now exposed.

**(b) Revisit the "no live read-back" rationale that drove the scoping.** It is outdated for these
three fields: `configure_optimisations` already does a per-write `CFG-VALGET` read-back with retry
(`_write_and_verify_locked`), so pushing them adds *more* verified writes, not unverifiable ones.
If the endpoint-level result should surface them too, extend `ApplyConfigResult`'s read-back to the
applied optimisation keys (mirror of what `_write_and_verify_locked` already does internally) so a
firmware refusal is reported as a per-key diff instead of a raised exception.

**(c) Close the silent-success gap either way** (ticket hypothesis 3, confirmed real): a successful
apply should be able to say *which* profile fields were actually applied. At minimum, when the
operator applies a *profile* (not a transient matrix edit), the UI should make it visible that
non-matrix fields are/weren't part of the write — "out of sync" currently only tracks the RTCM
matrix (`form_matrix != live_matrix`), so the three extras have no sync indicator at all.

**Open unit question (separate, do not guess):** what does the profile's `elevation_mask_deg: 15`
mean on the wire? The HPG 1.51 IFD declares the key's unit as `deg` (default 10 = 10°), which is
consistent with the live factory value (10) and with `docs/zed-f9p-base-station-config-reference.md`
("15°"). But `01 - Reference vs VAI0-Base Config Comparison.md` (2026-08-25) annotates the
reference receiver's raw value as "15 (0.1° → 1.5°)" — a 10× discrepancy between two repo docs
about the *same key on the same hardware class*. The HPG 1.51 IFD's own defaults table (default=10,
live factory=10) favors "deg," but the 1.12-era doc predates it and its annotation should be
checked against the HPG 1.12 IFD before the profile's target value is trusted cell-for-cell against
the reference. (Live probe of one `MINELEV=15` write + NAV-satellite behavior would settle it in
minutes.)

---

## 3. BeiDou constellation: a genuine driver-capability limitation (the reconciliation gap)

### 3.1 The mechanism, confirmed in code

`DeviceService.apply_receiver_config` handles constellations by **reconciling the profile's wanted
set against the live CFG-GNSS blocks** — it only flips the `enabled` flag on blocks the receiver
currently reports:

```python
if config.constellations is not None:
    current_gnss = await asyncio.to_thread(driver.get_gnss_config)   # live CFG-GNSS POLL
    wanted = set(config.constellations)
    updated_gnss = GnssConfig(systems=[
        system.model_copy(update={"enabled": system.constellation in wanted})
        for system in current_gnss.systems                            # ← live blocks only
    ])
    await asyncio.to_thread(driver.configure_gnss, updated_gnss)
```

`_parse_cfg_gnss` skips blocks whose `gnssId` is not in the app's 6-constellation map (GPS=0,
SBAS=1, GAL=2, BDS=3, QZSS=5, GLO=6 — no NavIC/IMES=7), and `configure_gnss` emits exactly the
blocks it was given, with per-block `maxTrkCh`/`resTrkCh` preserved from the live read. So the
write **cannot add a BeiDou block the device does not currently enumerate** — and on vaio-base the
live enumeration is GPS/SBAS/Galileo/GLONASS/**IMES**/QZSS: no BeiDou block. Nothing for the
reconciliation to toggle. (Conversely the IMES/NavIC block, which the profile doesn't mention, is
silently dropped from the write — same one-way asymmetry.)

So for BeiDou the ticket's hypothesis 2 is **confirmed**: it is the live-block-reconciliation gap,
not a missing key write.

### 3.2 "IMES" is NavIC — and its presence is a firmware capability question, not a bug

`IMES` is pyubx2's label for the NavIC constellation (`pyubx2/ubxhelpers.py` MON-VER parser;
`gnssId 7`; config key `CFG_SIGNAL_IMES_ENA` `0x10310023`), and the F9 HPG 1.51 IFD documents
NavIC (gnssId 7, NMEA 4.11 system ID 6) in its satellite numbering tables. A ZED-F9P enumerating
NavIC in CFG-GNSS is normal F9-family behavior — the vendor's CFG-GNSS documentation note says a
poll "returns the configuration of all supported GNSS, whether enabled or not; it may also include
GNSS unsupported by the particular product, in which case the enable flag will always be unset."

**But** the same RN/IFD pair states the HPG 1.51 supported constellation set as
*GPS, GLONASS, Galileo, BeiDou (B1I/B2I), QZSS* (+ SBAS) — i.e. the vendor documentation says this
firmware's set **includes BeiDou**, while the live vaio unit's block enumeration **includes NavIC
and omits BeiDou**. The CFG-GNSS block set is firmware-variant-dependent (which constellations that
firmware image can configure), so the live enumeration is the ground truth for *this* unit's
firmware build. This is exactly the kind of "older firmware vs newest" surface the operator asked
about — but note it would be a difference *between firmware builds/images*, not a regression on
vaio-base: the unit already runs the newest (1.51), the reference receiver runs the same 1.51, and
neither can gain a BeiDou block via the current code path.

**Live verification still owed (cheap, on the unit):** confirm the raw CFG-GNSS POLL on vaio-base
lists no `gnssId=3` block (vs. an enabled-0 block the app's parser dropped), and note the exact
`numTrkChHw` — this distinguishes "firmware build doesn't report a BDS block" from "parser dropped
it." Either way, the app-side fix below applies.

### 3.3 Recommendation (BeiDou / constellations in general)

**(c) Make the constellation write assertive via the config DB, not block-reconciliation.** The F9
family is "relying exclusively on the configuration interface using UBX-CFG-VALSET/VALGET/VALDEL"
(HPG 1.51 RN §3.1; CFG-GNSS SET is *deprecated* in protocol versions > 23.01, with the IFD's
crosswalk mapping it to `CFG-SIGNAL-GPS_ENA / -SBAS_ENA / -BDS_ENA / -QZSS_ENA / -GLO_ENA`), and
every per-constellation enable key is documented in the HPG 1.51 IFD config DB (e.g.
`CFG-SIGNAL-BDS_ENA` `0x10310022`, default 1). Writing the wanted set through `CFG-VALSET`
(e.g. `CFG-SIGNAL-BDS_ENA=1` + the existing per-signal `CFG-SIGNAL-BDS_B1_ENA`/`B2_ENA`) can
**enable a constellation the block list didn't enumerate** (and disable IMES/NavIC if the profile
says so), and each write gets the existing read-back verify. Keep the CFG-GNSS block read as the
*capability probe* (what the hardware/firmware can do), but stop using it as the *write boundary*.

Also: reconcile the profile's constellation list against **device capability** explicitly — report
"BeiDou not supported by this unit's firmware" when the enable write NAKs or the key is absent
from a `CFG-VALGET` capability probe, instead of silently no-op'ing.

---

## 4. The verify gap (ticket hypothesis 3) — confirmed, and broader than stated

The apply-config endpoint's read-back verify covers **only the RTCM matrix**
(`device_service.py`: "a final read-back of the full RTCM matrix decides `status`"). Three
consequences, all confirmed:

1. **Dropped fields sail through "success."** Elevation/BDS/SPI (and the other extras) are not in
   any endpoint-level read-back — today they are not even sent, and a `status="ok"` says nothing
   about them.
2. **The UI's "out of sync" indicator only tracks the RTCM matrix** (`form_matrix != live_matrix`)
   — the extras have no live-state comparison at all, so the operator has no in-UI signal that the
   profile's hardware-section values were never applied.
3. **Per-write verify exists but is only as good as its inputs.** `_write_and_verify_locked`
   (used by `configure_optimisations` and all role fields) is a real write-then-read-back with
   retry — good. But it only runs when the field is non-`None`, which today never happens from the
   UI Apply path.

**Recommendation:** extend the endpoint-level read-back (and the UI sync state) to all applied
fields — the driver primitives already exist; this is a coverage change, not a mechanism change.
This turns "applied ✓" into an honest claim and makes firmware-level refusals (e.g. a NAK on
`CFG-SIGNAL-BDS_ENA`) surface as diffs instead of exceptions.

---

## 5. Direct answer to the operator's question

**"Is this a difference between the older u-blox F9P firmware vs the newest, or a real code bug?"**

- **Not a firmware design change.** The unit runs the newest firmware (HPG 1.51, the current
  release); all three optimisation keys are documented, settable, and unchanged between HPG 1.50
  and 1.51 per the vendor release notes; and the writes would have *failed loudly* (verify-and-raise)
  if the firmware refused them. The clean "Applied and verified ✓" is itself the fingerprint of
  "nothing was sent," not of "the firmware dropped it."
- **Code — but mostly a *scope* decision, with one real capability gap:**
  - Elevation mask / BDS B2 / SPI: the service + driver **do** write and verify them; the UI's
    Apply button **doesn't send them** (deliberate #65/#66 scoping, since `eec4d6f`, re-asserted in
    `4984281`). Fix = pass `form_extras` through `_apply()` + extend the read-back/coverage story
    (§2.4).
  - BeiDou: the apply path's block-reconciliation **cannot add a constellation block the firmware
    doesn't enumerate**, and this unit's firmware enumeration has no BeiDou block (it has IMES/NavIC
    instead). Fix = assertive `CFG-VALSET` on the per-constellation enable keys + explicit
    capability reconciliation (§3.3).
- **The one genuinely firmware-flavored item** is the *block-enumeration* difference itself
  (vendor doc says HPG 1.51 includes BeiDou; this unit's live set includes NavIC and not BeiDou).
  That is a property of the firmware image the unit was flashed with, to be confirmed by a live
  CFG-GNSS POLL raw read (§3.2) — and regardless of its explanation, it changes the *fix*, which is
  app-side.

## 6. Sources

- sp-rtk-base `main @ f7680d9`: `src/sp_rtk_base/ui/pages/gps_config.py` (`_apply`, `build_apply_config`,
  `FormExtras`, `_current_form_config`), `src/sp_rtk_base/services/device_service.py`
  (`apply_receiver_config`), `src/sp_rtk_base/services/drivers/ublox.py`
  (`configure_optimisations`, `_write_and_verify_locked`, `configure_gnss`, `_parse_cfg_gnss`),
  `src/sp_rtk_base/profiles/builtin/ublox-f9p-base-standard.yaml`,
  `tests/unit/test_ublox_driver.py`; git history of the `_apply` call site (`eec4d6f` → `4984281`).
- u-blox F9 HPG 1.51 Interface Description, UBXDOC-963802114-13124 R01 (08-Nov-2024): config DB keys
  + defaults (MINELEV/BDS_B2/SPI_ENABLE), CFG-GNSS spec + deprecation note + crosswalk, satellite
  numbering tables (gnssId incl. NavIC 7).
- u-blox ZED-F9P FW 1.00 HPG 1.51 Release Note, UBXDOC-963802114-13110 R01: supported constellations
  (BeiDou B1I/B2I), §3.1 config-interface statement, empty interface-change tables.
- u-blox ZED-F9P FW 1.00 HPG 1.50 Release Note, UBXDOC-963802114-12826: new/modified item lists
  (none of the three keys).
- Repo docs: `docs/zed-f9p-base-station-config-reference.md` (MINELEV "deg" interpretation),
  Obsidian VAIO project notes (`01 - Reference vs VAI0-Base Config Comparison.md` — the 0.1°
  annotation; HPG 1.51 / PROTVER 27.50 / HW 00190000 on vaio-base).
- Corroboration that F9P firmware version changes *can* alter config behavior (context, not the
  mechanism here): SparkFun Community — Facet RTK FW 4.1 + ZED-F9P 1.32→1.51 update broke message
  rate configuration (required downgrade to 1.50).
