# X20P firmware-specific application key and parser contract

Research for [#253](https://github.com/rodenj1/sp-rtk-base/issues/253), under [map #247](https://github.com/rodenj1/sp-rtk-base/issues/247), 2026-10-12. Application baseline **8854c750f06ff321be29c9d29f9874484ce92b97 (v0.10.0)**. Target is ArduSimple simpleRTK4 Optimum / ZED-X20P, **stationary base and rover**. This resolves a desk/offline contract, not architecture, implementation, firmware-update eligibility or hardware compatibility. Actual receiver model, ROM and installed firmware remain unknown. No device, serial endpoint, deployed application, host configuration or network port was accessed.

## Outcomes

- The driver's complete finite key-builder expansion is **112 distinct keys**, not the earlier literal-only count of 31. All 112 encode and decode in both tested library sets, but only **83 / 99 / 109 / 109** appear in the respective 2.00 / 2.02 / 2.10 / 2.11 interface catalogs. These are **catalog counts, not counts of working receiver features**; older GLONASS and generic B2I entries need product-level qualification.
- **The locked parser already decodes RXM-COR.** Replacing the app's RXM-RTCM-only setup/tally is local firmware-specific handling, not a reason by itself to upgrade pyubx2. Its old RXM-RTCM keys are absent from all four X20 catalogs; sending one in a mandatory batch can reject the entire operation.
- pyubx2 1.3.8 supplies missing named HAS/host, DF028 antenna-height, QZSS L1C/B and range-rate-sign keys. It still contains the **removed 2.11 host-enable key**. A newer database cannot select receiver capabilities. Ordinary stationary 1005/MSM4/MSM7, TMODE, COR and the exercised primary NAV fields do not require these additions merely to serialize/parse.
- pyrtcm 1.1.12 already decodes the exercised GPS L5, Galileo E5a/E6 and BeiDou B2a/B3 MSM signals. Its signal tables equal 1.2.0's. Relay 4.1.0 accepts their CRC-valid frames and an unknown numeric message ID without semantic decoding. The app's L1/L2-centric quality mapping, receiver output selection and opaque transport are separate contracts.
- Real offline execution produced **165 locked cases (156 successful, 9 expected errors)** and **164 candidate cases (160 successful, 4 expected errors)**. The one-case difference is the unchanged relay check, run only in the locked environment. All bytes are explicitly **synthetic**, not captured hardware.

## Evidence and artifacts

Read the four completed resolution comments ([248](https://github.com/rodenj1/sp-rtk-base/issues/248#issuecomment-6129416837), [249](https://github.com/rodenj1/sp-rtk-base/issues/249#issuecomment-6129417553), [250](https://github.com/rodenj1/sp-rtk-base/issues/250#issuecomment-6129418251), [251](https://github.com/rodenj1/sp-rtk-base/issues/251#issuecomment-6129418967)) and their immutable artifacts: [protocol][prior-protocol], [backend][prior-backend], [UI][prior-ui], [validation][prior-validation]. The map and #253 had no comments at retrieval. The four child resolution comments explicitly distinguish the existing RXM-RTCM consumer from X20's intended RXM-COR path.

Primary interface descriptions retrieved **using web/Firecrawl extraction first**, in full:

| Label | Document / actual revision | Firmware / protocol |
|---|---|---|
| [I00] | UBXDOC-304424225-19888 R01, 22-May-2025 | HPG 2.00 / 50.02; shared X20P/F20P |
| [I02] | UBXDOC-304424225-19967 R03, 13-Nov-2025 | HPG 2.02 / 50.10; shared X20P/F20P; R03 adds USB documentation |
| [I10] | UBXDOC-304424225-21263 R02, 17-Jun-2026 | HPG 2.10 / 50.11; X20P |
| [I11] | UBXDOC-304424225-21617 R01, 25-Aug-2026 | HPG 2.11 / 50.11; X20P |

Also retrieved/read [R00] (7-May-2025), [R02] (16-Oct-2025), [R10] (15-May-2026), [R11] (24-Aug-2026), [IM] R05 (19-Aug-2026), and the prior protocol artifact's data-sheet reconciliation. **2.10 and 2.11 have identical protocol version**; preserve MON-VER FWVER, software build, module and ROM evidence rather than dispatching on 50.11 alone. R11 lists only ROM 1.11 support; no upgrade eligibility follows for the unknown unit.

Canonical, machine-readable evidence under [`assets/x20p-key-parser-contract/`](assets/x20p-key-parser-contract/):

- [`app-key-contract.csv`](assets/x20p-key-parser-contract/app-key-contract.csv): every one of the 112 exact expanded names, numeric ID, type, unit, layer contract, app use groups, four firmware statuses, caveats and printed primary-manual pages.
- [`locked-results.json`](assets/x20p-key-parser-contract/locked-results.json) and [`candidate-results.json`](assets/x20p-key-parser-contract/candidate-results.json): per-key independent VALGET response bytes, actual VALSET serializer bytes, decoded values, exact errors, schemas and signal tables. Each firmware key match retains ID/type, extraction line, printed page and a short context excerpt. These excerpts explain presence rather than implying product usability.
- [`supplemental-contract.json`](assets/x20p-key-parser-contract/supplemental-contract.json): 27 relevant keys outside the baseline expansion, including COR, 1006, signal plan/bands, HAS, station ID and range-rate controls. Not a complete receiver catalog.
- [`message-contract.json`](assets/x20p-key-parser-contract/message-contract.json): 17 baseline message identities plus proposed COR, with exact source lines and firmware presence.
- [`summary.json`](assets/x20p-key-parser-contract/summary.json): deduped counts, expected errors, candidate key additions and SHA-256 of full extracted manuals.
- [`offline_contract.py`](assets/x20p-key-parser-contract/offline_contract.py), [`aggregate.py`](assets/x20p-key-parser-contract/aggregate.py): reproducible research-only scripts. They execute only AST-selected pure enums/builders/fixture encoder nodes; they do not import the application or open hardware. Aggregate asserts counts, document IDs/types and the exact expected error set.

Full proprietary manuals are not committed. Cached Firecrawl full extractions used here are `/home/ubuntu/.hermes/cache/scratch/x20p-manual-extracts/{2.00,2.02,2.10,2.11}.md`; their hashes are in summary.json. Reproduction needs fresh full extractions of the revision-matched official URLs, not the head/tail text shown by the tool. Extraction-line numbers refer to these particular texts; **printed pages and official URLs are the portable citations**.

Status vocabulary: **documented** means affirmative catalog/message evidence; **absent/unknown** means not found in that revision, not an observed NAK; **removed** requires explicit release-note evidence; **product-uncertain** preserves shared-catalog/product conflicts. No status here means bench-observed.

## 1. Exact baseline key expansion and firmware qualifications

The baseline pure `_rtcm_key` and `_protocol_key` functions were actually executed, not approximated by a string search. Source: [catalog/builders L102–242][builders], [RTCM read L1274–1326][matrix], [protocol read L1333–1374][protocol-read], [correction setup L1636–1659][correction], [scalar writers L1720–1879][scalar], [TMODE repair L2857–2944][repair].

| Use group | Exact expansion | Count / operation |
|---|---|---|
| RTCM matrix read | 12 row bases × I2C/UART1/UART2/USB/SPI | 60 keys, one read batch |
| RTCM matrix write | Same 12 × UART1/UART2/USB | 36 keys; subset of read inventory |
| Port protocols | UART1/UART2/USB × IN/OUT × UBX/NMEA/RTCM3X | 18 keys |
| Correction-report setup | RXM-RTCM × UART1/UART2/USB | 3 keys; known console uses 1 port, unknown uses all 3 |
| Scalar assertion | 8 scalar keys + six constellation enables | 14; overlaps other groups |
| All TMODE keys | mode, type, ECEF/LLH main+HP, accuracy, survey limits | 17; overlaps scalar/literal group |
| Rate writer | RATE_MEAS + RATE_NAV | 2; overlaps literal group |
| Exact literal keys in DB | AST-selected complete key literals | 31; **not** a complete generated-key inventory |
| **Deduped union** | RTCM 60 + protocols 18 + correction 3 + remaining literals 31 | **112** |

Scope of this finite inventory: normal u-blox driver key reads/writes/repair, not the generic config-audit CLI's database-derived arbitrary catalog polling or caller-supplied debug reset bit names. No polling tool was run.

### Scalar, constellation and TMODE keys

All following numeric IDs and types match catalog entries in **all four** manuals, with units below; exact per-version printed pages are in CSV/JSON. Readability/persistence of an actual unit is not implied. Sources: I00/I02/I10/I11 named CFG-NAVSPG, RATE, SIGNAL, SPI, TMODE and UART groups; I11 TMODE pp.245–247, RATE pp.239–240, SIGNAL pp.242–244. `L` is a one-byte logical value; `E1`, `I1`, `I4`, `U2`, `U4` denote the documented byte-width/type, not a scaling operation performed by config_set.

| Exact Python key | ID | Type | Raw unit / meaning |
|---|---|---|---|
| CFG_NAVSPG_DYNMODEL | 0x20110021 | E1 | enum; app maps only 0,2–8 |
| CFG_NAVSPG_INFIL_MINELEV | 0x201100a4 | I1 | degrees |
| CFG_RATE_MEAS | 0x30210001 | U2 | ms; documented scale 0.001 s |
| CFG_RATE_NAV | 0x30210002 | U2 | measurements per navigation solution; app writes 1 but scalar assertion omits it |
| CFG_UART1_BAUDRATE | 0x40520001 | U4 | baud |
| CFG_UART2_BAUDRATE | 0x40530001 | U4 | baud |
| CFG_SPI_ENABLED | 0x10640006 | L | interface enable; not proof of carrier SPI accessibility |
| CFG_SIGNAL_GPS_ENA | 0x1031001f | L | constellation enable |
| CFG_SIGNAL_SBAS_ENA | 0x10310020 | L | constellation enable |
| CFG_SIGNAL_GAL_ENA | 0x10310021 | L | constellation enable |
| CFG_SIGNAL_BDS_ENA | 0x10310022 | L | constellation enable |
| CFG_SIGNAL_QZSS_ENA | 0x10310024 | L | constellation enable; QZSS requires GPS |
| CFG_SIGNAL_GLO_ENA | 0x10310025 | L | **product-uncertain in 2.00/2.02**, region-gated in modern firmware |
| CFG_SIGNAL_BDS_B2_ENA | 0x1031000e | L | **B2I, not B2a**; B2I is absent from the product-supported signal lists |
| CFG_TMODE_MODE | 0x20030001 | E1 | disabled=0 / survey=1 / fixed=2 |
| CFG_TMODE_POS_TYPE | 0x20030002 | E1 | ECEF=0 / LLH=1 |
| CFG_TMODE_ECEF_X | 0x40030003 | I4 | cm |
| CFG_TMODE_ECEF_Y | 0x40030004 | I4 | cm |
| CFG_TMODE_ECEF_Z | 0x40030005 | I4 | cm |
| CFG_TMODE_ECEF_X_HP | 0x20030006 | I1 | 0.1 mm signed residual |
| CFG_TMODE_ECEF_Y_HP | 0x20030007 | I1 | 0.1 mm signed residual |
| CFG_TMODE_ECEF_Z_HP | 0x20030008 | I1 | 0.1 mm signed residual |
| CFG_TMODE_LAT | 0x40030009 | I4 | 1e-7 degree |
| CFG_TMODE_LON | 0x4003000a | I4 | 1e-7 degree |
| CFG_TMODE_HEIGHT | 0x4003000b | I4 | cm, ellipsoidal ARP height |
| CFG_TMODE_LAT_HP | 0x2003000c | I1 | 1e-9 degree signed residual |
| CFG_TMODE_LON_HP | 0x2003000d | I1 | 1e-9 degree signed residual |
| CFG_TMODE_HEIGHT_HP | 0x2003000e | I1 | 0.1 mm signed residual |
| CFG_TMODE_FIXED_POS_ACC | 0x4003000f | U4 | 0.1 mm |
| CFG_TMODE_SVIN_MIN_DUR | 0x40030010 | U4 | seconds |
| CFG_TMODE_SVIN_ACC_LIMIT | 0x40030011 | U4 | 0.1 mm |

Every HP residual accepts −99…+99. Each fixed-coordinate family is used according to MODE and POS_TYPE; docs do **not** establish that X20 needs the app's F9-specific dual LLH+ECEF write workaround. Survey duration and accuracy conditions both matter. Config serializer values are raw integers; the app multiplies accuracy mm by 10 and splits coordinates itself ([fixed writes L948–1063][fixed]). The offline −1 tests verify signed serialization, not a geodetically correct complete profile.

The app's dynamics values 0,2–8 occur in all four CFG-NAVSPG-DYNMODEL enum tables (I00/I02/I10 Table 25, I11 Table 26). R02 §2 adds product MOWER functionality (value 11), even though I00's shared table already lists MOWER with “not available in all products”. Other catalog values 9–15 likewise must not be inferred as X20P features solely from an enum table. An actual MOWER read currently has no app reverse mapping; a parser upgrade does not add one.

### RTCM output keys: all 60 cells

Name is exactly `CFG_MSGOUT_RTCM_3X_TYPE<row>_<port>`, with `4072_0` and `4072_1` as separate name components. All are U1 **output dividers**: zero off, N every N relevant events/epochs; not N messages per epoch. These IDs are present in both parser databases even when absent from the receiver manual. Sources: four CFG-MSGOUT groups/defaults; I11 §4.3 pp.176–177; R10 §2.4.1.

| Row name component | I2C | UART1 | UART2 | USB | SPI |
|---|---|---|---|---|---|
| 1005 | 0x209102bd | 0x209102be | 0x209102bf | 0x209102c0 | 0x209102c1 |
| 1074 | 0x2091035e | 0x2091035f | 0x20910360 | 0x20910361 | 0x20910362 |
| 1077 | 0x209102cc | 0x209102cd | 0x209102ce | 0x209102cf | 0x209102d0 |
| 1084 | 0x20910363 | 0x20910364 | 0x20910365 | 0x20910366 | 0x20910367 |
| 1087 | 0x209102d1 | 0x209102d2 | 0x209102d3 | 0x209102d4 | 0x209102d5 |
| 1094 | 0x20910368 | 0x20910369 | 0x2091036a | 0x2091036b | 0x2091036c |
| 1097 | 0x20910318 | 0x20910319 | 0x2091031a | 0x2091031b | 0x2091031c |
| 1124 | 0x2091036d | 0x2091036e | 0x2091036f | 0x20910370 | 0x20910371 |
| 1127 | 0x209102d6 | 0x209102d7 | 0x209102d8 | 0x209102d9 | 0x209102da |
| 1230 | 0x20910303 | 0x20910304 | 0x20910305 | 0x20910306 | 0x20910307 |
| 4072_0 | 0x209102fe | 0x209102ff | 0x20910300 | 0x20910301 | 0x20910302 |
| 4072_1 | 0x20910381 | 0x20910382 | 0x20910383 | 0x20910384 | 0x20910385 |

Firmware qualifications apply to the entire grid:

| Cells / feature | HPG 2.00 | 2.02 | 2.10 | 2.11 |
|---|---|---|---|---|
| 1005, GPS/Galileo/BeiDou MSM4/7, non-USB | documented output | documented | documented | documented |
| USB column | absent/unknown | documented | documented | documented |
| GLONASS 1084/1087/1230 | generic catalog; usable X20P feature unknown | same | documented product support, region-gated | retained, region-gated |
| All 4072.0/4072.1 cells | absent output keys, despite generic message catalog | same | keys documented | keys documented |
| 4072.1 operational need | unknown | unknown | unresolved: interface lists it, IM operational tables omit it | same |

Stationary base does not need 4072. Observation messages may emit before a usable reference position; coordinate output requires completed survey/fixed initialization. Ordinary rover input uses source 1005/1006 + compatible observations; it need not generate a base matrix. Receiver stream consistency (one MSM family, matching station IDs/rates) and signal overlap are in [IM §2.4]. A 13th row read across five ports would be 65 keys and must be batched; adding a database entry alone does not solve that.

### Protocol and correction-report keys: exact remaining 21

Protocol name is `CFG_<port><IN|OUT>PROT_<protocol>`. All are L. UART protocol keys are documented in all manuals; USB keys are **absent/unknown in I00**, documented in I02/I10/I11. Underlying input/output protocol enabling is separate from periodic message enabling. Sources: named CFG-UART1/2INPROT/OUTPROT and USBINPROT/OUTPROT groups in all interfaces.

| Prefix | UBX | NMEA | RTCM3X |
|---|---|---|---|
| CFG_UART1INPROT_ | 0x10730001 | 0x10730002 | 0x10730004 |
| CFG_UART1OUTPROT_ | 0x10740001 | 0x10740002 | 0x10740004 |
| CFG_UART2INPROT_ | 0x10750001 | 0x10750002 | 0x10750004 |
| CFG_UART2OUTPROT_ | 0x10760001 | 0x10760002 | 0x10760004 |
| CFG_USBINPROT_ | 0x10770001 | 0x10770002 | 0x10770004 |
| CFG_USBOUTPROT_ | 0x10780001 | 0x10780002 | 0x10780004 |

The three existing `CFG_MSGOUT_UBX_RXM_RTCM_{UART1,UART2,USB}` keys are respectively **0x20910269 / 0x2091026a / 0x2091026b**, U1. They are known to pyubx2 but **absent from every X20 catalog**, as is the RXM-RTCM message. COR replacements `CFG_MSGOUT_UBX_RXM_COR_{I2C,UART1,UART2,USB,SPI}` are **0x209106b6 / b7 / b8 / b9 / ba**, U1. COR USB is absent in I00; other COR ports are documented throughout. Actual board connector reachability is not conferred by the key catalog.

## 2. Supplemental firmware contract: don't import a library superset

These are not currently app-used keys. All use the same layer mechanism below; logical and enum values are dimensionless unless stated. Complete 27-entry evidence is in supplemental JSON. Sources: CFG-SIGNAL/NAVCOR/RTCM/MSGOUT groups, R10 §2.4.1 and R11 §2.1.

| Key / exact ID | Type/unit | 2.00 | 2.02 | 2.10 | 2.11 | Locked parser |
|---|---|---|---|---|---|---|
| CFG_SIGNAL_PLAN 0x2031003a | E1; SP1=1, SP2=2, SP6=6 | SP1/SP2 | adds SP6 | retained | retained | named key present |
| CFG_SIGNAL_NAVIC_ENA 0x10310026; NAVIC_L5_ENA 0x1031001d | L | documented L5 | documented L5 | documented L5 | retained | present; app model omits NavIC |
| CFG_SIGNAL_GPS_L5_ENA 0x10310004 | L | documented | documented | documented | documented | present; health/plan separate |
| CFG_SIGNAL_GAL_E5A_ENA 0x10310009; GAL_E6_ENA 0x1031000b | L | documented X20P signals | documented (E6 X20P-specific) | documented | retained | present; plan/enable/use remain distinct |
| CFG_SIGNAL_BDS_B1C_ENA 0x1031000f; BDS_B2A_ENA 0x10310028; BDS_B3_ENA 0x10310010 | L | documented X20P signals | documented (B3I X20P-specific) | documented | retained | present; B2a is not old B2I control |
| CFG_SIGNAL_QZSS_L1CB_ENA 0x10310039 | L | absent | absent | added | retained | missing named key |
| CFG_NAVCOR_ENABLE_GAL_HAS 0x100d0002 | L | absent | absent | added | retained, HAS fallback | missing named key |
| CFG_NAVCOR_ENABLE_HOST 0x100d0001 | L | absent | absent | added | **explicitly removed; host always enabled** | missing in 1.3.0; **present in 1.3.8 despite removal** |
| CFG_RTCM_DF028_OUT 0x30090010 | U2, 0.0001 m antenna height | absent | absent | added | retained | missing named key |
| CFG_RTCM_MSM_OUT_RANGERATE_SIGN 0x10090011 | L, default true in I11 | absent | absent | absent | added | missing named key |
| CFG_RTCM_DF003_OUT 0x30090001; DF003_IN 0x30090008 | U2, station ID 0..4095 | documented | documented | documented | documented | present |
| CFG_RTCM_DF003_IN_FILTER 0x20090009 | E1 | documented | documented | documented | documented | present |
| CFG_MSGOUT_RTCM_3X_TYPE1006_<port> | U1 divider; I2C/UART1/UART2/USB/SPI IDs 0x209102c2/c3/c4/c5/c6 | input only, output keys absent | same | output added | retained | present even in 1.3.0 |

NAV2 secondary solution/output is **2.11-only documented feature**, not an app consumer. A library's existing NAV2 names or other receiver-family keys do not enable it on 2.10. The examined research-key delta additionally includes LBAND enable/PMP keys, but they are not required for this app's ordinary RTCM stationary-base/rover path. Don't expand scope into SPARTN accessory support on the strength of those additions.

## 3. Exact message and decode boundary

Sources: all interface §3.8 message inventories and named message descriptions. I00 explicitly names VALGET/SET/DEL in §6.4.1 but omits their detailed §3.10 wire entries; it is not evidence of unsupported key configuration. For detailed wire semantics use I02/I10/I11. The app has 17 literal identities; proposed COR is an additional message, not a renamed RTCM response.

| Message | Class/ID | Firmware status | Required app-facing decode/semantics |
|---|---|---|---|
| ACK-ACK / ACK-NAK | 05/01, 05/00 | all documented | ACK class/ID refers to acknowledged request, not arbitrary success |
| CFG-CFG | 06/09 | all documented | broad layer clear/save/load; not legacy subsection semantics |
| CFG-RST | 06/04 | all documented | do not wait for ACK; exact reset scope below |
| CFG-VALSET / VALGET / VALDEL | 06/8a, 06/8b, 06/8c | high-level I00; detailed ≥2.02 | version/layer/position/transaction and typed key/value handling |
| CFG-GNSS | 06/3e (library/legacy ID) | **absent all four** | baseline poll/channel-block response contract is not justified |
| MON-VER | 0a/04 | all documented | CH30 software + CH10 hardware + repeated CH30 extensions; retain raw identity |
| MON-COMMS | 0a/36 | all documented | version 0; 8-byte header + 40 bytes/port; protIds selects counter slots |
| NAV-STATUS | 01/03 | all documented | 16-byte payload; app uses poll for console fingerprint |
| NAV-PVT | 01/07 | all documented | 92-byte payload; lat/lon 1e-7° pre-scaled by parser; height/accuracy mm; carrSoln/diffSoln/lastCorrectionAge |
| NAV-HPPOSECEF | 01/13 | all documented | 28-byte version-0 payload; ECEF main cm + HP 0.1mm merged by parser; pAcc 0.1mm pre-scaled to mm; invalidEcef |
| NAV-SVIN | 01/3b | all documented | 40-byte version-0; main ECEF cm and HP 0.1mm remain **separate**; meanAcc raw 0.1mm; valid/active/dur |
| NAV-SAT | 01/35 | all documented | version 1, 8 + 12×numSvs; svUsed/rtcmCorrUsed/elevation |
| NAV-SIG | 01/43 | all documented | version 0, 8 + 16×numSigs; gnssId/sigId/corrSource/used+correction flags |
| RXM-RTCM | 02/32 (library/legacy ID) | **absent all four** | baseline `_WatchedReader` intercepts only this identity |
| RXM-COR | 02/34 | all documented | version 1, 12-byte payload; parser coverage established offline |

Baseline line anchors for consumers: [GNSS L1390–1510][gnss], [watcher L318–335][watcher], [position/sample L1993–2231][nav-consumers], [survey L2323–2418][svin], [MON-VER L2691–2809][monver], [MON-COMMS L2510–2613][comms]. The parser's group/bitfield **schema container names change** between 1.3.0 and 1.3.8 (`group`→`sv_grp`/`sig_grp`, `flags`→`flags_bit`, etc.). Exercised public flattened attributes and scales remain the same. Don't claim schemas identical from those passes; retained raw schemas make the differences auditable.

### RXM-COR versus the existing tally, narrowly scoped

I00 §3.16.1 pp.118–120, I02 §3.16.1 pp.125–126, I10 §3.15.1 pp.119–121, I11 §3.16.1 pp.152–154 define COR. Both libraries expose: version, ebno (0.125 dB scale), protocol (5 bits), errStatus (2), msgUsed (2), correctionId (16), msgTypeValid, msgSubTypeValid, msgInputHandle, msgEncrypted (2), msgDecrypted (2), msgType U2, msgSubType U2. Synthetic RTCM3 example decoded **protocol=1, errStatus=1, msgUsed=2, correctionId=42, ebno=10.0, msgType=1077** in both libraries.

This establishes schema coverage, **not a complete replacement telemetry policy** (that is separate research #254). COR reports successfully parsed messages even if unsupported or unused. `msgUsed=2` means used; 0 unknown, 1 not used. `errStatus=1` means error-free, 2 erroneous, 0 unknown. Do not reuse a truthiness check or assume a COR attribute such as RTCM's crcFailed exists. Type/subtype validity and protocol attribution matter. Existing tally/setup must be firmware-qualified locally; upgrading the package leaves RXM-RTCM-only code unchanged.

CFG-GNSS must instead use qualified `CFG_SIGNAL_*` reads and plan/band dependencies. The six existing enable keys do not describe individual signals. GAL_E5B and BDS_B2I generic catalog entries are not evidence of X20P usable navigation bands; product lists and plan constraints prevail. MON-GNSS can describe supported plans, while NAV-SIG reports tracked/used signals, not the configured enable state. GPS L5 health and QZSS/GPS dependencies must not be lost in a checkbox rewrite. [IM §2.2.4–2.2.5; I11 CFG-SIGNAL; prior protocol §5]

## 4. Unsupported batches, layers, save/delete/reset

Exact detailed sources: I02/I10/I11 §3.10 and configuration-interface §6; I11 VALDEL pp.64–66, VALGET pp.66–67, VALSET pp.67–69, layers pp.204–205, transaction semantics pp.206–207. I00 lacks detailed message wire entries, so identical firmware behavior is not a measured or complete-wire-documented 2.00 claim.

| Operation | Wire/layer contract | Consequence for baseline |
|---|---|---|
| VALGET | ≤64 requested IDs, ≤64 response pairs. Selector RAM=0, BBR=1, Flash=2, Default=7. Unknown key or invalid layer returns NAK. | Optional/removed keys cannot share a mandatory batch safely; one unknown key rejects poll. Separate layer requests, retain missing/NAK/timeout distinctions. |
| Pagination | position skips already returned key/value pairs. Wildcards can exceed 64 pairs; <64 ends enumeration, exactly 64 may require next page. | `_read_cfg_keys_locked` uses position=0 and no paging. App's explicit 60-cell matrix fits today; expanding catalog requires deliberate splitting. |
| VALSET v0 | ≤64 pairs; **mask** RAM=1, BBR=2, Flash=4. Unknown key/no layer/invalid RAM configuration → NAK **and no configuration applied for that message**. | Layer 5 is RAM+Flash, not all layers. Library accepting a key does not prevent firmware NAK. Whole-form apply consists of separate messages and is not all-or-nothing. |
| VALSET v1 transaction | start/continue/apply state; same layer mask; another CFG request including VALGET cancels transaction without applying. | Don't read back midway through a future transaction. Existing transaction=0 calls do not obtain multi-message transaction protection. |
| VALDEL | ≤64 keys; delete BBR=2 / Flash=4 masks, not RAM/default; does not change effective RAM until reloaded. Known already-unset/deleted items are acknowledged. Unknown key/no delete layer → NAK/no applied change. | TMODE BBR deletion is a persistent modification, not a read-only cleanup. An unsupported key NAK is not proof BBR is empty. |
| Layer precedence | RAM is current; saved BBR overrides saved Flash, then Default when rebuilding. Default immutable, saved layers are overrides and can omit keys. | RAM ACK/readback is not durable verification; stale BBR can shadow a layer-5 Flash write at restart. Physical backup power controls BBR retention. |
| CFG-CFG | Any nonzero clearMask clears all selected saved config; any nonzero saveMask copies all current config; any nonzero loadMask rebuilds RAM. Omitted deviceMask defaults BBR+Flash if available. | Old subsection fields in pyubx2's serializer do not restore old receiver semantics. Save may persist temporary diagnostic/protocol changes too. |
| CFG-RST | No ACK expected. hot 0x0000 / warm 0x0001 / cold 0xffff navBbrMask. | Cold navigation start is not Flash factory reset. Do not validate by ACK timeout. |
| Reset scope | IM §3.6.1 Table 43 p.49: hardware 0x00/0x04 or RESET_N clears RAM/BBR/RTC; controlled SW 0x01 clears RAM and selected BBR; GNSS-only 0x02/stop0x08/start0x09 affect navigation RAM, not complete config. Flash survives. | Baseline F9P five-second same-path reopen, BBR repair and survey dur workaround are **not X20 qualified**. Reconnect/readback after separately authorized lifecycle test. |

These are **global documented layer mechanisms**, not a promise that every key is stored on every physical unit or that the unknown board has working Flash/backup battery. No per-key exception was found for the enumerated catalog keys; actual layer availability, lockdown, saved-value omission and lifecycle durability remain future evidence gates. Unsupported keys have no valid firmware-layer claim. Minimize Flash writes; RAM-only corrections setup/restore must not silently become a persistent profile. [IM §2.1, §3.13.2]

Baseline danger points: generic write/readback [L1167–1268][verify] treats Flash mismatch/NAK as advisory; stored reads [L2881–2944][repair] collapse **any NAK** to empty stored layer; connect invokes repair; save [L1906–1926][save] checks ACK only. These cannot establish the above distinctions without local policy/readback changes. No new parser version fixes them.

## 5. Actual library experiments and dependency decisions

### Environments and commands executed

Isolated scratch virtual environments, Python **3.14** (full version in result JSON), no system-wide installation and no repo lock changes:

```sh
python -m venv /home/ubuntu/.hermes/cache/scratch/x20p-parser-locked
/home/ubuntu/.hermes/cache/scratch/x20p-parser-locked/bin/pip install \
  pyubx2==1.3.0 pyrtcm==1.1.12 pynmeagps==1.1.4 sp-rtk-base-relay==4.1.0
python -m venv /home/ubuntu/.hermes/cache/scratch/x20p-parser-candidate
/home/ubuntu/.hermes/cache/scratch/x20p-parser-candidate/bin/pip install \
  pyubx2==1.3.8 pyrtcm==1.2.0 pynmeagps==1.1.7
```

An initial candidate install **failed dependency resolution** with pynmeagps==1.1.4: pyubx2 1.3.8 requires ≥1.1.7. Retried successfully with 1.1.7. pip warns locked pyubx2 1.3.0 is yanked as obsolete, but an explicit pin installed successfully. These environments reproduce the **named parser/relay version pins**, not the entire application lock environment; relay's ancillary dependencies were resolved by pip, no Bluetooth/dbus service was run. No application test suite was run.

From this exclusive research worktree:

```sh
/home/ubuntu/.hermes/cache/scratch/x20p-parser-locked/bin/python \
  docs/research/assets/x20p-key-parser-contract/offline_contract.py \
  --manual-dir /home/ubuntu/.hermes/cache/scratch/x20p-manual-extracts \
  --output docs/research/assets/x20p-key-parser-contract/locked-results.json
/home/ubuntu/.hermes/cache/scratch/x20p-parser-candidate/bin/python \
  docs/research/assets/x20p-key-parser-contract/offline_contract.py \
  --manual-dir /home/ubuntu/.hermes/cache/scratch/x20p-manual-extracts \
  --output docs/research/assets/x20p-key-parser-contract/candidate-results.json
python docs/research/assets/x20p-key-parser-contract/aggregate.py \
  --manual-dir /home/ubuntu/.hermes/cache/scratch/x20p-manual-extracts
```

All final commands exited 0. Aggregate checks the expected error ledger rather than treating caught exceptions as successful tests. Intermediate additional tests were added and rerun; **only the final 165/164-case artifacts are authoritative**.

| Probe | Locked result | Candidate result | Interpretation |
|---|---|---|---|
| Each 112 app keys: actual config_set serialize, independent expected raw key/value bytes, parse SET, independently packed VALGET then parse expected value | 112 passed | 112 passed | library encoding/decoding coverage, including keys absent on X20; no receiver acceptance |
| 64 / 65 keys for poll/set/delete | 64 accepted; each 65 raises UBXMessageError | same | upgrade does not raise receiver/library cap |
| Poll selectors 0/1/2/7 with position=64, SET masks 1/2/4/5/7 | all serialized | all serialized | correct wire distinction; not readback/persistence test |
| COR version1/status flags | expected enum/ID/ebno values | same | no parser update needed solely for COR |
| PVT / HPPOSECEF / SAT / SIG / SVIN / MON-COMMS synthetic independent layouts | all asserted public fields/scales passed | same | primary consumer coverage; does not test every firmware bit/reserved field |
| Named HOST / GAL_HAS / range-rate-sign / DF028 / QZSS_L1CB | all five undefined-key errors | all encode | targeted candidate catalog addition, not automatic firmware authorization |
| PLAN / 1006_USB / COR_USB / NAVIC_ENA / NAVIC_L5_ENA | encode | encode | app/model gaps, not missing parser catalog |
| Numeric 0x10090011 with integer 1 | TypeError: unknown key inferred X001 requires bytes | known L001, integer works | don't use an untyped numeric workaround blindly |
| Same numeric ID with bytes b'\x01' | encodes generic X001 | TypeError: known L001 requires int | a workaround can break when later library learns the key; use explicit typed catalog policy |
| 17 single-cell MSM7 frames, selected new bands + QZSS/NavIC | parsed expected IDs/labels/CN0=45, serialization byte equality | same | full-band decoding already present, not receiver output ability |
| Relay CRC/framing+ID check, 1077/1097/1127/1117/1137 and unknown 4090 | all six accepted | not rerun; relay unchanged | opaque decoder supports frames independently of semantic parser |

**9 locked errors:** three intentional 65-key boundary errors, five intentionally missing candidate key names, one integer-vs-generic-X numeric-key error. **4 candidate errors:** three intentional 65-key boundaries and one bytes-vs-known-L numeric-key mismatch. None is a failed hardware test. The synthetic inputs are not navigation-valid correction observations and do not prove a rover can fix.

### Needed changes versus merely newer releases

Primary library references: [pyubx2 release notes][ubx-rn], [1.3.0 source][ubx-locked], [1.3.8 source][ubx-candidate], [pyrtcm release notes][rtcm-rn], [locked RTCM signal tables][rtcm-tables]. The application lock, not manifest floor, selects pyubx2=1.3.0 / pyrtcm=1.1.12 / pynmeagps=1.1.4 / relay=4.1.0 ([uv.lock][lock]).

| Requirement | Evidence-based decision input |
|---|---|
| Ordinary stationary 1005 + MSM4/7 + TMODE + primary NAV + COR | Locked serializer/parser covers the exercised contract. **Local model/FW filtering, COR consumer and GNSS read changes are still necessary.** Updating libraries alone is insufficient. |
| HPG2.10 named HAS/host, antenna height DF028, QZSS L1C/B control | Absent in locked names; 1.3.3 release reports added X20P keys, candidate supplies exact documented IDs/types. A targeted typed local definition or qualified dependency update is needed **if these controls enter scope**. Tests do not mandate using HAS. |
| HPG2.11 range-rate compatibility control | Missing locked name; 1.3.7 explicitly adds 0x10090011 and 2.11 support. Upgrade/local typed catalog necessary if exposing that control. **Still reject HOST on 2.11**, despite candidate accepting it. |
| Arbitrary unknown numeric/X config keys | 1.3.1 release describes spurious X validation fix; observed integer failure illustrates the locked path. Current app key set does not exercise generic X keys, so not an automatic blocker for it. |
| pyubx2 1.3.8 specifically | Release adds F11/M11 keys and internal advisories, not a demonstrated required delta for this X20 core path. Candidate is an examined superset, not a prescribed upgrade version. |
| pyrtcm 1.2.0 specifically | Adds filtering/metadata parsing. Signal tables match locked version and tested new bands parse identically. No demonstrated signal-decoding necessity to upgrade. |
| Relay upgrade | No demonstrated need for new MSM signal payloads/opaque unknown IDs. Filters, bounded subscriber queues, mixed-stream/fragments and byte fidelity need their own validation; offline decoder acceptance is not end-to-end forwarding proof. |

These tests compare declared schemas and selected independent fixtures; they are not exhaustive upstream regression suites. A dependency change must also retain F9 behavior and pin its transitive changes deliberately.

## 6. RTCM signal IDs versus application band interpretation

Actual pyrtcm serializer/parser results in both environments:

| MSM / signal IDs tested | Labels | Band |
|---|---|---|
| 1077 / 22,23,24 | 5I,5Q,5X | GPS L5 |
| 1097 / 8,9,10 | 6C,6A,6B | Galileo E6 |
| 1097 / 22,23,24 | 5I,5Q,5X | Galileo E5a |
| 1127 / 8,9,10 | 6I,6Q,6X | BeiDou B3 |
| 1127 / 22,23,24 | 5D,5P,5X | BeiDou B2a |
| 1117 / 22 | 5I | QZSS L5; **not** documented X20 MSM output/input inventory |
| 1137 / 22 | 5A | NavIC L5; **not** documented X20 MSM output/input inventory |

Each case carries a synthetic single satellite/cell, CN0 45, zero ranges/phases and epoch 123000, wraps CRC-24Q, parses through the actual RTCMReader, and serializes identically. The baseline fixture encoder is reused via pure AST-selected nodes ([fixture L20–110][msm-fixture]). Neither QZSS/NavIC parser support nor constellation reception establishes corresponding X20 RTCM generation.

The app maps RINEX labels to L1/L2/OTHER ([MSM adapter L28–51][msm-adapter], [NAV mapping L2033–2057][nav-consumers]). L5/E5a/E6/B2a/B3 currently become OTHER rather than qualified full-band metrics. Expand local mappings/quality semantics if wanted, not the receiver's output inventory merely to please a parser. **Opaque relay bytes may remain correct while UI labels are incomplete.** Relay's numeric 4072 identity also cannot distinguish subtypes; receiver config keys can.

Primary relay implementation inspected: [published 4.1.0 package][relay-package], `sp_rtk_base_relay/rtcm_decoder.py` L279–414 (CRC/preamble/length/12-bit ID, no semantic signal lookup), exercised directly in the locked environment. Prior backend research §7 audits the raw-versus-filtered/subscriber paths and queue limits; this report tests the decoder, **not a live relay pipeline**.

## 7. Remaining manufacturer/research questions and decision boundary

1. **Earlier usable GLONASS:** I00/I02 list GLO enable/MSM/1230, but X20P-00B data sheet (see prior protocol evidence) and [R00] §3.1/[R02] §2.1 supported signal lists omit it. Ask u-blox which hardware/FW/region combinations actually enable L1OF/L2OF. Catalog recognition, serialization and a NAK absence are not product evidence.
2. **4072 catalog versus operation:** generic older RTCM catalogs list 4072.0/4072.1, but output keys are absent; R10 announces moving-base/4072.0 output. Ask whether older X20P accepts/uses these as rover input and exactly when output became functional. Clarify 4072.1's purpose/necessity and receiver pairing; modern IM omits it from operational output tables. Stationary base does not depend on this answer.
3. **MSM5 wording:** R11 says the corrected DF399+DF404 sign affects MSM5/MSM7 output, whereas I11 §4.3 lists 1075/1085/1095/1125 as input only and exposes no output keys. Ask whether shared-stack wording or undocumented product output explains it. Do **not** invent TYPE1075 output keys or enable an MSM5 profile.
4. **Generic signal controls:** clarify older product signal availability and whether documented B2I/E5b generic enable keys can be accepted but unused; confirm plan-change atomicity/allowed combinations for every promised role/FW. A checkbox must not erase unrepresented enabled bands.
5. **Actual unit identity/ROM/ports/storage:** separately authorized capture of MON-VER, board endpoint mapping, saved layers and lockdown is required. R11 ROM1.11-only image list must be reconciled before any upgrade recommendation. No hardware access is authorized by this report.
6. **Receiver negative and lifecycle evidence:** after approval, capture individual known/unsupported polls, optional-key batch NAKs, pagination, RAM/BBR/Flash omission, key readback after restart and reset scopes. Synthetic library passes cannot answer receiver rejection, battery persistence or F9 survey timing workarounds.
7. **Implementation acceptance, not this ticket:** real mixed-stream/capture replay, exact dependency regression suite, same-epoch navigation samples, serial bandwidth, subscriber drops, base output and sustained rover correction use. Full telemetry policy remains separate #254; choice of shared versus separate driver remains a live human architecture decision.

**Boundary:** this contract establishes which documented key/message subset and tested parser paths can inform design. It neither adds X20P support nor recommends applying any F9P/ArduSimple preset. Issues/map were not edited, commented or closed; no PR was opened. Only research documentation/assets are committed on `research/x20p-key-parser-contract`.

## Source index

[I00]: https://content.u-blox.com/sites/default/files/documents/u-blox-20-HPG-2.00_InterfaceDescription_UBXDOC-304424225-19888.pdf
[I02]: https://content.u-blox.com/sites/default/files/documents/u-blox-X20-HPG-2.02_InterfaceDescription_UBXDOC-304424225-19967.pdf
[I10]: https://content.u-blox.com/sites/default/files/documents/u-blox-X20-HPG-2.10_InterfaceDescription_UBXDOC-304424225-21263.pdf
[I11]: https://content.u-blox.com/sites/default/files/documents/u-blox-X20-HPG-2.11_InterfaceDescription_UBXDOC-304424225-21617.pdf
[R00]: https://content.u-blox.com/sites/default/files/documents/HPG200_RN_UBXDOC-304424225-19895.pdf
[R02]: https://content.u-blox.com/sites/default/files/documents/HPG202_RN_UBXDOC-304424225-20040.pdf
[R10]: https://content.u-blox.com/sites/default/files/documents/HPG210_RN_UBXDOC-304424225-21261.pdf
[R11]: https://content.u-blox.com/sites/default/files/documents/HPG211_RN_UBXDOC-304424225-21629.pdf
[IM]: https://content.u-blox.com/sites/default/files/documents/ZED-X20P_IntegrationManual_UBXDOC-963802114-12901.pdf
[prior-protocol]: https://github.com/rodenj1/sp-rtk-base/blob/3672798bf962d638f2a97f4e198f340b47dc64d5/docs/research/x20p-protocol.md
[prior-backend]: https://github.com/rodenj1/sp-rtk-base/blob/251461cdfaa0102f7ded5205e9856b516addea58/docs/research/x20p-backend.md
[prior-ui]: https://github.com/rodenj1/sp-rtk-base/blob/6700a67c2d7d761a1ea0438a3c70363e071fd2e6/docs/research/x20p-ui.md
[prior-validation]: https://github.com/rodenj1/sp-rtk-base/blob/39bed71d0c3694fbb7b220c6b8bbf7f1ec22f0f4/docs/research/x20p-validation.md
[builders]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L102-L242
[matrix]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1274-L1326
[protocol-read]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1333-L1374
[correction]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1636-L1659
[scalar]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1720-L1879
[repair]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2857-L2944
[fixed]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L948-L1063
[gnss]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1390-L1510
[watcher]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L318-L335
[nav-consumers]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1993-L2231
[svin]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2323-L2418
[monver]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2691-L2809
[comms]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2510-L2613
[verify]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1167-L1268
[save]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1906-L1926
[msm-fixture]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/tests/unit/msm_frames.py#L20-L110
[msm-adapter]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/signal_quality/msm.py#L28-L51
[lock]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/uv.lock
[ubx-rn]: https://github.com/semuconsulting/pyubx2/blob/v1.3.8/RELEASE_NOTES.md
[ubx-locked]: https://github.com/semuconsulting/pyubx2/tree/v1.3.0/src/pyubx2
[ubx-candidate]: https://github.com/semuconsulting/pyubx2/tree/v1.3.8/src/pyubx2
[rtcm-rn]: https://github.com/semuconsulting/pyrtcm/blob/v1.2.0/RELEASE_NOTES.md
[rtcm-tables]: https://github.com/semuconsulting/pyrtcm/blob/v1.1.12/src/pyrtcm/rtcmtables.py
[relay-package]: https://pypi.org/project/sp-rtk-base-relay/4.1.0/
