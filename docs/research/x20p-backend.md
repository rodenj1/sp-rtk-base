# ZED-X20P backend and dependency assumptions

Research for [issue #249](https://github.com/rodenj1/sp-rtk-base/issues/249), under [map #247](https://github.com/rodenj1/sp-rtk-base/issues/247). Scope: ArduSimple simpleRTK4 Optimum / ZED-X20P, **both base and rover**. This is an audit and integration decision input, not an implementation or declaration of receiver support.

## Evidence boundary and outcome

All application citations below are immutable links to **8854c750f06ff321be29c9d29f9874484ce92b97**, version 0.10.0. Full issue body and comments were read (no comments at audit time). Only the isolated `research/x20p-backend` worktree was used. No receiver, serial device, running service, host configuration, or main-checkout files were accessed or changed. Dependency wheels were downloaded into scratch, matched against the SHA-256 hashes in this baseline's lockfile, and unpacked without installation. Offline probes below use explicitly synthetic payloads, not hardware observations.

**Finding:** adding a model label or a profile alone is insufficient. The u-blox transport is reusable in principle, but capabilities, schemas, fixed catalogs, connect-time repair, survey/reset quirks, and persistence policy currently carry F9/Gen9 assumptions. Rover use is presently an intermediate state of a corrected base survey, not a complete persistent rover workflow. Both a shared capability-aware driver and a separate X20-specific driver remain viable; protocol-dependent conclusions are **provisional pending reconciliation with the parallel chip/firmware research**.

Most consequential boundaries:

- The pinned `pyubx2` is **1.3.0**, not the `>=1.2.43` manifest floor and not today's 1.3.8. It includes some X20P definitions, but later releases add more. A parser's key catalog is not a receiver-support matrix.
- Identity accepts arbitrary `MOD=` strings, while profile validation accepts a closed model catalog; `ZED-X20P` is absent. A profile cannot currently target it.
- Every u-blox connection claims the same base capabilities and attempts a BBR TMODE repair, regardless of model/firmware.
- Whole-form apply and profile validation require base-output semantics, including a UART carrying 1005. They cannot represent an ordinary corrections-in / navigation-out rover with no base RTCM output.
- Unsupported keys can appear as off/zero, raise a `KeyError`, fail a whole batch, or be interpreted as an empty stored layer depending on the call site. These are different outcomes, not an established X20 capability probe.

## 1. Registry and abstract contract

| Baseline evidence | Assumption and implication |
|---|---|
| [Driver registry, lines 19–21, 43–102][registry] | Only production key is `ublox`, selecting `UbloxDriver` before any identity is read. Factory is vendor-key based, not model dispatch. `driver_key` compares exact class identity, so an unregistered subclass will not round-trip its key. |
| [API connection and saved vendor models][vendor-models]; [handoff, 761–793][handoff] | Connection requests and saved device profiles default to `ublox`; handoff stores the registry key and falls back to `ublox` when absent. A separate key must survive detection, UI selection, saved profiles, restart, and handoff—not merely import. |
| [Abstract interface, 112–137, 381–815][contract] | Synchronous I/O is wrapped by `asyncio.to_thread`. The ostensibly vendor-neutral contract contains TMODE, UART1/2/USB, UBX/NMEA/RTCM3X, BeiDou B2 and SPI fields, dense RTCM matrices, flash save, survey and correction input. There is no field-level capability/unsupported-state representation or complete rover role contract. |
| [Capability enum, 245–258][cap-enum]; [u-blox capability return, 398–407][caps] | The enum is coarse. All u-blox models claim survey-in, fixed base, RTCM selection, flash persistence, position/satellite and GNSS selection. No model/FW/protocol gating follows MON-VER. |
| [Detection policy, 52–109, 283–375][detection]; [default baud and links, 46–90][baud-default] | Default baud 57600 derives from the reference FTDI/F9P deployment. Sweep uses eight rates and 1.5 s per candidate; a first-rate hit is checked at a deliberately wrong rate to detect rate-indifferent USB. This is not evidence for the X20 board's native USB versus USB-UART wiring, baud limits, or enumeration path. |

A new driver must meet the existing broad ABC or the contract must evolve. Merely removing `SURVEY_IN` would switch the survey service to station averaging, but that still requires disable-base, high-precision position, fixed-base commit and saved-base restoration.

## 2. Identity and profiles: extensible read, closed write

[MON-VER parsing, 2691–2809][monver] reads SW/HW, `FWVER`, `PROTVER`, `MOD`, then explicit `ZED-`/`NEO-`/other names. [Identity resolution, 56–175][identity] accepts a reported model verbatim as confirmed, but has only F9P/F9R/M9N in `MODEL_FAMILY`; HPG without stronger evidence is inferred as F9P; protocol majors 27–33 are inferred as `gen9`. Do not invent an X20 HW version, firmware family, or protocol-to-generation mapping. A confirmed model name establishes identity, not the availability of every operation.

[Compatibility, 178–225][compatibility] prevents inferred identities from unlocking a specific-model profile, but permits family/`any` matches; inferred HPG→F9P can therefore carry a gen9 family. The resolver's introductory claim that arbitrary new-model YAML alone suffices is contradicted by [profile hardware validation, 245–261][profile-identity], which checks `KNOWN_MODEL_TOKENS` from the closed family table. Confirmed `MOD=ZED-X20P` can be displayed, but an X20P-tagged profile is rejected at baseline.

The only shipped [F9P profile, 1–60][builtin] reproduces HPG 1.12: 57600/115200 UART rates, all six constellations, 1 Hz, stationary dynamics, 15-degree elevation mask, BDS B2 disabled, SPI enabled, UART RTCM outputs, 1005, both 4072 subtypes, MSM4 and 1230. USB protocol settings and TMODE are omitted. **Do not retag this profile as X20 or `any`.** The [service's base-invariants check/remedy, 911–983][invariants] chooses it by a hard-coded name without a model-specific profile lookup; the remedy strips baud but still merges/applies its other settings.

[Profile/Apply envelope, 65–76, 175–242, 311–347][profile-role] requires at least one UART data-link port, 1005 on at least one, and a nonempty output row on every selected port. USB is excluded as a data-link port. [Receiver assertion, 278–303][assertion] requires all optimization and role fields even if a new receiver does not support them. [Merge, 660–739][merge] retains live optional fields, but RTCM matrix omission means off and omitted measurement period still means 1000 ms. This is a base-centric schema, not a role-neutral rover profile.

## 3. Hard-coded key/message/signal inventory

| Inventory and exact baseline source | What must be reconciled for X20 |
|---|---|
| [RTCM key bases, 105–126][key-catalog] | Exactly 1005, 4072.0, 4072.1, 1074/1077, 1084/1087, 1094/1097, 1124/1127, 1230. No configurable 1006, QZSS 111x or NavIC 113x, despite broader transport/parser possibilities. RTCM row identity preserves the two 4072 subtypes; relay's numeric message ID alone is 4072. |
| [Port and dynamics/TMODE maps, 129–185][key-catalog] | Protocol configuration only UART1/2/USB × IN/OUT × UBX/NMEA/RTCM3X. Dynamics mapping covers values 0,2–8. Other dynamics cause direct reverse-map lookup failure; unknown TMODE values in base reads instead default to disabled. Neither outcome means supported. |
| [Constellation/scalar keys, 187–242][key-catalog] | Six `CFG_SIGNAL_{GPS,GLO,GAL,BDS,SBAS,QZSS}_ENA`; no NavIC. Mandatory scalar batch includes both UART bauds, RATE_MEAS, NAVSPG_DYNMODEL, TMODE_MODE, INFIL_MINELEV, BDS_B2_ENA and SPI_ENABLED. All 17 TMODE mode/coordinate/accuracy/survey keys are used for BBR deletion/restoration. |
| [GNSS read/write, 1380–1510][gnss] | Legacy CFG-GNSS POLL and channel-block parsing remain; writes use all six constellation-enable keys at RAM+Flash. A post-write legacy poll is mandatory inside `configure_gnss`: inability to answer it raises even if key writes/readbacks succeeded. Connect-time probe catches the same failure. NavIC/IMES IDs are ignored. |
| [Scalar and optimization writers, 1720–1879][scalar-writers] | RATE_NAV pinned to 1; role TMODE write persists at layer 5; optimizations write the exact old BDS B2 and SPI keys. Scalar read directly indexes every key and enum mapping: omission is not optional/unknown. There is no individual signal selection beyond BDS B2. |
| [RTCM/protocol reads, 1283–1374][matrix-read] | RTCM read is 12 rows × 5 ports = 60 keys; missing returned keys become zero. Protocol missing keys become disabled. Unsupported I2C/SPI/message keys could affect whole-batch acceptance, yet the schema's write matrix covers only UART1/2/USB. |
| [Matrix writer, 1787–1803][matrix-write] | Writes all 36 cells assertively, converting booleans to rate 0/1. Existing nonzero rates become enabled booleans in the service assertion, so cadence greater than 1 is lost in this representation. Expanding to 13 rows × 5 read ports exceeds the pinned helper's 64-key cap; future reads need deliberate batching. |
| [NAV signal tables/filter, 2033–2125][signals]; [MSM adapter, 28–51, 98–125][msm]; [Band model][bands] | F9-oriented NAV signal mapping and MSM RINEX mapping judge only L1/L2. L5/E5a/E6/B2a/B3 etc. go to OTHER; NavIC is absent, SBAS deliberately excluded. NAV snapshot requires svUsed and above-mask elevation. MSM supports QZSS observation parsing although configuration has no QZSS rows. Full-band receiver quality cannot be inferred from these metrics. |

### Unit and snapshot assumptions

[Position parsing, 2155–2231][pvt] expects pyubx2 pre-scaled lat/lon, heading and DOP, but integer heights/accuracies in mm. [Survey sample, 1993–2031][hp-position] reads HPPOSECEF then PVT separately, uses merged HP ECEF in cm and `pAcc` in mm, `carrSoln` 0/1/2, `diffSoln`, and a lastCorrectionAge bucket. It does **not** check that the two messages share iTOW. Missing `invalidEcef` defaults to false and missing accuracy to zero; lack of correction-age evidence is rejected by corrected-survey freshness logic. [NAV-SVIN, 2323–2418][svin] reconstructs separate cm/0.1-mm HP components and divides meanAcc by ten. [Base config, 2432–2504][base-read] reads ECEF HP but omits LAT/LON/HEIGHT HP from the LLH readback, although fixed-base writes those HP fields. These distinctions need raw fixture-based unit tests, not wholesale reuse of one scaling rule.

## 4. Reset, ownership, timeout and failure behavior

- [F9P hardware reset, 571–643][reset] issues CFG-RST `pos=1, resetMode=0`, closes the handle, waits five seconds and opens the **same link/path**. The comments justify this specifically with HPG 1.12 survey duration behavior. [Survey start, 688–883][survey-start] uses a 500 ms disable-enable gap, two polls separated by two seconds, a 30 s stale-duration floor, and ignores `active` because of the old firmware quirk. These constants/behaviors are not X20 facts.
- [Service failure/cancel/reset paths, 858–909, 1021–1088][service-reset] automatically reset on survey-start failure and cancellation when the method exists. [SurveyService progress, 411–441][survey-progress] treats not-valid as active, again compensating for HPG 1.12. A shared-driver strategy must gate **both** driver and service quirks; a subclass overriding only one reset method does not resolve all assumptions.
- [Serial opener, 83–105][opener] requests exclusive open and adds a nonblocking flock; detection and audit have their own analogous open paths. Source inspection of **pyserial 3.5 `serial/serialposix.py:381–389`** shows `exclusive=True` is an advisory flock, not the TIOCEXCL kernel exclusion claimed in application comments. Cooperative owners are excluded; arbitrary non-locking opens are not guaranteed to be. No hardware test was attempted.
- [u-blox locks, 290–303, 348–358][locks] serialize every write through an RLock and hold a separate driver lock for read/poll/write-and-verify. Corrections take only the TX lock and can flow while a poll awaits its reply. There is no continuous dedicated telemetry reader; reset-input-buffer before most polls discards unsolicited output, including potentially some RXM-RTCM evidence.
- [Read budgets, 82–100, 245–287][deadlines] use three-second serial reads and 50 read attempts. Only MON-VER and MON-COMMS explicitly arm the deadline stream ([MON-VER][monver-deadline], [MON-COMMS][comms]). CFG/NAV/ACK paths are attempt-limited, **not** equivalent to a three-second wall-clock bound: an error-ignoring reader can continue internally on corrupt continuous bytes. Increased full-band traffic can consume the attempt budget before the relevant response.
- [ACK loop, 2946–2972][ack] accepts any ACK-ACK/ACK-NAK; `expected_msg` is error text, not matched against acknowledged class/ID. [CFG-VALGET, 1078–1165][cfg-read] similarly accepts the first VALGET response without layer/position/request correlation. NAK is distinguished by text; transient retry is three attempts and never retries a NAK. Most parse exceptions are swallowed. Position/survey-status timeouts return default no-fix/inactive objects ([PVT][pvt-default], [SVIN][svin]), unlike explicit exceptions in high-precision NAV polling.
- [Connect completion, 456–460][after-connect] attempts GNSS probe, saved-config repair and port identification. **Connect is not read-only**: [repair, 2857–2944][repair] can delete all TMODE keys from BBR, even on a newly recognized X20 unit; it does not distinguish configuration legitimately owned by another tool from old application residue. Failure is best effort/logged, not a connect failure.
- [Console identification, 2510–2613][comms] compares MON-COMMS snapshots around 16 NAV-STATUS polls with 200 ms settle. RTCM protocol slot is located using protIds, but UBX count is assumed to be slot 1. Actual X20 port IDs/counter layout and board link mapping need confirmation. Unknown console port is explicitly retained, not guessed.

## 5. Apply, save/readback and host persistence

[DeviceService apply, 1280–1571][apply] guards UBX input on the identified console port; when unknown, guards drops on all ports. It checks a survey's `active` flag before a TMODE change—different from the survey service's not-valid compensation—and rejects fixed mode only when displayed LLH is exactly all zero. It performs a fresh pre-read, skips equal steps except previously warned steps, then measurement, protocols, constellations, optimizations, dynamics, TMODE, matrix, baud. Writes already performed are not rolled back when a later step fails; subsequent steps skip. A final RAM assertion diff determines overall status. An empty diff can yield `status=ok` even with warnings or a failed step; callers must inspect steps and persistence warnings, not the headline alone. There is no single transaction spanning the whole apply.

[Baud handling, 1643–1764][baud-handoff] reopens only the known console UART; USB avoids UART reopen. Unknown console checks the current link, then changed UART rates. Known-port reopen failure tries the prior baud once and reports failure even when contact is restored; it never rewrites old receiver settings. The [Bluetooth guard, 1602–1641][bluetooth-baud] refuses the attached UART baud change because the external module cannot follow it. A failed baud step is not added to `changed_bauds`, so an applied-but-unacknowledged change can strand final readback. The [handoff endpoint, 733–802][handoff] validates before disconnect, saves vendor/port/baud, closes the driver, points the relay serial input at the same path, then starts it. This is exclusive ownership transfer, not simultaneous navigation/config access on a second receiver port. The service's relay mutex is global even if distinct physical ports exist.

Persistence is operation-dependent:

1. [Survey start][survey-start] uses RAM only. [Fixed base, 948–1063][fixed] pre-disables in RAM, clears BBR TMODE, writes mode, both LLH and ECEF including HP at RAM+Flash, retries RAM ECEF mismatch, then requires exact Flash config and no BBR TMODE. Dual coordinate writes and transition gap originate from F9P observations and are provisional for X20.
2. [Generic write/verify, 1167–1268][verify] retries once; RAM mismatch raises. Flash differing/omitted/NAK results become advisories after retry, but a transport timeout still raises. An apply success does not guarantee persistence. BBR precedence is only explicitly checked by the fixed-base path, not every generic layer-5 write.
3. [Stored keys and restore, 2881–2944][repair] interprets **any** NAK as an empty stored layer, overlays BBR over Flash, and treats missing saved mode as off. Unsupported-layer/key NAK is indistinguishable from genuinely empty storage; that distinction must be established before migration/repair.
4. [Save button, 1906–1926][save] sends legacy CFG-CFG saveMask with `devFlash=1`, checks ACK only, and does no durable readback. It is not equivalent to verified keywise Flash persistence. The ABC says correction input must be restored before saving, but [DeviceService.save_to_flash, 1259–1274][service-save] directly delegates; it does not itself enforce that precondition for arbitrary concurrent calls.
5. [ConfigService atomic private writes, 111–136, 245–261][host-save] persist application YAML separately from the receiver; [device profile save, 650–666][device-save] stores connection preferences. [ProfileStore][profile-store] saves/imports validated named YAML profiles, not a full opaque receiver backup. Saving either host file proves nothing about receiver RAM/BBR/Flash.
6. [Configuration audit, 40–45, 132–140, 195–239, 280–360][audit] polls catalog-derived keys in groups of 12 with fresh readers, compares only RAM versus factory default (layers 0/7), and collapses NAK/timeout to missing keys. It does not inspect Flash/BBR or verify reboot persistence. The JSON output emits differences/readable entries rather than a complete failure ledger ([audit output, 498–528][audit-output]); unreadable keys are not proof of defaults or support. Do not run this tool behind an active owner.

## 6. Base survey versus persistent rover

[SurveyService, 235–307][survey-role] chooses receiver averaging based on the static capability set; corrected surveying always disables base mode, enables correction input, starts a dedicated destination-free relay and averages the receiver as a rover. [Input setup/restore, 1636–1718][correction-input] remembers keys, turns on RTCM3 input and RXM-RTCM reports, suppresses RTCM3 output in RAM, and restores only keys that were returned. Unknown console touches every schema port. Partial setup/restoration cannot reconstruct settings omitted by an unsupported/partial read.

[CorrectionFeed, 82–98, 132–178][feed] subscribes after the engine starts; initial frames before subscription may be missed. It concatenates whole queued frames in order, taking another whole frame while accumulated size is below 4096 (thus 4096 is a threshold, not a strict maximum). Successful driver write increments delivery counters, not receiver-use proof. Failed batches are skipped and counted; it keeps pumping. [Driver write][correction-input] ignores the returned write byte count; partial writes are not explicitly validated. RXM-RTCM use flags and MON-COMMS counters are diagnostics, not complete reliable telemetry because polling drains receive buffers.

[Survey observation policy, 50–86, 168–185, 461–577][survey-observe] samples approximately once per second, requires valid fixed solutions held for 30 seconds with correction age ≤10 seconds, warns at 60 seconds without observations and aborts at 600. It restarts averages after three consecutive >5 cm fixed jumps, applies an overall cap, and uses max(mean estimated accuracy, spatial standard deviation), not standard error. Distinct-message iTOW/fix/age support must be established for X20 before those observations can be trusted. [Commit and cleanup, 701–788][survey-commit] stops the feed and restores input before committing fixed base, aborting if restore fails; failures/cancel return to the saved base. This is useful infrastructure for base positioning but **not** a perpetual rover client.

A rover integration needs an explicit durable role (TMODE disabled, appropriate dynamics/rates/signals, corrections-in, navigation-out), an input source with maintained correction delivery and optional caster GGA policy, independently useful solution telemetry, and lifecycle behavior that does not eventually commit a fixed base or restore a saved base on termination. Neither a shared nor separate driver alone supplies these service/schema changes.

[Correction-source verification, 38–50, 92–117, 171–277][correction-verify] proves network stages and CRC-valid RTCM3 plus 1005/1006 in its observation window; it never talks to the receiver. It does not prove usable signals/message types, freshness, coordinate reference compatibility, receiver ingestion, RTK fixed, or rover navigation output. In the unpacked relay 4.1.0, `config.py:129` begins NtripInputConfig; inspection found no GGA configuration field there and no periodic GGA implementation in the NTRIP input source. Treat VRS/GGA requirements as unresolved, not supported by a green source verification.

## 7. Dependency support: locked versus available upstream

[Manifest, 25–36][manifest] permits ranges. The [lockfile][lockfile] selects `pyubx2=1.3.0` (2562–2572), `pyrtcm=1.1.12` (2382–2391), `pynmeagps=1.1.4` (2360 onward), `pyserial=3.5` (2394 onward), and `sp-rtk-base-relay=4.1.0` (2779–2791). Evidence below refers to the hash-verified wheel contents from that lock, not the machine's installed packages.

| Dependency | Pinned-source finding | Upstream distinction / implication |
|---|---|---|
| pyubx2 1.3.0 | `pyubx2/ubxmessage.py:280–320` applies declared scaling and merges `_HP` attributes. `:718–837` implements config_set/del/poll, each limited to 64 entries; application uses transaction=0, poll position=0, without chunking or response pagination. `ubxtypes_get.py:2184–2200` merges HPPOSECEF HP values and scales pAcc by 0.1; NAV-PVT includes lastCorrectionAge. `ubxtypes_configdb.py:1194–1196` includes GPS L5 key and `:320–324` includes RTCM1006 output keys, but no TYPE1117_USB or TYPE1137_USB key was present. | [Release notes v1.3.8][ubx-releases] say 1.2.53 added X20P keys, 1.2.57 updated X20P HPG2.00 NAV definitions, 1.3.3 added more X20P keys, and 1.3.7 added X20 HPG2.11 support/new config keys. Thus locked 1.3.0 is not devoid of X20 support, but not current-complete either. Manifest floor 1.2.43 precedes those additions. Reconcile an explicit required version and fixture-tested field/key set instead of assuming any allowed version works. |
| pyrtcm 1.1.12 | `pyrtcm/rtcmtables.py:16–149` includes GPS L5/E5a and other signal labels and NavIC mappings. It is broader than application catalogs. Parsing ability does not enable receiver output keys. | [Release notes v1.2.0][rtcm-releases] identify 1.1.12 invalid-length handling and 1.2.0 filtering/metadata parsing enhancements. No inference that latest is required just for full-band MSM follows. Validate actual recorded X20 MSM fragmentation/signal labels and app band handling first. |
| relay 4.1.0 | `sp_rtk_base_relay/core/frame_subscription.py:1–88`: CRC-valid whole-frame subscriptions, before destination filtering, bounded queue of 100, nonblocking drop-newest. `core/broadcast_hub.py:658` onward separates raw destinations from frame-filtered/subscriber paths. `rtcm_decoder.py:279–532` validates framing/CRC and extracts numeric IDs without semantic payload decoding. Serial input requests exclusive open (`core/input_sources/serial_input.py:103`). | Receiver-independent RTCM3 transport can potentially carry new IDs/signals without payload changes; subscribers, selected message filters, queues and serial bandwidth still matter. Latest repository README fetched during research calls itself 2.1.3 despite locked 4.1.0; do not use that stale prose to override the verified artifact. No latest relay version claim or upgrade recommendation is made. |
| pyserial 3.5 / pynmeagps 1.1.4 | pyserial's POSIX exclusivity is flock; pynmeagps is a transitive parser dependency of the GNSS libraries. | Their presence proves host-side framing/stream APIs, not X20 USB adapter mapping or device write/read semantics. |

### Executed offline checks (no receiver)

Using only the unpacked locked GNSS packages on Python's temporary import path:

```text
Literal keys known to pinned database: 31
Supplementary keys:
 CFG_SIGNAL_GPS_L5_ENA: True
 CFG_MSGOUT_RTCM_3X_TYPE1006_USB: True
 CFG_MSGOUT_RTCM_3X_TYPE1117_USB: False
 CFG_MSGOUT_RTCM_3X_TYPE1137_USB: False
 CFG_NAVCOR_ENABLE_HOST: False
UBXMessageError Number of configuration keys 65 exceeds maximum of 64
UBXMessageError Number of configuration tuples 65 exceeds maximum of 64
Synthetic HPPOSECEF: 12345.1 -12345.1 123.4
```

The 31 count is an AST-selected set of **literal strings exactly matching the locked database**, not a count of all generated keys or proof of complete coverage. The HP fixture sets raw X/Y cm to ±12345, HP to ±10 and raw pAcc to 1234; resulting X/Y and pAcc demonstrate the parser's merged/scaled semantics. No synthetic frame was presented as an X20 measurement. The absent NAVCOR key is especially not proof the receiver rejects host corrections: upstream notes say that configuration changed again in HPG2.11. No application test suite or receiver end-to-end tests were run; this deliverable is documentation only.

## 8. Alternatives without locking the choice

| Alternative | Reuse and strengths | Costs and acceptance boundary |
|---|---|---|
| Shared `UbloxDriver` + model/FW/protocol capability policy | Keep vendor key and MON-VER/serial/locking/framing/reconnect code. Move supported CFG keys, ports, RTCM rows, signal tables, persistence policy and quirks into explicit model/FW policy. Expose supported/unsupported/unknown separately; gate repair and destructive operations before writes. | Must untangle static capabilities and mandatory assertions, then gate quirks outside the driver too. Needs regression fixtures for F9P HPG1.12 and new firmware, not just X20 fixtures. Appropriate if reconciled protocol shows largely common semantics with bounded differences. Avoid broad `gen9+` claims. |
| Separate `UbloxX20PDriver` or model-selected strategy with shared transport helpers | Isolate X20 config catalog, persistence/reset/survey semantics and supported capabilities; explicit registry key can make operator intent clear. A subclass can reuse transport, or a dispatch factory can identify then select a strategy. | Registry is currently chosen before identity; dispatch must avoid a second serial owner and before-model connect repair. Separate key requires saved-key/UI/detection/handoff compatibility. Inheriting UbloxDriver wholesale inherits destructive repair and F9 behavior unless overridden. Duplicating transport risks deadline/lock fixes drifting. Still requires role-neutral profiles/assertions and persistent rover service. Appropriate if protocol/firmware differences make shared policy complex or hazardous. |

**Common work regardless of choice:** role-aware base/rover profiles and validation; supported-field discovery/state; per-model catalog and rate limits; bounded/correlated batched reads; explicit persistence/readback contract; full-band signals; serial/correction lifecycle; fixtures and documented compatibility matrix. No code changes are proposed as already implemented.

## 9. Protocol reconciliation and remaining research gates

Before a design choice or implementation ticket is treated as ready, reconcile these concrete questions with chip/board research:

1. What exact X20P firmware and MON-VER MOD/HW/PROTVER strings will be supported? What authoritative model identity supports family matching without HPG→F9 inference?
2. Which CFG keys, message outputs, ports and layers are supported on each targeted firmware? Specifically TMODE, BDS_B2, SPI, SBAS/GLONASS/NavIC, every old RTCM output key, RXM-RTCM reports, and new full-band/QZSS/NavIC outputs. Does an unsupported key NAK the entire mixed batch or produce omission? What are the supported request sizes and pagination semantics?
3. Are CFG-GNSS, CFG-CFG, CFG-RST and CFG-VALDEL supported with the same forms/semantics? Is NAK at a stored layer actually empty or unsupported? Is Flash present/writable on the board/module and does BBR outrank it? Can connect-time repair safely be disabled for unknown/new units?
4. Are fixed base/survey-in available and do both LLH/ECEF need writing? What is the defined behavior of HP fields, uncertainty, survey valid/active/duration and disable-enable transitions? Which old F9 empirical quirks must **not** apply? Is a destructive reset ever necessary?
5. Are NAV-PVT/HPPOSECEF/SVIN/SAT/SIG and MON-COMMS versions/units/counter slots unchanged? Does correction age have the same bucket meanings? Can samples be epoch-correlated? Are validity/accuracy fields authoritative across base/rover modes?
6. What are the board's actual native USB/USB-UART/UART/socket connections, enumeration/re-enumeration identities, throughput limits and safe reconnect timing? Can console and data links be separate and should the current global mutex remain?
7. Which RTCM types/signals are generated versus consumed, with what station/reference/datum assumptions? What is the X20 triple/full-band output bandwidth and maximum-rate MSM fragmentation behavior? Does downstream Sure-Path equipment accept it or require restricted signals/messages? Frame relay correctness alone does not answer this.
8. What rover output is needed (UBX high precision, NMEA, another consumer), at what rate, and what continuous correction client lifecycle is required? Are GGA/VRS, authentication/TLS/reconnect and correction-age policies needed? Corrected surveying is not the rover deliverable.
9. What exact dependency floor/lock revision supplies the necessary keys/messages? Test key IDs/types and raw recorded payloads against the reconciled firmware docs; add fixtures for missing keys/layers, unrelated ACK, partial VALGET, dead serial links, queue drops, partial write and after-reset persistence. Upgrade only with an explicit reason and regression evidence.

Bench acceptance is intentionally future work: identify without writes; prove chosen console/data mapping; test unsupported-state behavior and protected liveness; apply/read back RAM/Flash/BBR as appropriate; reset/power-cycle verify; prove base corrections and rover ingestion/fixed/navigation separately; repeat F9 regressions. None was attempted during this research.

## Immutable baseline source links

[registry]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/__init__.py#L19-L102
[contract]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/base.py#L112-L815
[vendor-models]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/models/api_models.py#L235-L275
[cap-enum]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/models/device_models.py#L245-L258
[caps]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L398-L407
[detection]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/base.py#L52-L375
[baud-default]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/models/device_models.py#L46-L90
[monver]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2691-L2809
[identity]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/models/hardware_identity.py#L56-L175
[compatibility]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/models/hardware_identity.py#L178-L225
[profile-identity]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/models/profile_models.py#L245-L261
[builtin]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/profiles/builtin/ublox-f9p-base-standard.yaml#L1-L60
[invariants]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/device_service.py#L911-L983
[profile-role]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/models/profile_models.py#L65-L347
[assertion]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/models/profile_models.py#L278-L303
[merge]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/models/profile_models.py#L660-L739
[key-catalog]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L105-L242
[gnss]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1380-L1510
[scalar-writers]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1720-L1879
[matrix-read]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1283-L1374
[matrix-write]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1787-L1803
[signals]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2033-L2125
[msm]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/signal_quality/msm.py#L28-L125
[bands]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/models/signal_quality_models.py#L21-L27
[pvt]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2155-L2231
[hp-position]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1993-L2031
[svin]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2323-L2418
[base-read]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2432-L2504
[reset]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L571-L643
[survey-start]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L688-L883
[service-reset]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/device_service.py#L858-L1088
[survey-progress]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/survey_service.py#L411-L441
[opener]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/console_link.py#L83-L105
[locks]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L290-L358
[deadlines]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L82-L287
[monver-deadline]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2628-L2689
[comms]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2510-L2613
[ack]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2946-L2972
[cfg-read]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1078-L1165
[pvt-default]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1942-L1974
[after-connect]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/device_service.py#L456-L460
[repair]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L2857-L2944
[apply]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/device_service.py#L1280-L1571
[baud-handoff]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/device_service.py#L1643-L1764
[bluetooth-baud]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/device_service.py#L1602-L1641
[handoff]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/api/device.py#L733-L802
[fixed]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L948-L1063
[verify]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1167-L1268
[save]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1906-L1926
[service-save]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/device_service.py#L1259-L1274
[host-save]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/config_service.py#L111-L261
[device-save]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/config_service.py#L650-L666
[profile-store]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/profile_store.py
[audit]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/cli/config_audit.py#L40-L360
[audit-output]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/cli/config_audit.py#L498-L528
[survey-role]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/survey_service.py#L235-L307
[correction-input]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/drivers/ublox.py#L1636-L1718
[feed]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/correction_feed.py#L82-L178
[survey-observe]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/survey_service.py#L50-L577
[survey-commit]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/survey_service.py#L701-L788
[correction-verify]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/src/sp_rtk_base/services/correction_verification.py#L38-L277
[manifest]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/pyproject.toml#L25-L36
[lockfile]: https://github.com/rodenj1/sp-rtk-base/blob/8854c750f06ff321be29c9d29f9874484ce92b97/uv.lock
[ubx-releases]: https://github.com/semuconsulting/pyubx2/blob/v1.3.8/RELEASE_NOTES.md
[rtcm-releases]: https://github.com/semuconsulting/pyrtcm/blob/v1.2.0/RELEASE_NOTES.md
