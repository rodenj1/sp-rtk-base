# Research: per-field write / read-back coverage of the F9P hardware section

> **Ticket [#85](https://github.com/rodenj1/sp-rtk-base/issues/85)** · Map [#84](https://github.com/rodenj1/sp-rtk-base/issues/84) · Type: research
> **Code under review:** `main @ f7680d9` · **Live cross-check:** vaio-base, ZED-F9P HPG 1.51 / PROTVER 27.50 / HW 00190000 (read-only app-API GETs, 2026-08-31)
> **Companion finding:** [issue #82](https://github.com/rodenj1/sp-rtk-base/issues/82) / `docs/research/apply-config-dropped-fields.md` (branch `research/apply-config-dropped-fields`) — this document answers the *broader per-field* question #85 asks; #82 answers the narrower "are these four fields dropped, and why."

---

## TL;DR — verification of the three starting facts

The three facts given to this ticket are **all reframed** by the code at `main @ f7680d9`:

| Starting fact | Verdict at `f7680d9` |
|---|---|
| **1.** "The apply path (`apply_receiver_config`, ~line 825) **drops** `elevation_mask_deg`, `bds_b2_enabled`, `spi_enabled` — no driver write in the apply path." | **REFUTED as stated.** The service *does* write all three — `device_service.py:838-843` calls `driver.configure_optimisations(config.elevation_mask_deg, config.bds_b2_enabled, config.spi_enabled)`, and the driver (`ublox.py:1121-1146`) writes them with a per-write `CFG-VALGET` read-back. What drops them is the **UI layer**: `_apply()` builds the POSTed config *without* `form_extras` (`gps_config.py:1376`), so the three arrive at the service as `None` and `configure_optimisations` writes nothing. The drop is UI scoping (#65/#66), not a missing service/driver write. |
| **2.** "The BeiDou constellation write **FAILS** because `configure_gnss` cannot add a BeiDou block the device doesn't currently enumerate." | **Confirmed as a mechanism**, and *conditional*: `configure_gnss` (`ublox.py:999-1035`) emits exactly the blocks it is handed (built from the live `get_gnss_config` read, `device_service.py:825-836`), so it cannot *add* a block the firmware doesn't enumerate. Live today (2026-08-31) vaio-base **does** enumerate a BeiDou block (`enabled=true`), so a BeiDou write would now *succeed* — the failure is real only on a firmware image whose block list lacks BeiDou. (The #82 snapshot saw no BeiDou block + an IMES/NavIC block; the unit's enumeration has since changed.) |
| **3.** "#65/#66 deliberately scoped Apply to 'matrix + data-link ports only' — `form_extras` is excluded from apply and has no read-back reconciliation." | **Confirmed.** `_apply()` docstring states it explicitly (`gps_config.py:1361-1370`) and the call omits extras (`gps_config.py:1376`). **But the stated rationale — "those fields have no live read-back" — is factually wrong for 5 of the 7 fields** (see §2). baud, constellations, port protocols, dyn_model, and tmode_mode all have dedicated driver read-back getters; only `meas_period_ms` and the three optimisation fields lack a standalone getter. |

**Bottom line:** the service + driver already implement write *and* (for most fields) read-back for essentially the entire hardware section. The gap is (a) the UI's Apply button not *sending* the extras, and (b) four fields (`meas_period_ms`, `elevation_mask_deg`, `bds_b2_enabled`, `spi_enabled`) having no *standalone* read-back getter, so they can't be displayed or reconciled in-form — even though each *does* get a per-write `CFG-VALGET` verify when actually written.

---

## 1. Where the writes and reads actually live (code map)

**Apply path — `DeviceService.apply_receiver_config` (`device_service.py:727-880`), ordered layer=5 writes:**

| Step | Service line | Driver method | Driver lines |
|---|---|---|---|
| guards (UBX-in liveness, tmode-fixed coordinate check) | `789-810` | `get_base_config` | `1625-1733` |
| measurement rate | `814-816` | `configure_measurement_rate` | `1081-1090` |
| port in/out protocols | `818-823` | `configure_port_protocols` | `1041-1079` |
| constellations | `825-836` | `get_gnss_config` + `configure_gnss` | `942-997` / `999-1035` |
| optimisations (elev / BDS-B2 / SPI) | `838-843` | `configure_optimisations` | `1121-1146` |
| role: dyn_model | `845-846` | `configure_dyn_model` | `1092-1099` |
| role: tmode_mode | `847-848` | `configure_tmode_mode` | `1107-1119` |
| RTCM matrix (36 cells) | `850` | `apply_rtcm_matrix` | `1148-1164` |
| baud (last) | `852-857` | `configure_baud` | `1180-1195` |
| reopen at new UART1 baud | `865-866` | `reconnect_at_baud` | `1197-1220` |
| read-back verify (baud + matrix) | `868-880` | `get_uart_baud_rates` + `get_rtcm_port_config` | `1166-1178` / `786-835` |

**Verification ladder used by the write helpers:**

- **Strongest** — write + `CFG-VALGET` read-back + retry-once-on-mismatch, raise on second mismatch: `_write_and_verify_locked` (`ublox.py:750-780`). Used by `configure_measurement_rate`, `configure_port_protocols`, `configure_dyn_model`, `configure_tmode_mode`, `configure_optimisations`.
- **Endpoint-level** — bare write, then a *separate* full read-back diff owned by the caller: `apply_rtcm_matrix` uses bare `_send_cfg_valset_locked` (`ublox.py:1164`); `device_service.py:871-877` reads the matrix back and diffs all 36 cells, returning `status="failed"` + per-cell diff on mismatch (nothing rolled back).
- **Weakest** — write + ACK/NAK only, no value read-back in the write path: `configure_baud` (`ublox.py:1195`, bare `_send_cfg_valset_locked`) and `configure_gnss` (`ublox.py:1029`, `_wait_for_ack("CFG-GNSS")` on the legacy `CFG-GNSS SET` message). Baud is still functionally checked by the subsequent reopen (`device_service.py:865-866`) + `get_uart_baud_rates` read (`868`); the constellation write has **no** value read-back at all — only an ACK.

**The endpoint's `status` reflects only the RTCM matrix.** `device_service.py:868-880` reads back baud (for non-blocking throughput *warnings* only, `868-869`) and the RTCM matrix (status-determining, `871-880`). It does **not** read back rate, constellations, protocols, dyn_model, tmode, or the three optimisations at the endpoint level. Those are verified only *inside* their per-write `_write_and_verify_locked` calls — which run only when the field is non-`None` (today never, from the UI Apply path).

---

## 2. Per-field coverage table

"Standalone read-back" = a dedicated driver getter that a UI page or API endpoint can call to display/reconcile the field independently of a write. "Per-write verify" = the `CFG-VALGET` read done inside `_write_and_verify_locked` on each actual write. Live = value read from vaio-base on 2026-08-31.

| Field (form) | Write CFG key(s) | Write layer | Write helper | **Standalone read-back?** | Read-back key / getter | **In Apply path?** | Live vaio read (2026-08-31) | Known defect / note |
|---|---|---|---|---|---|---|---|---|
| `baud.uart1` | `CFG_UART1_BAUDRATE` | 5 (RAM+Flash) | `configure_baud` (bare VALSET, `ublox.py:1195`) | **YES** | `get_uart_baud_rates` (`ublox.py:1166-1178`) | yes — `device_service.py:852-857` (written *last*, #62) | no direct API endpoint (driver-only); link itself is 57600 (`/status`) | ACK-only write verify; real check is the post-write **reopen** (`device_service.py:865-866`) + baud read-back. UART1 is the console's own management link. |
| `baud.uart2` | `CFG_UART2_BAUDRATE` | 5 | `configure_baud` (`ublox.py:1190`) | **YES** | `get_uart_baud_rates` (`ublox.py:1177`) | yes — `device_service.py:852-857` | no direct API endpoint (driver-only) | No reopen for UART2 (not the console's link, `device_service.py:769-770`). |
| `meas_period_ms` | `CFG_RATE_MEAS` + `CFG_RATE_NAV=1` | 5 | `configure_measurement_rate` (`_write_and_verify_locked`, `ublox.py:1081-1090`) | **NO standalone** (per-write verify only) | `CFG_RATE_MEAS`/`CFG_RATE_NAV` via CFG-VALGET inside `_write_and_verify_locked`; **no `get_measurement_rate`** exists | yes — *always* written (`device_service.py:814-816`; not optional, defaults 1000) | cannot read (no API endpoint) | `CFG_RATE_NAV` pinned to 1 (NAV period tied to 1× meas period). No getter → not displayable, no in-form sync. |
| constellations `gps`/`glonass`/`galileo`/`beidou`/`qzss`/`sbas` | `UBX-CFG-GNSS SET` (legacy message, **not** CFG-VALSET) | n/a (CFG-GNSS) | `configure_gnss` (ACK only, `ublox.py:999-1035`); apply reconciles against live blocks (`device_service.py:825-836`) | **YES** | `get_gnss_config` → `CFG-GNSS POLL` (`ublox.py:942-997`); API `GET /api/device/gnss` | yes (if `constellations` non-None) — `device_service.py:825-836` | **all 6 enabled=true** (gps/sbas/galileo/**beidou**/qzss/glonass); no IMES/NavIC block | (a) cannot **add** a block the firmware doesn't enumerate; (b) cannot **remove** a block the profile omits (one-way asymmetry — a live IMES/NavIC block is silently dropped from the write); (c) `CFG-GNSS SET` is **deprecated** on protocol >23.01 — F9 family is moving to `CFG-VALSET` + per-constellation `CFG-SIGNAL-{GPS,SBAS,BDS,QZSS,GLO,IMES}_ENA`; (d) write has **no value read-back**, only ACK. |
| `ports` in/out, per-port (UART1/UART2/USB) | `CFG_{UART1,UART2,USB}{IN,OUT}PROT_{UBX,NMEA,RTCM3X}` (18 keys) | 5 | `configure_port_protocols` (**assertive** — writes every key on/off per touched port, `_write_and_verify_locked`, `ublox.py:1041-1079`) | **YES** | `get_port_protocols` → all 18 keys (`ublox.py:864-922`); API `GET /api/device/port-protocols` | yes (if `ports` non-empty) — `device_service.py:818-823` | UART1 in=[UBX,NMEA,RTCM3X] out=[RTCM3X]; UART2 in=[UBX,RTCM3X] out=[RTCM3X]; USB in=[UBX,NMEA,RTCM3X] out=[UBX,NMEA,RTCM3X] | USB-IN guard: apply refuses a config that turns UBX off on USB IN (console's own control channel, `device_service.py:789-796`). |
| `ports` in/out — **I2C / SPI** | `CFG_{I2C,SPI}{IN,OUT}PROT_*` | — | **NOT written** by the driver | **NO** (protocol keys) | protocol keys not in `_PROTOCOL_PORT_PREFIXES` (`ublox.py:92-96` = UART1/UART2/USB only); *RTCM cells* on I2C/SPI **are** read by `get_rtcm_port_config` (`RtcmOutputPort` incl. I2C/SPI, `device_models.py:236-243`) | no | I2C=0, SPI=0 on every RTCM row (from `/rtcm-ports`) | I2C/SPI are **advisory-only**: the profile matrix schema doesn't claim them (`RtcmStreamConfig` → `PortId` = UART1/UART2/USB only, `device_models.py:311-316`), and the UI shows a "this profile doesn't manage those ports" advisory when a row is on I2C/SPI (`i2c_spi_advisory_rows`, `gps_config.py:186-192`). No CFG-VALSET write, no protocol read-back for I2C/SPI. |
| `dyn_model` | `CFG_NAVSPG_DYNMODEL` | 5 | `configure_dyn_model` (`_write_and_verify_locked`, `ublox.py:1092-1099`) | **YES** | `get_dyn_model` (`ublox.py:1101-1105`); used internally by `check_base_invariants` (`device_service.py:441`) | yes (if non-None) — `device_service.py:845-846` | no API endpoint (driver/internal only); base-invariants check reads it | Value map `ublox.py:123-132` (portable=0, stationary=2, … 1 reserved). No `/dyn-model` API endpoint to display it in-form. |
| `tmode_mode` | `CFG_TMODE_MODE` | 5 | `configure_tmode_mode` (`_write_and_verify_locked`, `ublox.py:1107-1119`) | **YES** | `get_base_config` → `CFG_TMODE_MODE` + LLH/ECEF (`ublox.py:1625-1733`); API `GET /api/device/base-config` | yes (if non-None) — `device_service.py:847-848` | mode=**fixed**, pos_type=llh, 32.7328943/-117.2362775, 29.49 m, 596 mm | Coordinate guard: `tmode_mode=fixed` refused unless the receiver already holds a non-zero position (`device_service.py:798-810`). Note: this is a *plain mode assertion* — distinct from the full base-mode transitions in `configure_survey_in`/`configure_fixed_base` (which also write the position + SVIN keys). |
| `elevation_mask_deg` | `CFG_NAVSPG_INFIL_MINELEV` | 5 | `configure_optimisations` (`_write_and_verify_locked`, `ublox.py:1135`) | **NO standalone** (per-write verify only) | `CFG_NAVSPG_INFIL_MINELEV` via CFG-VALGET inside `_write_and_verify_locked`; **no getter** | yes (service) — `device_service.py:838-843` | cannot read (no API endpoint) | Unit ambiguity (see #82): HPG 1.51 IFD says unit=`deg`, default 10; profile target 15; an older repo doc annotates 15 as `0.1°` (1.5°) — 10× discrepancy, unresolved. |
| `bds_b2_enabled` | `CFG_SIGNAL_BDS_B2_ENA` | 5 | `configure_optimisations` (`ublox.py:1137`) | **NO standalone** (per-write verify only) | `CFG_SIGNAL_BDS_B2_ENA` via CFG-VALGET; **no getter** | yes (service) — `device_service.py:838-843` | cannot read (no API endpoint) | BeiDou B2I signal enable. Related to but distinct from the BeiDou *constellation* block (`CFG-GNSS`). |
| `spi_enabled` | `CFG_SPI_ENABLED` | 5 | `configure_optimisations` (`ublox.py:1139`) | **NO standalone** (per-write verify only) | `CFG_SPI_ENABLED` via CFG-VALGET; **no getter** | yes (service) — `device_service.py:838-843` | cannot read (no API endpoint) | The SPI *interface* enable. Distinct from the SPI RTCM output port (which is read but not written — see I2C/SPI row). |

**Read-back summary (of the 7 non-matrix hardware fields):** dedicated getter exists for **5** — `baud` (`get_uart_baud_rates`), constellations (`get_gnss_config`), port protocols (`get_port_protocols`), `dyn_model` (`get_dyn_model`), `tmode_mode` (`get_base_config`). **No standalone getter for 4** — `meas_period_ms`, `elevation_mask_deg`, `bds_b2_enabled`, `spi_enabled` (each still gets a per-write `CFG-VALGET` verify when actually written). The shipped page's docstring ("the driver has no read-back for most of those fields, baud excepted", `gps_config.py:21-25`) **understates** the coverage: it is only accurate for `meas_period_ms` + the three optimisation fields, and wrong for the other four.

---

## 3. The "no read-back" claim in the shipped page's docstring — audited

`gps_config.py:18-26` (module docstring):

> "…the ports/GNSS/baud/role section remains a read-only *display* of form state rather than a click-to-edit grid — **the driver has no read-back for most of those fields (baud excepted)**, so there's nothing yet to reconcile an edit against…"

Audited against the driver's actual getter surface (`ublox.py` method defs, verified by grep — the complete read-side list):

- `get_rtcm_port_config` (`786`), `get_port_protocols` (`864`), `get_gnss_config` (`942`), `get_dyn_model` (`1101`), `get_uart_baud_rates` (`1166`), `get_base_config` (`1625`), plus generic `_read_cfg_keys_locked` (`723`) and internal `_read_ecef_locked` (`1560`).

Against that list, the docstring's "no read-back for most" is **only true for** `meas_period_ms`, `elevation_mask_deg`, `bds_b2_enabled`, `spi_enabled` (4 fields). For **`constellations`, `port protocols`, `dyn_model`, `tmode_mode`, and `baud`** a live read-back getter exists. So the docstring's stated rationale for keeping those sections read-only — and the `build_apply_config` docstring's rationale for scoping Apply to matrix+data-link ("the other fields have no live read-back to verify a write against", `gps_config.py:271-272`) — **rests on a claim that is false for 5 of the 7 fields**. The real remaining gaps are narrower: (a) no *standalone* getter for the 4 fields above, so no in-form display/sync; (b) the endpoint-level read-back only covers the matrix, so `status="ok"` says nothing about the rest.

---

## 4. Live cross-check (vaio-base, read-only)

Read via `GET` on the app's own API at `127.0.0.1:8080` (host `vaio-base.lan`, user `rtkbase`) — no config writes, no restarts, no port probing.

- `GET /api/device/status` → `state=connected`, `/dev/ttyUSB0 @ 57600`, `ZED-F9P`, `HPG 1.51`, `PROTVER 27.50`, `HW 00190000`.
- `GET /api/device/port-protocols` → UART1 in `[UBX,NMEA,RTCM3X]` / out `[RTCM3X]`; UART2 in `[UBX,RTCM3X]` / out `[RTCM3X]`; USB in `[UBX,NMEA,RTCM3X]` / out `[UBX,NMEA,RTCM3X]`. *(Matches the built-in profile `ublox-f9p-base-standard.yaml` on UART1/UART2; USB is left at factory.)*
- `GET /api/device/gnss` → **all 6 constellations `enabled=true`**: gps, sbas, galileo, **beidou**, qzss, glonass. No IMES/NavIC block. *(Contrast: the #82 snapshot, taken the same day, reported **no** BeiDou block and an IMES/NavIC block — the unit's CFG-GNSS enumeration has since changed, so the "cannot add BeiDou" failure is now dormant on this unit.)*
- `GET /api/device/base-config` → `mode=fixed`, `pos_type=llh`, `32.7328943 / -117.2362775`, `29.49 m`, `596 mm`.
- `GET /api/device/rtcm-ports` → rows 1005/4072.0/4072.1/1074/1084/1094/1124/1230 = `1` on UART1+UART2, `0` on USB/I2C/SPI; rows 1077/1087/1097/1127 = `0` everywhere. *(First call returned `"No CFG-VALGET response for RTCM port config"` — a transient read-timeout on a busy base link; two retries succeeded identically.)*
- **No API endpoint exists** to read `meas_period_ms` / `elevation_mask_deg` / `bds_b2_enabled` / `spi_enabled` (and `dyn_model` has no `/dyn-model` endpoint — it's read internally by `check_base_invariants`). Full GET surface (`api/device.py`): `/status`, `/capabilities`, `/base-invariants`, `/rtcm-ports`, `/port-protocols`, `/gnss`, `/base-config`, `/position`, `/survey-in`, `/base-positions`.

---

## 5. Minimum driver additions to make apply+verify work per field

The write side is complete for every field; the verify side needs the following, from cheapest to most:

1. **Add 4 standalone getters** (mirror of the existing `get_dyn_model`, each a one-line `CFG-VALGET`):
   - `get_measurement_rate() -> int` → poll `CFG_RATE_MEAS` (`+CFG_RATE_NAV` to confirm the =1 pin).
   - `get_elevation_mask_deg() -> int` → poll `CFG_NAVSPG_INFIL_MINELEV`.
   - `get_bds_b2_enabled() -> bool` → poll `CFG_SIGNAL_BDS_B2_ENA`.
   - `get_spi_enabled() -> bool` → poll `CFG_SPI_ENABLED`.
   Each reuses `_read_cfg_keys_locked` (`ublox.py:723`). This alone makes the 4 currently-unreadable fields displayable + reconcilable in-form and closes the docstring's inaccuracy.
2. **Extend the endpoint-level read-back** (`device_service.py:868-880`) from "matrix only" to every applied field — read back rate, constellations, protocols, dyn_model, tmode, the 3 optimisations, and fold mismatches into `ApplyConfigResult.diff` so `status="ok"` is an honest claim. (The per-write `_write_and_verify_locked` already raises on a bad write; this turns firmware refusals into *reported diffs* instead of *exceptions* and covers the "written but unverified" state the map wants surfaced.)
3. **Make the constellation write assertive + non-deprecated** (fixes defect (c) in the table): stop using `CFG-GNSS SET` as the *write* boundary; write the wanted set through `CFG-VALSET` on the per-constellation enable keys (`CFG-SIGNAL-{GPS,SBAS,BDS,QZSS,GLO}_ENA`, per the HPG 1.51 IFD crosswalk), keep `CFG-GNSS POLL` only as the *capability probe*, and reconcile the profile's list against device capability explicitly (report "BeiDou not supported by this unit's firmware" on NAK / absent key rather than silently no-op). This removes the add-block / drop-block asymmetry.
4. **Push `form_extras` through `_apply()`** (`gps_config.py:1376` → pass `form_extras` into `build_apply_config`): one-line change that makes the UI actually send the fields the service+driver already write and verify. This is the single change that converts "status=ok but 4 fields at factory default" into a real full-form apply. (Scope decision owned by #65/#66, to be re-opened per map #84 locked item 3.)

---

## 6. Sources

**Code (`sp-rtk-base`, `main @ f7680d9`):**
- `src/sp_rtk_base/services/drivers/ublox.py` — `_write_and_verify_locked` (750-780), `_read_cfg_keys_locked` (723-748), `get_rtcm_port_config` (786-835), `get_port_protocols` (864-922), `get_gnss_config`/`_parse_cfg_gnss` (942-997), `configure_gnss` (999-1035), `configure_port_protocols` (1041-1079), `configure_measurement_rate` (1081-1090), `configure_dyn_model`/`get_dyn_model` (1092-1105), `configure_tmode_mode` (1107-1119), `configure_optimisations` (1121-1146), `apply_rtcm_matrix` (1148-1164), `get_uart_baud_rates` (1166-1178), `configure_baud` (1180-1195), `reconnect_at_baud` (1197-1220), `get_base_config`/`_parse_cfg_tmode` (1625-1733); `_MATRIX_PORTS` (146), `_PROTOCOL_PORT_PREFIXES` (92-96), `_DYN_MODEL_VALUES` (123-132).
- `src/sp_rtk_base/services/device_service.py` — `apply_receiver_config` (727-880): guards (789-810), rate (814-816), ports (818-823), constellations (825-836), optimisations (838-843), role (845-848), matrix (850), baud (852-857), reopen (865-866), read-back verify (868-880); `check_base_invariants` (428-462, reads `get_dyn_model` at 441).
- `src/sp_rtk_base/ui/pages/gps_config.py` — module docstring (1-26; the "no read-back" claim at 18-26), `FormExtras` (238-256), `build_apply_config` (261-301, `extras or FormExtras()` at 282), `i2c_spi_advisory_rows` (186-192), `_current_form_config` (1149-1154, the only extras-passing call), `_apply` (1361-1435, extras-omitting call at 1376).
- `src/sp_rtk_base/models/device_models.py` — `BaseMode` (159), `DynModel` (167), `RtcmOutputPort` (236-243), `PortId` (311-316), `UbxProtocol` (319-324), `GnssConstellation` (389-397).
- `src/sp_rtk_base/models/profile_models.py` — `ReceiverConfig` (118-167), `BaudConfig` (78-84), `RtcmStreamConfig` (98-109).
- `src/sp_rtk_base/profiles/builtin/ublox-f9p-base-standard.yaml` — built-in base profile (asserts all fields incl. `elevation_mask_deg: 15`, `bds_b2_enabled: false`, `spi_enabled: true`, all 6 constellations).
- `src/sp_rtk_base/api/device.py` — GET surface (see §4).

**u-blox F9P protocol references:**
- u-blox F9 HPG 1.51 Interface Description, **UBXDOC-963802114-13124** R01 (08-Nov-2024): config-DB keys + defaults (`CFG-NAVSPG-INFIL_MINELEV`, `CFG-SIGNAL-BDS_B2_ENA`, `CFG-SPI-ENABLED`, `CFG-UART1/2-BAUDRATE`, `CFG-*{IN,OUT}PROT_*`), CFG-GNSS spec + deprecation note (>23.01) + crosswalk to `CFG-SIGNAL-*_ENA`, satellite numbering (gnssId incl. NavIC 7), `CFG-NAVSPG-DYNMODEL` value table.
- u-blox ZED-F9P FW 1.00 HPG 1.51 Release Note, **UBXDOC-963802114-13110**: supported constellations (GPS/GLONASS/Galileo/BeiDou B1I+B2I/QZSS + SBAS), §3.1 "relying exclusively on the configuration interface using UBX-CFG-VALSET/VALGET/VALDEL", empty new/modified item tables vs HPG 1.50.
- u-blox ZED-F9P Integration Manual **UBX-18010802** (CFG-VALSET/VALGET/VALDEL; UART baud key).
- Corroboration: gpsd `ubxtool` examples (`CFG-NAVSPG-DYNMODEL` default 4 automotive, 2 stationary — matches the driver's `_DYN_MODEL_VALUES`), u-blox Support forum (dynamic-model semantics, UART1 INPROT/OUTPROT key naming).

**Live:** vaio-base app API (ZED-F9P HPG 1.51), read-only GETs 2026-08-31 — see §4.
