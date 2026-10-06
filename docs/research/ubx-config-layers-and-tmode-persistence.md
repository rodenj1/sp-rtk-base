# Research: ZED-F9P configuration layers, and how to persist a TMODE fixed base across resets

> **Researched: 2026-10-06** · Type: research (AFK) · Code read at `main @ 21b3299`
> **Verdict: the hypothesis is confirmed by the u-blox docs.** BBR outranks Flash when the RAM layer
> is rebuilt, so the `CFG_TMODE_MODE=0` that the app's "disable" step leaves in BBR overrides the
> `CFG_TMODE_MODE=2` that `configure_fixed_base` writes to Flash. After any reset that keeps BBR
> alive (every `UBX-CFG-RST` that reloads config, and a power cycle with a backup supply on
> V_BCKP), the base comes up with TMODE disabled. A power cycle *without* a backup supply wipes
> BBR and the base comes up fixed, which is why the bug looks intermittent.
>
> **A second bug makes it worse:** `save_to_flash()` passes `deviceMask=b"\x17"` to pyubx2, which
> has no field called `deviceMask` for CFG-CFG. pyubx2 drops the argument and sends
> `deviceMask=0x00`, so the "save" selects no memory device at all. The u-blox spec only defines
> the behaviour when the byte is *absent*. This probably explains the old "CFG-CFG does not reliably
> persist TMODE" comment in `configure_fixed_base`.
>
> **Fix:** keep TMODE out of BBR completely. Do the pre-disable in RAM only, `CFG-VALDEL` the TMODE
> keys from BBR, write the fixed config to RAM+Flash (layer 5), and make `save_to_flash` save to
> Flash only (or remove it). See [§8](#8-recommended-write-sequence).

---

## 1. Question

Bench: `test-base.lan`, ZED-F9P, `EXT CORE 1.00 (9e1716)`. That is **HPG 1.51** (protocol 27.50),
per the u-blox HPG 1.51 release note [RN151 §1.2, p.4]. After the app's fixed-base flow,
`CFG-VALGET` shows:

| Layer (VALGET enum) | `CFG_TMODE_MODE` | Position keys |
|---|---|---|
| RAM (0) | 2 | present |
| BBR (1) | **0** | absent |
| Flash (2) | 2 | present |
| Default (7) | 0 | (defaults) |

`UbloxDriver.configure_fixed_base` (`src/sp_rtk_base/services/drivers/ublox.py:916-1010`) first
writes `CFG_TMODE_MODE=0` to VALSET layers `7` (RAM|BBR|Flash, `_TMODE_DISABLE_ALL_LAYERS`,
`:670`), sleeps 0.5 s, and then writes the full fixed config to layer `5` (RAM|Flash, `:977`).
Does BBR's `0` beat Flash's `2` on reload, and what is the correct write sequence?

## 2. Sources

All are first-party u-blox documents, downloaded and read on 2026-10-06.

- **[ID151]** u-blox F9 HPG 1.51 Interface description, UBXDOC-963802114-13124 R01 (08-Nov-2024).
  This matches the bench firmware.
  <https://content.u-blox.com/sites/default/files/documents/u-blox-F9-HPG-1.51_InterfaceDescription_UBXDOC-963802114-13124.pdf>
- **[ID132]** u-blox F9 HPG 1.32 Interface Description, UBX-22008968 R01 (02-May-2022). Used to
  check that the wording below is unchanged between 1.32 and 1.51, and it is (the 1.51 CFG-CFG
  comment adds one ACK/NAK paragraph).
  <https://content.u-blox.com/sites/default/files/documents/u-blox-F9-HPG-1.32_InterfaceDescription_UBX-22008968.pdf>
- **[IM]** ZED-F9P Integration manual, UBX-18010802 R16 (30-Oct-2024). It covers HPG 1.13
  through 1.51.
  <https://content.u-blox.com/sites/default/files/ZED-F9P_IntegrationManual_UBX-18010802.pdf>
- **[RN151]** ZED-F9P FW 1.00 HPG 1.51 Release note, UBXDOC-963802114-13110 R01.
  <https://content.u-blox.com/sites/default/files/documents/ZED-F9P-FW100HPG151_RN_UBXDOC-963802114-13110.pdf>
- **[C099]** u-blox C099-F9P board package config scripts, `u-blox/ublox-C099_F9P-uCS @ 40fccd2`
  (2020-06-05), directory `zed-f9p/`:
  [`F9P Base Survey in disable.txt`](https://github.com/u-blox/ublox-C099_F9P-uCS/blob/40fccd297643a6a85a65e6f7c3198ef58cb3b088/zed-f9p/F9P%20Base%20Survey%20in%20disable.txt),
  [`F9P Base Survey in start.txt`](https://github.com/u-blox/ublox-C099_F9P-uCS/blob/40fccd297643a6a85a65e6f7c3198ef58cb3b088/zed-f9p/F9P%20Base%20Survey%20in%20start.txt),
  [`F9P Base config C99.txt`](https://github.com/u-blox/ublox-C099_F9P-uCS/blob/40fccd297643a6a85a65e6f7c3198ef58cb3b088/zed-f9p/F9P%20Base%20config%20C99.txt),
  [README](https://github.com/u-blox/ublox-C099_F9P-uCS/blob/40fccd297643a6a85a65e6f7c3198ef58cb3b088/README.md).
- pyubx2 1.3.0 (the version in this repo's venv). I ran it locally to see the exact bytes the
  driver sends. pyubx2 is not a u-blox source, but here it is the code that produces the wire bytes.

---

## 3. Q1: the layer model and how RAM is rebuilt

**Priority, highest first: RAM > BBR > Flash > Default.** [ID151 §6.3, p.243]:

> "Layers are organized in terms of priority. Values in a high-priority layer replace values stored
> in a low-priority layer. At startup, the receiver reads all configuration layers and stacks up the
> items to create the current configuration … (in order of priority, highest priority first):
> RAM … BBR … Flash … Default."

The worked example settles the BBR-vs-Flash case directly [ID151 §6.3, p.244]:

> "The third item is present in the Default, flash and BBR layers. **The value from the BBR layer
> has the highest priority and therefore it ends up in the RAM layer.**"

Each item is resolved separately: "for each item in the default layer, the receiver software goes
through the layers above and stacks all the found items on top … given configuration values coming
from the highest priority layer the corresponding item was present" [ID151 §6.3, p.243-244]. So
on the bench, `CFG_TMODE_MODE` resolves from BBR (0), while the position keys, which are absent
from BBR, resolve from Flash. The result is a RAM layer holding a valid position with
`MODE=DISABLED`.

**When RAM is rebuilt.** [ID151 §6.7, p.247]:

> "The RAM layer is always rebuilt from the layers below when the chip's processor comes out from
> reset. When using UBX-CFG-RST the processor goes through a reset cycle with these reset types
> (resetMode field): 0x00 hardware reset (watchdog) immediately · 0x01 controlled software reset ·
> 0x04 hardware reset (watchdog) after shutdown."

[IM §3.16, p.82] gives the behaviour of each resetMode:

| resetMode | Meaning [ID151 UBX-CFG-RST, p.88-89] | Rebuilds RAM from BBR/Flash/Default? |
|---|---|---|
| `0x00` | Hardware reset (watchdog) immediately | **Yes** ([ID151 §6.7]). "Immediate, asynchronous reset. No Stop event is generated." [IM §3.16] |
| `0x01` | Controlled software reset | **Yes**: "restarts operation, reloads its configuration" [IM §3.16]; listed in [ID151 §6.7] |
| `0x02` | Controlled software reset (GNSS only) | **No**: "only restarts the GNSS tasks, without reinitializing the full system or reloading any stored configuration" [IM §3.16] |
| `0x04` | Hardware reset (watchdog) after shutdown | **Yes** ([ID151 §6.7]) |
| `0x08` | Controlled GNSS stop | No: "will not be restarted, but will stop any GNSS-related processing" [IM §3.16] |
| `0x09` | Controlled GNSS start | No: "starts all GNSS tasks" [IM §3.16]. Not listed in §6.7, so no config reload is documented |

[ID151 §1.3, p.17] says the same from the run-time side: changes made only to RAM "will be lost when
there is a power cycle, a hardware reset or a (complete) controlled software reset".

Two related points:

- A layer write to BBR or Flash does **not** change the running config. BBR and Flash items "become
  effective when the receiver is restarted" [ID151 §6.3]. RAM can also be rebuilt without a reset by
  sending UBX-CFG-CFG with any `loadMask` bit set: "The current configuration is discarded and
  rebuilt from all the lower layers" [ID151 UBX-CFG-CFG, p.65].
- The `navBbrMask` field of CFG-RST (eph, alm, pos, …, `0xFFFF` = cold start) clears *navigation*
  sections of BBR [ID151 p.88]. None of its bits is documented as clearing the BBR
  *configuration layer*. A cold start is therefore not a documented way to clear BBR config.

## 4. Q2: when BBR survives

- **The BBR layer holds configuration, not only nav data.** "BBR: This layer contains items stored
  in the battery-backed RAM. The contents in this layer are preserved as long as a battery backup
  supply is provided during off periods" [ID151 §6.3]. "If stored in BBR (battery-backed RAM), the
  setting will be used as long as the backup battery supply remains" [IM §3.1, p.12]. BBR "will
  maintain all MGA related information plus any user configuration" [IM §3.11.2, p.59].
- **Power cycle with V_BCKP supplied:** BBR survives. "The V_BCKP pin can be used to provide power to
  maintain the real-time clock (RTC) and battery-backed RAM (BBR) when VCC is removed" [IM §4.2.2,
  p.88]. "If VCC is removed while a battery is connected to V_BCKP, most of the receiver is switched
  off leaving the RTC and BBR powered" (Hardware Backup Mode) [IM §4.2.2, p.89].
- **Power cycle without V_BCKP:** BBR is lost. "If V_BCKP is not provided, the module performs a cold
  start at power up" [IM §4.2.2, p.88]. If the board ties V_BCKP to VCC ("If no backup supply
  voltage is available, connect the V_BCKP pin to VCC" [IM §4.2.2]), BBR is lost on every power
  cycle.
- **Software resets (`0x01`) and watchdog resets (`0x00`, `0x04`):** VCC stays up, so BBR is
  retained, and the RAM rebuild after the reset reads it. *The docs do not say this in one
  sentence.* It follows from (a) BBR being lost only when it loses power, (b) [ID151 §6.7]
  rebuilding RAM "from the layers below" after these resets, and (c) CFG-RST having a separate
  `navBbrMask` for choosing which BBR sections to clear, which would make no sense if a reset
  cleared BBR anyway. The repo's own bench history agrees: `reset_and_reconnect`'s docstring
  (`ublox.py:531-566`) records the BBR-backed NAV-SVIN `dur` accumulator surviving several reset
  variants.
- Config lock gives a matching statement: a lock "set on a configuration layer in volatile memory
  (RAM, BBR) is removed when the memory is cleared", and flash is "permanent" [IM §3.14.4.3, p.69].

**So for the bench state (BBR MODE=0, Flash MODE=2):**

| Event | BBR | Base comes up as |
|---|---|---|
| CFG-RST `0x01` controlled software reset | kept | **TMODE disabled** (BBR 0 wins) |
| CFG-RST `0x00` / `0x04` watchdog reset | kept | **TMODE disabled** |
| CFG-RST `0x02` / `0x08` / `0x09` | kept | unchanged (RAM not rebuilt): still fixed |
| Power cycle, backup supply on V_BCKP | kept | **TMODE disabled** |
| Power cycle, no backup supply | lost | Fixed (Flash 2 wins) |

## 5. Q3: CFG-VALSET layer bits, and CFG-VALDEL

**CFG-VALSET `layers` is a bitmask** [ID151 UBX-CFG-VALSET, p.98]: `bit 0 ram`, `bit 1 bbr`,
`bit 2 flash`. So 1 = RAM, 2 = BBR, 4 = Flash, 5 = RAM+Flash, 7 = all three. Notes:

- The write is NAK'd "if the layer's bitfield does not specify a layer to save a value to".
- "The validity of a configuration is checked only if the message requests to apply the
  configuration to the RAM configuration layer." A BBR-only or Flash-only write is not validated.
- At most 64 key/value pairs per message. Version 1 of the message supports transactions.

**CFG-VALGET `layer` is an enum, not a bitmask**: 0 = RAM, 1 = BBR, 2 = Flash, 7 = Default
[ID151 UBX-CFG-VALGET, p.96-97]. The driver's comment at `ublox.py:672-680` already gets this
right. VALGET returns only one layer per request: "It is not possible to retrieve configuration
values for the same configuration item from multiple configuration layers."

**CFG-VALDEL is the documented way to remove a key from BBR or Flash** [ID151 UBX-CFG-VALDEL,
p.95]:

> "This message can be used to delete saved configuration to effectively revert the item values to
> defaults. This message can delete saved configuration from the flash configuration layer and the
> BBR configuration layer. The changes will not be effective until these layers are loaded into the
> RAM layer."

- Its `layers` field has only `bit 1 bbr` and `bit 2 flash`. There is no RAM bit, so RAM cannot be
  deleted from. Use `layers=2` for BBR.
- "Attempting to delete items that have not been set before, or that have already been deleted, is
  considered a valid request." This means deleting from BBR on every fixed-base write is safe to
  repeat and needs no check first.
- Up to 64 keys per message. It is NAK'd if any key is unknown. Version 1 adds transactions.
- "effectively revert the item values to defaults" is a simplification. Under the stacking rule in
  §6.3, deleting a key from BBR exposes the **next lower layer that holds it**: Flash if present,
  otherwise Default. Deleting `CFG_TMODE_MODE` from BBR on the bench unit would therefore expose
  Flash's `2`.
- VALDEL documents no wildcards (only VALGET has group wildcards), so list the TMODE keys
  explicitly: `CFG-TMODE-*`, 17 keys, `0x20030001`…`0x40030011` [ID151 §6.9.28, p.288-289].
- pyubx2 provides it: `UBXMessage.config_del(layers=2, transaction=0, keys=[...])`.

**Legacy CFG-CFG `clearMask`** also clears a layer, but only all of it: "if any bit is set in the
clearMask: all configuration in the selected non-volatile memory is deleted" [ID151 UBX-CFG-CFG,
p.65]. With `devBBR` alone it would wipe every BBR config item, not only TMODE. VALDEL is the
targeted tool, and u-blox says to use it instead: "Use UBX-CFG-VALSET and UBX-CFG-VALDEL with the
appropriate layers instead."

## 6. Q4: is TMODE edge-triggered, and should it be written to all layers?

**Edge-triggering is not documented.** Neither [ID151] nor [IM] says that `CFG-TMODE-MODE` has to
pass through `DISABLED` before a new `SURVEY_IN`/`FIXED` takes effect. The CFG-TMODE table
[ID151 §6.9.28] and the base-station sections [IM §3.1.5.5.1-2, p.20-21] list the keys and
nothing about ordering. The behaviour the driver relies on (0 → wait → 1/2) is the repo's own
bench observation, recorded at `ublox.py:650-656` and `:954-962`. It is reasonable to keep, but
treat it as empirical. **What the edge needs is a RAM transition.** The running config is RAM, and
BBR or Flash writes do nothing until a restart [ID151 §6.3]. Writing the pre-disable to BBR and
Flash as well adds nothing to the edge, and that extra write is what leaves the residue.

The closest u-blox statement is in the C099 README, which says of `F9P Base Survey in disable.txt`:
"(a cold start must be sent after this)" [C099 README]. That is a restart of the navigation engine
after leaving time mode, not a 0→N edge rule.

**What the u-blox reference scripts actually write** [C099]:

| Script | Writes |
|---|---|
| `F9P Base Survey in disable.txt` | `CFG-TMODE-MODE 0` to **RAM, Flash and BBR** |
| `F9P Base Survey in start.txt` | `CFG-TMODE-MODE 1`, `SVIN_MIN_DUR 60`, `SVIN_ACC_LIMIT 50000` to **RAM only**, and `CFG-TMODE-MODE 0` to **Flash** |
| `F9P Base config C99.txt` (port/RTCM setup) | 21 keys, each to **RAM + Flash**. No BBR writes, no TMODE |

So the scripts:

1. use all three layers **only for disable**. That makes it a full reset to rover, and leaves BBR
   at 0 on purpose;
2. treat survey-in as **non-persistent**: RAM gets 1 and Flash is pinned to 0, so a restarted
   receiver does not silently start a new survey;
3. write persistent settings to **RAM + Flash** and keep them out of BBR;
4. ship **no fixed-base script**, so none of them persists `MODE=FIXED`.

The scripts are consistent because nothing they write afterwards depends on a lower layer showing
through BBR: survey-in is RAM-only. The app took the "disable to all three layers" step from the
disable script and put it in front of a *persistent* RAM+Flash enable. u-blox never shows that
combination, and the layer rules in §3 make the BBR `0` override the Flash `N`.

The driver's comment at `ublox.py:664-669` ("Writing only to RAM leaves BBR pinned at TMODE=1 across
host restarts") does not match the scripts. The u-blox start script never writes BBR, so BBR could
only hold `1` if something else had written it, such as a CFG-CFG save to BBR or a u-center
"save configuration".

## 7. Q5: legacy UBX-CFG-CFG on Gen9, and the `save_to_flash` bug

**The masks are now all-or-nothing** (protocol > 23.01; the F9P HPG firmwares are 27.x)
[ID151 UBX-CFG-CFG, p.65-66]:

> "The three masks which were used to clear, save and load a subsection of configuration have lost
> their meaning. … if any bit is set in the saveMask: **all current configuration is stored
> (copied) to the selected layers** … The sequence of execution is clear, save, then load."
>
> `deviceMask` (optional, byte 12): `bit 0 devBBR`, `bit 1 devFlash` (bits 2/4 EEPROM/SPI flash
> "only supported for protocol versions less than 14.00"). "**Note that if a deviceMask is not
> provided, the receiver defaults the operation requested to battery-backed RAM (BBR) and Flash
> (if available).**"

How this works with BBR: a working CFG-CFG save (no `deviceMask`, or `devBBR` set) copies **the
whole RAM config into BBR**. From then on BBR holds a full snapshot that outranks Flash for *every*
key. Any later RAM+Flash (layer 5) write, which is how this driver writes everything (`ublox.py`
`:794`, `:977`, `:1270`, `:1424`, `:1497`, `:1673`, `:1700`, `:1728`, `:1747`), would then be
undone by the stale BBR snapshot at the next reset while BBR has power. **Saving to BBR and writing
to RAM+Flash do not mix.**

**What the driver actually sends.** `save_to_flash` (`ublox.py:1850-1865`) builds
`UBXMessage("CFG", "CFG-CFG", SET, saveMask=b"\x1f\x1f\x00\x00", deviceMask=b"\x17")`. In pyubx2
1.3.0, CFG-CFG's last byte is a bitfield made of `devBBR`/`devFlash`/`devEEPROM`/`devSpiFlash`.
There is no field called `deviceMask`, so the argument is silently ignored. Run locally:

```
deviceMask=b"\x17"       -> b56206090d00 00000000 1f1f0000 00000000 00 5ac8   (devBBR=0, devFlash=0, ...)
devBBR=1, devFlash=1     -> b56206090d00 00000000 1f1f0000 00000000 03 5dcb
```

So the receiver gets a 13-byte CFG-CFG with `deviceMask` **present and zero**. The spec defines
the default (BBR+Flash) only for a deviceMask that is *not provided*. It says nothing about a
present, all-zero mask. The natural reading is "save to no device", with an ACK because nothing
failed. Two pieces of evidence fit that reading:

- the bench BBR layer holds **no position keys and no other app config**. A working save-all to
  BBR would have copied the whole RAM config there, including `MODE=2` and the position;
- the old comment at `ublox.py:967-976` says CFG-CFG "does NOT reliably persist key/value-based
  TMODE config", and a hardware reset "reverted to the *prior* flashed config". That is what a
  save to no device looks like.

*This part is inference*: §10 item 3 has a one-minute bench check. Note that **fixing the `deviceMask`
alone (adding `devBBR=1`) would hide the fixed-base symptom by copying `MODE=2` into BBR, but would
create the wider snapshot problem described above.**

## 8. Recommended write sequence

Goal: **Flash is the only persistent source of TMODE, and BBR holds no TMODE keys.** This matches
how u-blox's own scripts keep persistent settings (RAM+Flash, no BBR).

`configure_fixed_base`:

1. **Pre-disable in RAM only**: `VALSET layers=1 {CFG_TMODE_MODE: 0}`. This is the 0→2 edge the
   driver relies on, and the edge only needs RAM (§6).
2. **Clear BBR**: `VALDEL layers=2` (BBR) for all 17 `CFG_TMODE_*` keys. This removes residue left
   by earlier app versions or other tools, and it is valid even when nothing is stored (§5).
3. Sleep `_TMODE_RESTART_DELAY_S`, as now.
4. **Write the fixed config to RAM+Flash**: `VALSET layers=5` with MODE=2, POS_TYPE, position and
   accuracy, as now. Consider also writing `LAT_HP`/`LON_HP`/`HEIGHT_HP` (0 or real values) so stale
   high-precision parts from an earlier config cannot combine with the new LLH. The current
   `cfg_data` sets LLH as the active `POS_TYPE` but never writes the `*_HP` keys.
5. **Verify each layer separately**: VALGET RAM (0) and Flash (2) should show MODE=2 and the
   position, and **VALGET BBR (1) should return no TMODE keys**. Absent keys are omitted from the
   reply, as `_read_cfg_keys_locked` already allows (`ublox.py:1022-1036`).
6. **Do not** follow it with a CFG-CFG save that includes BBR. Either drop the `save_to_flash()`
   calls that follow `configure_fixed_base` (`api/device.py:827`, `:904`; `ui/pages/survey.py:914`,
   `:1872`; `services/survey_service.py:684`), because step 4 already wrote Flash, or change
   `save_to_flash` to build the message with `devFlash=1` only (`devBBR=0`), so a full-config save
   never lands in BBR.

**Another option (works, not recommended):** write the fixed config to layer **7**
(RAM+BBR+Flash). BBR and Flash then agree, and a reset with BBR alive comes up fixed. The cost is
that BBR now holds TMODE, so *every* later TMODE writer must also write layer 7 or VALDEL BBR. That
is the same coupling that caused this bug. The VALDEL approach removes the coupling.

**Disable / cancel paths:** if rover mode should survive a restart, write MODE=0 to RAM+Flash
(layer 5) and VALDEL TMODE from BBR. Writing it to layer 7 also persists, but recreates the
residue, and only a later layer-7 write or a VALDEL will clear it.

To repair an affected unit in the field: send `CFG-VALDEL layers=2` for the `CFG_TMODE_*` keys,
then a controlled software reset (`CFG-RST resetMode=0x01`), or `CFG-CFG loadMask` to reload
without a reset. Check that RAM MODE reads 2 afterwards.

## 9. Other code paths with the same BBR residue

Every `_TMODE_DISABLE_ALL_LAYERS` (layer 7) write puts `MODE=0` in BBR, and every TMODE enable
writes layer 5 only. The enable is therefore overridden at the next reset while BBR has power:

| Location | Write | Effect |
|---|---|---|
| `configure_fixed_base` `ublox.py:963-977` | 0 → L7, then fixed → L5 | **The bench bug.** Fixed base lost on software reset, watchdog reset, or power cycle with backup |
| `configure_survey_in` `ublox.py:752-754`, `:794` | 0 → L7, then SVIN params + MODE=1 → L5 | BBR=0 overrides Flash=1. Survey-in does not resume after a reset with BBR alive. This matches u-blox's own "survey-in is not persistent" choice (§6), but it **contradicts the issue #42 intent** in the comment at `:762-770`, which says the enable must survive a reboot. Decide which behaviour is wanted. It only holds today when power is fully lost |
| `configure_survey_in` rollback `ublox.py:822-826` | 0 → L7 | Leaves BBR=0 (and Flash=0). Fine as a disable, but it is residue for the next layer-5 enable |
| `reset_and_reconnect` `ublox.py:576-580` | 0 → L7, then CFG-RST 0x00 | Intended to boot as a rover. Leaves BBR=0 permanently, so **any later fixed-base or survey-in enable is affected**. This path runs on every pre-survey auto-reset (`:716-724`), on every cancel (`device_service.py:740-747`), and after any `configure_survey_in` failure (`device_service.py:603-605`). It is the most likely way `test-base` got its BBR `0` |
| `configure_tmode_mode` `ublox.py:1690-1702` (apply-config "role fields" step) | MODE → L5 | Its own write puts nothing in BBR, but its result is **overridden** by BBR residue from any of the paths above. Its Flash read-back still passes, because it checks RAM and Flash, never BBR |
| `disable_base_mode` `ublox.py:897`, `:905` | 0 → **L1 (RAM only)** | Leaves no residue, but the disable does not persist either: after a reset the unit goes back to whatever BBR/Flash say. Its docstring says to call `save_to_flash()` for that, which (§7) currently saves nowhere |
| `save_to_flash` `ublox.py:1850-1865` | CFG-CFG, deviceMask sent as 0 | Most likely a no-op (§7). If "fixed" to include BBR, it snapshots all RAM into BBR and shadows every later layer-5 write |

The other layer-5 writers (ports, GNSS, rate, RTCM, optimisations, dyn model) put nothing in BBR
and do not touch TMODE. They are safe as long as BBR has no snapshot, which is another reason to
keep CFG-CFG away from BBR.

## 10. Ambiguities, and where the docs and the bench differ

1. **Edge-triggered TMODE**: not documented by u-blox (§6). This repo observed it empirically.
2. **BBR across watchdog/software resets**: inferred from the docs, not stated outright (§4). The
   repo's own `dur` observations support it.
3. **CFG-CFG with deviceMask = 0x00 present**: undefined in the spec. The bench layout (no app config
   in BBR) fits "saved nowhere", but it has not been measured. Check: run `save_to_flash()`, then
   VALGET layer 1 (BBR) for a non-TMODE key the app sets, such as `CFG_RATE_MEAS`. If it is absent,
   the save went nowhere.
4. **Cold start (`navBbrMask=0xFFFF`) and the BBR config layer**: the mask bits are documented as
   navigation-data sections only. Whether a cold start also clears the BBR config layer is not
   documented, so do not rely on it. Use VALDEL.
5. **"revert the item values to defaults"** (VALDEL text) versus the stacking rule: deleting from BBR
   exposes Flash, not Default, when Flash holds the key (§5). The §6.3 stacking text is the more
   precise of the two.
6. **Bench vs. hypothesis**: the bench readout (RAM 2, BBR 0, Flash 2) is exactly the layout §3
   predicts would come up DISABLED after a reset. RAM shows 2 only because no reset has happened
   since the write. To confirm: `CFG-RST resetMode=0x01`, then VALGET RAM `CFG_TMODE_MODE`.
   Expect 0 if the board's BBR has power, and 2 after a VALDEL of BBR TMODE plus a reset.
7. **Is BBR powered on this board?** Unknown from the repo. Many ZED-F9P carrier boards fit a
   backup cell, and if `test-base` has one, a power cycle will also come up disabled. If V_BCKP is
   tied to VCC, only resets will.
