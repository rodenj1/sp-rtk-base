# ZED-X20P protocol and firmware compatibility — base and rover

Research for [issue #248](https://github.com/rodenj1/sp-rtk-base/issues/248), under [map #247](https://github.com/rodenj1/sp-rtk-base/issues/247). Repository baseline: `8854c750f06ff321be29c9d29f9874484ce92b97`. Research date: 2026-10-12. Scope is desk research only, not an implementation, upgrade recommendation, or claim of tested support.

## Outcome

**X20P can perform stationary base and RTK rover roles, but is not a drop-in F9P protocol target.** The key/value configuration model, TMODE coordinates, NAV-SVIN, NAV-PVT and MSM4/MSM7 concepts carry over. Legacy CFG-GNSS polling and RXM-RTCM telemetry do not appear in the X20 interface inventories; the existing driver uses both. Firmware identity must select capabilities: HPG 2.00/2.02/2.10/2.11 map to protocol 50.02/50.10/50.11/50.11 respectively. Protocol version alone cannot distinguish 2.10 from 2.11. [I00 §1.2, §3.8; I02 §1.2, §3.8; I10 §1.2, §3.8; I11 §1.2, §3.8; R00 §2; R02 §1.2; R10 §1.2; R11 §1.2]

Three high-impact firmware differences:

- **2.10 adds documented X20P moving-base operation, RTCM 1006 output/antenna height and Galileo HAS.** Older interface manuals contain generic 4072.0/4072.1 entries, but lack their output configuration keys; that is not evidence of working older X20P moving-base support. [R10 §2, §2.4.1; IM §2.4.2–2.4.6, pp.23–30; I00/I02/I10 §4.3 and CFG-MSGOUT]
- **2.11 removes `CFG-NAVCOR-ENABLE_HOST`**, keeps host corrections always enabled and adds HAS fallback plus NAV2 secondary output. ArduSimple's current purported 2.11 HAS preset description still instructs setting the removed key. Do not import it uncritically. [R11 §2–2.1, pp.2–3; ACFG “Load a configuration file”, Galileo HAS row]
- **2.11 corrects RTCM MSM phase-range-rate sign**, affecting MSM7/Doppler consumers, and provides `CFG-RTCM-MSM_OUT_RANGERATE_SIGN`. The release note also mentions MSM5 output, while the interface message inventory lists MSM5 as input only; this does not establish X20P MSM5 generation. [R11 §2, p.2; I11 §4.3, pp.176–177; configuration defaults Table 94, p.270]

The provided context reports USB `1546:01ab` on hermes-01 but no tty/driver. This identifier agrees with the default configurable VID/PID in I02/I11 (5446/427), **not with a hardware-confirmed module, ROM or firmware identity**. No receiver, host driver, network port or device configuration was accessed or changed during this research. [I02 CFG-USB defaults; I11 Table 111, p.274; supplied task context]

## Evidence and source/revision inventory

The labels below are clickable primary-source references. Page numbers refer to printed PDF pages, not extraction line numbers. Release notes do not display an Rxx revision; their author date is recorded instead. Mutable URLs may later serve a newer revision.

| Label | Source actually read | Revision/date and applicability |
|---|---|---|
| [D00] | ZED-X20P-00B data sheet, UBXDOC-963802114-12690 | R06, 21-Sept-2026, 31 pages; p.2 lists `ZED-X20P-00B-00`, HPG 2.02. Its ordering table p.28 instead says shipped HPG 2.10: document inconsistency, not an identity guarantee. |
| [D01] | ZED-X20P-01B data sheet, UBXDOC-304424225-21281 | R02, 21-Sept-2026, 32 pages; p.2 lists `ZED-X20P-01B-00`, HPG 2.10. |
| [IM] | ZED-X20P integration manual, UBXDOC-963802114-12901 | R05, 19-Aug-2026, 110 pages; p.2 covers 00B-00/2.02, 01B-00/2.10, 01B-01/2.11. This is a multi-version manual, so footnotes matter. |
| [I00] | u-blox 20 HPG 2.00 interface description, UBXDOC-304424225-19888 | R01, 22-May-2025, 224 pages; protocol 50.02, shared X20P/F20P description. |
| [I02] | u-blox X20 HPG 2.02 interface description, UBXDOC-304424225-19967 | R03, 13-Nov-2025, 238 pages; protocol 50.10, X20P/F20P. Revision history p.237: R02 adds X20P IP release, R03 adds USB support. |
| [I10] | u-blox X20 HPG 2.10 interface description, UBXDOC-304424225-21263 | R02, 17-Jun-2026, 233 pages; protocol 50.11, X20P. R02 adds previously missing MGA-GAL-OSNMA and SEC-SIG descriptions. |
| [I11] | u-blox X20 HPG 2.11 interface description, UBXDOC-304424225-21617 | R01, 25-Aug-2026, 278 pages; protocol 50.11, X20P. |
| [R00] | Firmware HPG 2.00 for ZED-X20P, UBXDOC-304424225-19895 | Author date 7-May-2025, 3 pages. |
| [R02] | Firmware HPG 2.02 for ZED-F20P and ZED-X20P, UBXDOC-304424225-20040 | Author date 16-Oct-2025, 3 pages. This, not 19895, is the actual 2.02 release note linked by the product page. |
| [R10] | HPG 2.10 firmware for ZED-X20P-01B, UBXDOC-304424225-21261 | Author date 15-May-2026, 5 pages. |
| [R11] | HPG 2.11 firmware for ZED-X20P-01B, UBXDOC-304424225-21629 | Author date 24-Aug-2026, 3 pages. |
| [ABOARD] | ArduSimple simpleRTK4 Optimum user guide | Live unversioned page; product overview, ports, rails, XBee routing, power and LEDs read. |
| [ACFG] | ArduSimple “How to configure u-blox ZED-X20P, ZED-X20D” | Live unversioned page; currently based on 2.11 presets; X20D-only heading section is explicitly not for X20P. |
| [APROD] | ArduSimple simpleRTK4 Optimum product page | Live unversioned page; SKU `AS-RTK4-X20P-L1256-NH-00`, base/rover, antenna and battery option. Marketing update-rate text differs within this page. |

### Retrieval/access notes

Web search and Firecrawl extraction were used first. Several `www.u-blox.com` PDF extractions failed, including the data sheet and release notes. The corresponding official `content.u-blox.com` URLs succeeded. Search queries for the latest release-note identifiers returned no results; the browser's **Documentation & resources → Release Note/Interface Manual** categories supplied the real links. Guessed long-form release-note filenames returned “requested page could not be found” and were discarded. All four actual release notes and all four interface descriptions above were subsequently read; there is no remaining primary-manual access blocker for this matrix. No authenticated support-portal session was used, and no firmware binaries were downloaded or installed.

I00 is visibly incomplete as a configuration-message inventory: §6.4.1 explicitly names VALGET/VALSET/VALDEL, but detailed §3.10 entries are absent in the retrieved revision. Use it for documented high-level support, not as the authority for undocumented wire details. I02/I10/I11 contain the detailed messages. Also, I00/I02 are shared descriptions and contain generic RTCM and signal keys beyond the product-specific data-sheet lists. Mere message/key presence is not proof of usable X20P navigation functionality. [I00 §3.10, §6.4.1; I02 §3.10]

## 1. Firmware and identity compatibility matrix

Status vocabulary: **supported** = affirmatively documented for the stated firmware/product; **changed** = documented semantic/interface difference; **removed/not documented** distinguishes explicit release-note removal from absence in a message inventory; **uncertain** = conflicting/generic documentation or no device evidence; **firmware-dependent** = never treat as a family-wide constant.

| Capability | HPG 2.00 | HPG 2.02 | HPG 2.10 | HPG 2.11 | Base / rover consequence |
|---|---|---|---|---|---|
| Firmware/protocol | `EXT HPG 2.00 (1e963f3)`, 50.02 | `EXT HPG 2.02 (43e74c3)`, 50.10 | `EXT HPG 2.10 (b0eda3)`, 50.11 | `EXT HPG 2.11 (c97ee1)`, 50.11 | Preserve SW, FWVER, PROTVER, MOD and ROM evidence; do not branch only on 50.11. [R00 §2; R02/R10/R11 §1.2] |
| Released image ROM support | ROM 1.11 `0x00A9D329` and ROM 1.00 `0x22018AE9` | Same two ROM bases | Same two ROM bases | **Only ROM 1.11 `0x00A9D329` listed** | Do not infer 2.11 compatibility with a ROM 1.00 unit from ArduSimple's “no hardware difference” claim. [R00 §2; R02/R10/R11 §1.2; ACFG “Firmware update”] |
| Evaluation software | u-center 2 ≥25.03 | ≥25.06 | ≥26.05 | ≥26.05 | Legacy u-center is not the full X20 tool. This does not require Windows for a future UBX implementation. [R00 §2.1; R02/R10/R11 §1.3; IM App.A.2 p.103] |
| Static base / RTK rover | Supported | Supported | Supported | Supported | Capability exists, but current application's F9P workflows are not thereby validated. [R00 §3.2; R02/R10 §2.2; IM §2.4] |
| SPARTN performance and plan SP6 | SP1/SP2 documented | Adds SP6 and SPARTN high-ionosphere fixing improvements; RTCM performance described as unchanged | Further ionosphere/scintillation improvements | Further RTK improvement with ionosphere estimation disabled | Keep correction source, plan and firmware separate. [I00/I02 CFG-SIGNAL; R02 §2; R10 §2; R11 §2] |
| Galileo HAS | Not documented as positioning feature | Not documented as positioning feature | Added; host/HAS enable controls | HAS fallback; host corrections always enabled; host-enable key removed | 2.10 and 2.11 source-selection semantics differ despite same protocol version. HAS is PPP/float, not stationary-base RTK proof. [R10 §2/§2.4; R11 §2] |
| NAV2 secondary solution | Not documented | Not documented | Not documented | Added | Do not mistake secondary standard/SBAS position for primary RTK result. [R11 §2.1; IM §3.5 pp.46–48] |
| 1006 output / DF028 antenna height | 1006 input only in I00 | 1006 input only in I02 | Added output and key | Retained | Fixed-base profiles must gate 1006 generation; 1005 remains common. [I00/I02 §4.3; R10 §2.4.1; IM §2.4.3 footnote 8, §2.4.5.3 footnote 9] |
| Moving-base functionality | Generic 4072 catalog is not sufficient evidence | Same caveat; output keys absent | Added for another X20P receiver | Retained | Independent feature from stationary base; do not claim X20P↔F9P moving-base interoperability from RTCM support alone. [R10 §2; IM §2.4.6] |
| MSM range-rate sign | No corrected-sign key documented | Same | Pre-correction behavior | Corrected sign + compatibility key | Future MSM7 and Doppler/range-rate validation must record firmware and sign setting. [R11 §2; I11 defaults Table 94] |
| Explicit deprecated features | Older inventories contain logging/ODO/RINV/VLW | Older inventories contain these | R10 lists LOG, LOGFILTER, VLW, NAV-ODO/RESETODO, ODO and RINV removals | Do not restore removed keys | R10 §2.4 labels its comparison “HPG 1.50”, although §2 compares with 2.02; preserve this wording inconsistency rather than treating every removal as proven 2.02→2.10 delta. |

### MON-VER discovery contract (research finding, not an executed poll)

`UBX-MON-VER` class/ID `0x0a 0x04` is a zero-payload poll. Response payload is `40 + n×30` bytes: `swVersion` CH[30], `hwVersion` CH[10], followed by CH[30] NUL-terminated extension strings. Not every extension appears. Firmware, protocol, module identifier, ROM information and supported GNSS can appear in extensions. Retain raw strings as well as parsed fields; an absent `MOD` must not be replaced by a guess from USB VID/PID or a vendor product link. [I11 §1.2 pp.14–16, §3.13.11 pp.102–103; corresponding MON-VER sections in I00/I02/I10]

**Approval-gated future evidence needed:** obtain model/type/ROM and complete MON-VER from the actual device through an available interface. Nothing in this note claims that poll occurred. Firmware-update eligibility remains unresolved until that identity is known, especially for 2.11's narrower released-image ROM support. [R11 §1.2]

## 2. Transport and carrier matrix

| Interface | Chip-level documentation | ArduSimple mapping / compatibility implication |
|---|---|---|
| UART1/UART2 | Both configurable; no RTS/CTS hardware flow control. UART2 accepts UBX input even when its UBX output is disabled. Default baud is 38400, 8N1. [IM §3.3.1 pp.39–40; D01 §5.5 pp.21–22] | Pixhawk JST-GH uses UART1 RX/TX, 3.3 V logic. Arduino TX1/RX1 and TX2/RX2 also 3.3 V. The XBee selector can route UART1 or UART2; do not infer console port from connector name alone. [ABOARD “Pixhawk connector”, “Arduino rails”, “Buttons and switches”] |
| Native USB | USB 2.0 full-speed 12 Mbit/s, self-powered module interface; no suspend; disabled in safeboot; boot screen retransmitted after enumeration, earlier startup messages lost. USB documentation was added in I02 R03 and IM R03, not in I00. [IM §3.3.4 pp.43–44 and revision history p.109; I02 p.237] | `POWER+GPS`/USB GPS is native X20P USB. Board can take USB power through its own supply circuitry, so chip “self-powered” does not mean this carrier cannot be USB-powered. USB XBee is an FTDI adapter to the radio UART, **not the same GNSS endpoint**. Guide's Windows advice is not a tested Linux driver binding guarantee. [ABOARD “Power”, “USB GPS”, “USB XBee”] |
| I2C | Peripheral only, up to 1000 kbit/s, clock stretching up to 20 ms, byte-stream registers 0xFD/0xFE/0xFF; configured 8-bit address 0x84, not a host API's 7-bit address. [IM §3.3.2 pp.40–42] | SDA/SCL exposed on rails, 3.3 V. Need carrier/host wiring review before claiming application transport support. [ABOARD “Arduino rails”] |
| SPI | Peripheral only; D_SEL low selects SPI on pins shared with UART1/I2C. D_SEL open selects UART1+I2C. [IM §2.2.2 pp.15–16, §3.3.3 pp.42–43] | Carrier guide does not establish an accessible turnkey SPI connection for this application. Chip support is not host implementation support. |
| NTRIP / radio / Bluetooth | Network transport is outside the GNSS chip; corrections are delivered over enabled physical input protocols. [IM §1.2.3 p.8, §2.4.1 p.23] | NTRIP client/caster and XBee plugin routing belong to the host/accessory. For VRS, return GGA to the caster; merely receiving serial bytes is not proof of correction use. [IM App.D.2 p.107; ABOARD “Get RTK corrections”] |

Default **chip** output: UART1 NMEA GGA/GLL/GSA/GSV/RMC/VTG/TXT, UBX protocol enabled but no periodic UBX messages; UART2 RTCM protocol enabled but no periodic RTCM messages, NMEA/UBX output disabled. USB/I2C/SPI have UART1-like message defaults. Protocol enable and message enable are separate. ArduSimple says the shipped board is preconfigured as a rover; that is not a guarantee of chip-default contents on an owned or previously configured board. [D01 §5.5 pp.21–22; ABOARD “Get started”]

The board's optional backup battery is not included by default according to APROD. Its presence and voltage are unknown here; therefore BBR persistence across power removal cannot be assumed. All-band performance requires a suitable antenna; the guide recommends triple-band antennas, while L6/E6 use also requires actual antenna coverage at those frequencies. [APROD description; ABOARD antenna section; IM App.A.1 p.103]

## 3. Configuration interface, layers and save/reset

| Area | Classification and source | Both-role consequences |
|---|---|---|
| VALGET/VALSET/VALDEL | **Supported.** I00 §6.4.1 names them; I02/I10/I11 §3.10 provide detailed definitions. | Build configuration around the firmware's key catalog and explicit layer selection, not legacy F9P CFG blocks. |
| VALGET | At most 64 requested keys and 64 response pairs. Separate requests per layer: RAM=0, BBR=1, Flash=2, Default=7. Wildcards paginate by `position`; a full 64-pair response may need continuation. Unknown keys cause NAK. [I11 §3.10.5 pp.66–67] | Avoid bundling an optional/removed key with mandatory keys: one unsupported item rejects the poll. Never interpret Default as effective RAM state. |
| VALSET | At most 64 pairs/message. Layers are a **bitmask**, RAM=bit0, BBR=bit1, Flash=bit2, not VALGET's selector. v0 applies immediately; v1 supports transactions. Unknown keys/no layer/invalid RAM configuration cause NAK. [I11 §3.10.6 pp.67–69] | Do not use a VALGET layer number as VALSET mask. RAM+BBR+Flash must be explicitly requested when intentional; no implicit save. |
| Transactions | Same layer mask throughout; intervening other CFG request, including VALGET, cancels a VALSET transaction. Read back after commit, not mid-transaction. [I11 §3.10.6.2; §6.6 pp.206–207] | Multi-key constellation changes need coherent dependencies. ACK alone is not the later readback/persistence test. |
| Layer precedence | Current RAM, then BBR, Flash, Default; BBR/Flash overrides take effect when rebuilding configuration. BBR depends on backup supply. [I11 §6.3 pp.204–205; IM §2.1 p.15] | A stale BBR value can override the desired Flash value at restart. Verify each deliberately changed layer. |
| Legacy CFG-GNSS/PRT/MSG/RATE/NAV5/TMODE3 | **Not documented** in all four X20 UBX inventories, unlike preserved CFG-CFG and CFG-RST. Absence is not a captured NAK, but is no basis for advertised compatibility. [I00/I02/I10/I11 §3.8 and §3.10] | Replace legacy reads with VALGET keys; don't send an unsupported poll and then claim successful verification. NAV/GNSS status messages are not substitutes for effective configuration. |
| CFG-CFG | **Supported but broad semantics.** Any nonzero clearMask clears all selected saved configuration; any nonzero saveMask copies all current configuration; any nonzero loadMask rebuilds RAM. Old subsection mask semantics do not apply. Omitted deviceMask defaults to BBR+Flash if available. [I11 §3.10.1 pp.61–62; I00/I02/I10 corresponding sections] | Use targeted VALSET/VALDEL for a selective operation. A “save” may capture temporary telemetry/protocol changes too. |
| CFG-RST | Supported; do **not expect an ACK**. `navBbrMask` hot=0x0000, warm=0x0001, cold=0xFFFF. [I11 §3.10.3 pp.63–64] | Reconnect/read identity and effective settings after an approved reset rather than declaring failure from missing ACK. Cold start is not a flash factory reset. |
| Reset scope | Hardware reset 0x00/0x04 and RESET_N clear RAM/BBR/RTC; controlled software 0x01 clears RAM with selected BBR clearing; GNSS-only 0x02/stop 0x08/start 0x09 clear navigation RAM, not the whole configuration. Flash survives reset. [IM §3.6.1 Table 43 p.49] | Reapply transient RAM state as appropriate. Flash clearing requires an explicit different operation. |
| Flash longevity / config lockdown | Avoid frequent Flash writes; IM recommends production/infrequent updates. Configuration can be locked using CFG-SEC controls. [IM §2.1 p.15; §3.13.2 pp.64–65] | Don't persist every telemetry enable, start/stop or retry; distinguish rejected/locked writes from transport failure. |

### Representative exact keys and units

This is the task-relevant subset, not an exhaustive catalog or an executable profile. Spellings follow u-blox hyphenated names; Python libraries may expose underscores. Keys are confirmed in I11, with common TMODE/RATE keys also in the earlier interface descriptions. USB-specific keys are not documented by I00.

| Key | ID / type / units | Meaning / constraint |
|---|---|---|
| `CFG-TMODE-MODE` | `0x20030001`, E1 | 0 disabled, 1 survey-in, 2 fixed |
| `CFG-TMODE-POS_TYPE` | `0x20030002`, E1 | 0 ECEF, 1 LLH |
| `CFG-TMODE-ECEF_X/Y/Z` | `0x40030003/04/05`, I4, cm | Antenna reference point coordinates |
| `CFG-TMODE-ECEF_X_HP/Y_HP/Z_HP` | `0x20030006/07/08`, I1, 0.1 mm | Add signed residual, range −99…+99 |
| `CFG-TMODE-LAT/LON` | `0x40030009/0a`, I4, 1e−7 deg | Latitude/longitude main parts |
| `CFG-TMODE-HEIGHT` | `0x4003000b`, I4, cm | LLH height, not an automatic orthometric-height transform |
| `CFG-TMODE-LAT_HP/LON_HP` | `0x2003000c/0d`, I1, 1e−9 deg | Signed residual, −99…+99 |
| `CFG-TMODE-HEIGHT_HP` | `0x2003000e`, I1, 0.1 mm | Signed residual, −99…+99 |
| `CFG-TMODE-FIXED_POS_ACC` | `0x4003000f`, U4, 0.1 mm | Accuracy estimate, not a correction to wrong coordinates |
| `CFG-TMODE-SVIN_MIN_DUR` | `0x40030010`, U4, seconds | Minimum survey duration |
| `CFG-TMODE-SVIN_ACC_LIMIT` | `0x40030011`, U4, 0.1 mm | Survey accuracy condition |
| `CFG-RATE-MEAS` | `0x30210001`, U2, milliseconds | Measurement period |
| `CFG-RATE-NAV` | `0x30210002`, U2 | Measurements per navigation solution, 1…127 |
| `CFG-SIGNAL-PLAN` | `0x2031003a`, E1 | SP1=1, SP2=2, SP6=6; SP6 added in 2.02 |
| `CFG-UART1-BAUDRATE` / `CFG-UART2-BAUDRATE` | `0x40520001` / `0x40530001`, U4 | Firmware-supported baud values only |
| `CFG-USBINPROT-RTCM3X` / `CFG-USBOUTPROT-RTCM3X` | `0x10770004` / `0x10780004`, L | Correction protocol input/output gating |
| `CFG-MSGOUT-UBX_NAV_PVT_USB` | `0x20910009`, U1 | Primary navigation-result output divider |
| `CFG-MSGOUT-UBX_NAV_SVIN_USB` | `0x2091008b`, U1 | Survey telemetry divider |
| `CFG-MSGOUT-UBX_RXM_COR_USB` | `0x209106b9`, U1 | Correction input-status output |
| `CFG-MSGOUT-RTCM_3X_TYPE1005_USB` | `0x209102c0`, U1 | Base coordinate message divider |
| `CFG-MSGOUT-RTCM_3X_TYPE1006_UART1` | `0x209102c3`, U1 | Firmware ≥2.10 output |
| `CFG-MSGOUT-RTCM_3X_TYPE4072_0_UART1` / `…4072_1_UART1` | `0x209102ff` / `0x20910382`, U1 | Listed in 2.10/2.11 defaults; absent from older output-key lists |
| `CFG-RTCM-DF003_OUT` / `CFG-RTCM-DF003_IN` | `0x30090001` / `0x30090008`, U2 | RTCM station identity, valid station IDs 0…4095 |
| `CFG-RTCM-DF028_OUT` | `0x30090010`, U2, 0.0001 m | 1006 antenna height; ≥2.10 |
| `CFG-RTCM-MSM_OUT_RANGERATE_SIGN` | `0x10090011`, L | New in 2.11, documented default true; sign behavior needs a consumer-specific check |
| `CFG-NAVCOR-ENABLE_GAL_HAS` | `0x100d0002`, L | ≥2.10 HAS enable; host enable is a separate 2.10-only item, removed in 2.11 |

Sources: I11 CFG-TMODE §6.9.27 pp.245–247; CFG-RATE §6.9.18 pp.239–240; CFG-RTCM §6.9.19 and defaults Table 94 p.270; CFG-SIGNAL §6.9.22; CFG-MSGOUT/default tables; CFG-UART/USB sections. I00/I02/I10 corresponding named configuration groups; R02 §2; R10 §2.4.1; R11 §2.1.

## 4. Stationary base and rover behavior

| Workflow | Supported behavior and compatibility constraint |
|---|---|
| Native survey-in | Weighted mean of valid 3D solutions; **both** minimum duration and accuracy conditions must be satisfied. NAV-SVIN reports status. Completion enters time mode and allows configured stationary coordinate messages. Do not feed RTCM corrections during native survey-in. [IM §2.4.5.1 p.26] |
| Corrected pre-survey | Run as a rover with corrections first; enter the resulting coordinates in fixed mode afterward. This is a distinct supported route, not “corrected native survey-in”. [IM §2.4.5 pp.25–26] |
| Fixed base | Set correct ARP ECEF or LLH coordinates and accuracy estimate. Any coordinate error translates into rover absolute position error. A reported accuracy estimate does not remove it. A moved base needs new coordinates. [IM §2.4.5.2 pp.26–27] |
| Survey restart/cancel | TMODE disabled/survey/fixed values are documented, but the exact existing F9P disable→readback→reenable sequence's timing and stale NAV-SVIN behavior have not been demonstrated on X20P. Retain this as a future validation question, not a guaranteed identical state machine. [I11 CFG-TMODE; repository driver/memory-bank context] |
| Base output before survey completion | Observation messages stream as soon as enabled. Stationary coordinate messages require initialized time/fixed position. Thus bytes/MSM on the wire are not proof of a usable base. [IM §2.4.5 p.25; App.D.1 pp.105–107] |
| Rover ambiguity fixing | Need observations and 1005/1006 with matching station identity and appropriate shared signals. CFG-NAVHPG-DGNSSMODE controls fixed attempts versus float-only. Default corrections timeout is 60 s; configurable through CFG-NAVSPG-CONSTR_DGNSSTO and its scale. [IM §2.4.4 pp.24–25] |
| GLONASS fixing | Stream must include 1230 or suitable 1033, or GLONASS carrier ambiguities remain float. X20P generates 1230 but 1033 is input-only in the documented inventory. [IM §2.4.4 p.25, §2.4.5.3 p.28; I11 §4.3] |
| Reference frame / height | Rover follows correction source reference frame; datum/epoch and antenna reference point must be known. EGM96 internal geoid is limited resolution; for precise orthometric height apply an appropriate external model to ellipsoidal height. [IM App.C pp.104–105] |
| Moving base | ≥2.10 documented two-X20P route uses 4072.0 plus MSM4/7, matched rates and low link latency; separate from stationary TMODE. Do not apply X20D dual-antenna guide to X20P. [IM §2.4.6 pp.28–30; R10 §2; ACFG “Heading configuration”] |

## 5. Signal bands: supported is not the same as enabled or used

| Constellation | Documented signal set / bands | Firmware and plan qualifications |
|---|---|---|
| GPS | L1C/A, L2C, L5 | All four release notes list these. GPS L5 was unhealthy at release and is not used by default merely because its key is enabled. Treat any health override as an explicit later operational choice. [R00 §3.1; R02/R10 §2.1; IM §2.2.5 p.18] |
| Galileo | E1-B/C, E5a, E6 | E6 exists on X20P (not the shared F20P assumption); SP6 omits E6. E5b appears as a disabled generic key but not in R10's supported signal list; no claim of E5b navigation support here. HAS uses E6-B corrections and is ≥2.10. [R02/R10 §2.1; IM Table 6 p.17; §3.2 pp.38–39; I11 CFG-SIGNAL defaults] |
| BeiDou | B1I, B1C, B2a, B3I | SP1 includes B1I/B3I; SP2 includes B1C/B2a/B3I without B1I; SP6 includes B1I/B1C/B2a but not B3I. SP6 intended for PointPerfect Flex over IP, with CFG-BDS-D1D2_NAVDATA for B1I navigation data. [IM §2.2.4.1 Table 6 pp.17–18] |
| GLONASS | L1OF, L2OF explicitly in R10 and modern SP2/SP6 | R00/R02 supported-signal lists and D00 omit GLONASS, although I00/I02 generic signal/RTCM catalogs contain it. **Earlier usable X20P GLONASS is uncertain**, not proven by catalog entries. 2.10 adds geoblocking; regional availability must be checked. [R00 §3.1; R02 §2.1; R10 §2/§2.1; D00 §1.3/§1.6; IM §3.15 p.69] |
| QZSS | L1C/A, L1S, L2C, L5; L1C/B added in 2.10 | Disabled by default; requires GPS enabled. R11 improves L1C/B tracking/bias. Board marketing's L6 wording does not establish integrated X20P L6 reception: CLAS is documented via external NEO-D9C and UBX-RXM-QZSSL6 input. [R00/R02/R10 signal lists; R11 §2; IM §2.2.4, §2.5.6 p.33] |
| NavIC | L5 SPS | Disabled by default. D00 lists L1 SPS Data with “currently in development phase” footnote; release notes list L5 only and I11 has no NAVIC_L1 enable key. Do not advertise NavIC L1 as production firmware functionality. [D00 §1.3 pp.6–7; R10 §2.1; I11 CFG-SIGNAL] |
| SBAS | L1C/A; GPS/GAL/BDS dependency as described in IM | SBAS configuration and test/operational status matter. Guide presets disable SBAS, but that is not a chip-family requirement. [IM §2.2.4, §3.1 pp.35–37; ACFG presets] |

The latest IM states an L1-band requirement generically, while also listing NavIC L5-only in its signal plans. Do not generalize that wording into an invented NavIC L1 requirement. Invalid constellation/band combinations return NAK and can emit “inv sig cfg”; use the firmware's plan and constellation dependencies. Query supported plans through MON-GNSS rather than assuming every CFG-SIGNAL constant is available. [IM §2.2.4 pp.16–18; I11 CFG-SIGNAL-PLAN]

Base and rover need **overlapping usable signals**, not simply the same constellation checkbox. An F9P band variant and an all-band X20P may share only some observations. Extra X20P bands cannot be promised as used by an F9P rover; conversely an X20P rover receiving dual-band base corrections is not receiving all-band corrections. Actual F9P SKU/firmware and MSM signal masks are a further interoperability research dependency, not measured in this task. [IM §2.4.4 p.24 and App.A.1 p.103]

## 6. RTCM input/output compatibility matrix

This table describes the X20 firmware interface inventories, not proof that every shared-catalog function works on every X20P variant. Relevant product restrictions/conflicts are retained explicitly.

| RTCM family | Rover input | Base output | Classification / sources |
|---|---|---|---|
| 1001/1002/1003/1004 GPS legacy observations | Listed | Not listed | Input-only; not an output profile for this base. [I00/I02/I10/I11 §4.3] |
| 1009/1010/1011/1012 GLONASS legacy observations | Listed in generic interfaces | Not listed | Earlier product's GLONASS omission still applies; modern IM lists input. [IM Table 19 p.23; D00 §1.6; I11 §4.3] |
| 1005 stationary ARP | Listed | Listed | Common stationary-base coordinate message. Output gated by base position initialization. [All interfaces §4.3; IM §2.4.5] |
| 1006 ARP with height | Listed in all | ≥2.10 | Earlier I00/I02 input-only; R10 explicitly adds generation. [IM Table 20 p.24 footnote 8; R10 §2.4.1] |
| 1007 / 1033 descriptions | Listed | Not listed | 1033 can provide GLONASS receiver information needed for fixing; don't assume this base can synthesize it. [I11 §4.3; IM §2.4.4] |
| GPS MSM4 1074 / MSM7 1077 | Listed | Listed | Both variants supported; choose one per stream/constellation. [All interfaces §4.3] |
| GLONASS MSM4 1084 / MSM7 1087 | Listed in generic catalogs; modern IM/R10 support GLO | Listed in generic catalogs; modern IM/R10 support GLO | 00B/older product support unresolved; geoblocking is relevant ≥2.10. [R10 §2.1; D00 §1.6; IM Table 19/20] |
| Galileo MSM4 1094 / MSM7 1097 | Listed | Listed | Supported variants. [All interfaces §4.3] |
| BeiDou MSM4 1124 / MSM7 1127 | Listed | Listed | Supported variants. [All interfaces §4.3] |
| MSM5 1075/1085/1095/1125 | Listed | **Not listed** | Do not interpret R11's range-rate text as overriding interface inventory to enable MSM5 output. [I11 §4.3 pp.176–177; R11 §2] |
| QZSS 111x / NavIC 113x MSM | Not in these inventories | Not in these inventories | Reception of those constellations does not establish matching RTCM generation/input. Further vendor clarification if needed. [All interfaces §4.3; IM Tables 19/20] |
| 1230 GLONASS code-phase bias | Listed | Listed in generic catalogs and modern IM | Use where GLONASS is actually available/enabled; older-product omission unresolved. [I11 §4.3; IM §2.4.4–2.4.5.3] |
| 4072.0 proprietary station PVT | Generic input/output catalog entries in all | Actual ≥2.10 output keys and moving-base description | Firmware-dependent actual feature; earlier catalog entries conflict with introduction note. [R10 §2.4.1; IM footnote 8; all interface §4.3/CFG-MSGOUT] |
| 4072.1 proprietary additional reference info | Generic input/output catalog entries in all | Output keys in I10/I11 defaults | Modern interface documents it, but IM omits it from operational tables and R10 introduction lists only 4072.0. Needed/used semantics are unresolved; do not require it for ordinary stationary base. [I10/I11 §4.3/defaults; IM Tables 19/20] |
| RTCM2, MSM1/2/3/6, legacy ephemeris/SSR messages outside inventory | Not documented here | Not documented here | No support claim from “RTCM 3.4” alone. SPARTN/HAS are separate documented correction paths, not blanket RTCM-SSR support. [IM §2.4.1; I11 §4.3/§5] |

### Stream construction constraints

- All observation messages must have the same output rate; use one MSM family across constellations. Mixing MSM4/MSM7 can break the multiple-message bit. All ports generating RTCM must use the same RTCM configuration, or that bit may be set incorrectly. A control port's extra UBX telemetry is a separate protocol; the restriction is on the RTCM generation configuration. [IM §2.4.5.3 p.28]
- 1005/1006 can be slower than observations, but the rover cannot fix before receiving reference coordinates. Station IDs in coordinate and observation messages must agree. `CFG-RTCM-DF003_IN_FILTER` controls input filtering; IM recommends RELAXED. [IM §2.4.4 p.25, §2.4.5.3 p.28]
- Include 1230 (or source-provided compatible 1033) for GLONASS fixing. Never infer that a rover's overall “fixed” means every constellation's ambiguities are fixed. [IM §2.4.4 p.25]
- Stationary base needs no proprietary moving-base messages. X20P-generated ordinary MSM4/7 is a standards-based interoperability route, but neither the application's parser nor an arbitrary third-party/F9P rover has been tested here. Firmware 2.11's MSM7 range-rate correction makes recording exact firmware especially important. [IM §2.4.5; R11 §2]

## 7. Correction-use and position telemetry

**Replace the research assumption that RXM-RTCM exists on X20.** It is absent from all four X20 interface message inventories. `UBX-RXM-COR` class/ID `0x02 0x34`, version 1, 12-byte payload, is documented throughout. It is emitted after successful parsing of a correction message **whether or not supported or used**. [I00 §3.16.1 pp.118–120; I02 §3.16.1 pp.125–126; I10 §3.15.1 pp.119–121; I11 §3.16.1 pp.152–154]

| Field / message | Interpretation and future implementation constraint |
|---|---|
| COR `protocol` | RTCM3=1, SPARTN=2; newer firmware also HAS=5; PMP=29, QZSSL6=30. Separate source attribution rather than “correction bytes seen”. [I11 §3.16.1] |
| COR `errStatus` | 0 unknown, 1 error-free, 2 erroneous. It is not a truthy “valid” bit. |
| COR `msgUsed` | 0 unknown, 1 not used, 2 used. Must compare enum values explicitly. |
| COR `correctionId` | RTCM reference station identity for supported fields; otherwise 0xFFFF. Message type/subtype have explicit validity bits. |
| COR `msgInputHandle` | Input handling exists, **not** proof the message is supported/used. |
| NAV-PVT | Primary `carrSoln` 1 float / 2 fixed, plus validity and differential status/age. Confirms solution state, not alone correction-source provenance. [IM §2.4.4.1 p.25] |
| NAV-SAT / NAV-SIG | Satellite correction flags and signal correction source/use flags identify actual per-satellite/per-signal application. IM describes RTCM3 OSR attribution. [IM §2.4.4.1 p.25] |
| NAV-RELPOSNED / NAV-STATUS | Differential/relative validity and float/fixed fields, with role-specific interpretation. [IM §2.4.4.1] |
| NMEA GGA/GNS/RMC | GGA quality 4 fixed / 5 float; correction age and station ID; VRS GGA feedback needed. HAS can produce float without OSR RTK, so label source separately. [IM §2.4.4.1, App.D.2; R10 §2] |
| MON-COMMS / buffers | Distinguish physical input/output traffic, protocol counters and buffer loss from correction usage. [IM §3.3, §3.20.4] |
| NAV2 ≥2.11 | Secondary standard/SBAS solution may remain available beside primary RTK/PPP; keep primary and secondary result models separate. [IM §3.5; R11 §2] |

A future verification ladder should distinguish: host relayed bytes → receiver parsed messages → error-free/used correction reports → fresh NAV-SIG/NAV-SAT application → valid primary float/fixed solution. Silence is not equivalent to “not used”, and an unknown enum is not evidence of use. This is a proposed evidence model, not a performed receiver test.

## 8. Rates and operational limits

- D00/D01 §1.2 report **up to 25 Hz** PVT/SBAS, RTK/PPP-RTK and RAW; footnotes describe primary navigation output and conditions, not guaranteed simultaneous full telemetry throughput. D01 also lists moving-base heading up to **5 Hz**, a different constraint. The ArduSimple page variously says 20 Hz and up to 25 Hz; use chip/firmware-specific limits rather than marketing text. [D00 p.6; D01 pp.5–6; APROD]
- `CFG-RATE-MEAS=40` ms and NAV=1 is the carrier's 25 Hz example. MSGOUT=1 means every applicable event/solution, =2 every second, not universally 1/2 Hz. Lower output divisors/rates and navigation rate must be considered together. [IM §2.2.7–2.2.8 pp.19–20; ACFG Rover 25Hz row]
- UART supported rates listed by modern IM: 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 2000000, 4000000, 8000000 (8N1). Allow typically 100 ms after a baud-rate change before input at the new rate. Buffer overflow drops new outgoing messages; too many framing errors can temporarily disable UART RX. These are chip capabilities, not proof the attached USB adapter/host supports 8 Mbaud. [IM §3.3.1 pp.39–40]
- For **full debug/raw message sets**, IM recommends 2 Mbaud at 1–10 Hz, 4 Mbaud at 10–20 Hz, 8 Mbaud at 20–25 Hz. Those are not minimums for a lean RTCM-only base. A future profile needs actual frame-size/bandwidth measurement and host parsing/queue limits; no measured throughput is claimed here. [IM §3.20.4 Table 60 p.76]
- Moving-base latency guidance: keep latency below navigation update period minus 50 ms; receiver can reduce rate to 1 Hz if link latency prevents the configured rate. Moving base and rover measurement/navigation rates must match. [IM §2.4.6 pp.28–30]
- D01 operational specification includes ≤4 g, 80,000 m altitude and 500 m/s velocity; these are separate from update rate. RTK accuracy/convergence figures are conditional, not survey-in absolute accuracy guarantees. [D01 §1.2 pp.5–6]

## 9. Repository-specific compatibility constraints (read-only baseline)

These are code-inspection findings, not changes or runtime verification. Line references are to the stated baseline.

| Existing path | Why X20 desk evidence changes the assumption |
|---|---|
| `src/sp_rtk_base/services/drivers/ublox.py:get_gnss_config` / `_get_gnss_config_locked`, lines 1390–1412 | Polls legacy CFG-GNSS and expects that exact response; absent from X20 interface inventories. Replace/gate in later implementation research. |
| `ublox.py:begin_correction_input`, lines 1636–1659 | Reads/enables `CFG_MSGOUT_UBX_RXM_RTCM_<port>` along with mandatory protocol keys. X20 uses COR, and an unknown key makes the whole VALGET/VALSET fail. This can block corrected pre-survey/rover correction injection before any bytes are written. |
| `ublox.py:_CorrectionTallyReader`, around lines 319–338 | Tally intercepts RXM-RTCM only; an X20 COR stream cannot satisfy that assumption without separate decoding. |
| `ublox.py` MON-VER read loop, lines 2718 onward | Already parses SW/HW/extensions including FWVER and PROTVER. Future identity capability selection must preserve exact FW and ROM, not normalize X20 into an F9P default. |
| Builtin `src/sp_rtk_base/profiles/builtin/ublox-f9p-base-standard.yaml` and F9P configuration reference | F9P profile is not evidence of X20 signal-plan, port, telemetry or firmware-key support; introduce separately researched capability-qualified profiles only in a later approved task. |

No source, dependency, simulator, profile, host or hardware file was changed. No tracked issue was closed/commented and the map was not modified.

## 10. Further research gaps / decision gates

Desk research resolves the principal protocol families and version differences. It does **not** certify actual hardware support. Remaining questions, with evidence that would close them:

1. **Actual unit identity and transport:** model/type, ROM base, loaded HPG version, all MON-VER extensions, physical connector/routing, and a usable host endpoint. `1546:01ab` only matches a configurable default. Requires a separately approved device/host investigation; not performed here.
2. **2.11 eligibility for ROM 1.00:** R11 lists only ROM 1.11 whereas R10 lists both. Obtain u-blox confirmation or an expressly applicable release/image note before recommending an upgrade. ArduSimple's factory-suffix/no-hardware-difference statement cannot override that omission.
3. **Earlier GLONASS capability:** D00/R00/R02 omit it while shared interface catalogs list its keys/MSM; modern R10/IM include it and geoblocking. Obtain an X20P-specific old-firmware capability statement. Do not silently infer enabled/usable GLONASS for 00B/2.00/2.02.
4. **4072.0/4072.1 catalog versus operational support:** generic catalogs predate declared moving-base introduction; output keys are modern. IM omits 4072.1. Clarify actual 4072.1 need, limits and receiver pairing, especially X20P↔F9P moving base. Stationary-base support does not depend on closing this gap.
5. **MSM5 range-rate wording versus input-only inventory:** R11 says corrected MSM5/MSM7 output but I11 lists MSM5 input-only. Confirm whether this is shared-stack release-note wording. Do not add MSM5 output to a profile without affirmative product documentation.
6. **Signal-specific interoperability:** target F9P SKU/firmware and third-party rover signal support; all-band MSM signal masks, message sizes, fragmentation/multiple-message-bit behavior and decoding tables. NavIC/QZSS reception is not evidence of corresponding RTCM MSM support.
7. **Configuration/library coverage:** exact installed pyubx2 key/message support for 50.xx/COR/NAV2/new defaults, unknown-key isolation, pagination, transaction/readback behavior, signed residual conversion and persistence layers. This task did not modify or exercise application code.
8. **TMODE state transitions and reset/save qualification:** cancellation/restart from existing fixed/survey states, stale NAV-SVIN, time-mode coordinate gating, BBR precedence and optional battery, approved reset reconnection. X20 docs support the concepts, not every F9P workaround's identical timing.
9. **Correction-source policy:** 2.10 host-versus-HAS selection versus 2.11 fallback and always-enabled host; distinguish COR source and primary result; HAS/OSNMA cannot be simultaneously enabled in R10/R11 limitations. ArduSimple's 2.11 HAS description includes the removed host key; the binary `.ucf` contents were not independently parsed here.
10. **Rate/throughput and deployment reality:** lean base versus 25 Hz rover profiles, serial/network bandwidth, output buffers, parser load and selected port defaults. Modern chip UART/USB limits are not measured Linux-driver or end-to-end throughput. Driver fixes or firmware updates are outside this approval.
11. **Primary-source inconsistencies:** D00 p.2 HPG 2.02 versus ordering p.28 HPG 2.10; D00's QZSS L1C/B/NavIC developmental listing versus old firmware signal list; IM generic L1 rule versus NavIC L5-only; R10 §2.4 comparison label HPG 1.50; IM p.2 points to 19895 for its 2.02 row although product page links 20040. Keep these discrepancies visible and obtain manufacturer clarification instead of “repairing” their identifiers or inferring equivalence.

### Boundary for follow-on work

A sensible later acceptance plan is identity/capability readout first, then offline library/simulator fixtures, then explicitly authorized RAM-only configuration and real base/rover streaming tests, and only finally separately authorized persistence/reset/upgrade qualification. None of these actions is authorized or executed by this desk-research document. The current result supports defining that next work; it does not justify automatically applying an F9P or ArduSimple preset to the unknown unit.

[D00]: https://content.u-blox.com/sites/default/files/documents/ZED-X20P-00B_DataSheet_UBXDOC-963802114-12690.pdf
[D01]: https://content.u-blox.com/sites/default/files/documents/ZED-X20P-01B_DataSheet_UBXDOC-304424225-21281.pdf
[IM]: https://content.u-blox.com/sites/default/files/documents/ZED-X20P_IntegrationManual_UBXDOC-963802114-12901.pdf
[I00]: https://content.u-blox.com/sites/default/files/documents/u-blox-20-HPG-2.00_InterfaceDescription_UBXDOC-304424225-19888.pdf
[I02]: https://content.u-blox.com/sites/default/files/documents/u-blox-X20-HPG-2.02_InterfaceDescription_UBXDOC-304424225-19967.pdf
[I10]: https://www.u-blox.com/sites/default/files/documents/u-blox-X20-HPG-2.10_InterfaceDescription_UBXDOC-304424225-21263.pdf
[I11]: https://content.u-blox.com/sites/default/files/documents/u-blox-X20-HPG-2.11_InterfaceDescription_UBXDOC-304424225-21617.pdf
[R00]: https://content.u-blox.com/sites/default/files/documents/HPG200_RN_UBXDOC-304424225-19895.pdf
[R02]: https://content.u-blox.com/sites/default/files/documents/HPG202_RN_UBXDOC-304424225-20040.pdf
[R10]: https://content.u-blox.com/sites/default/files/documents/HPG210_RN_UBXDOC-304424225-21261.pdf
[R11]: https://content.u-blox.com/sites/default/files/documents/HPG211_RN_UBXDOC-304424225-21629.pdf
[ABOARD]: https://www.ardusimple.com/user-guide-simplertk4-optimum/
[ACFG]: https://www.ardusimple.com/how-to-configure-u-blox-zed-x20p/
[APROD]: https://www.ardusimple.com/product/simplertk4-optimum-zed-x20p/
