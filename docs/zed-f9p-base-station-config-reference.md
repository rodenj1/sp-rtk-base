# ZED-F9P Base Station Configuration Reference

> **Source:** Pre-configured RTK base station receiver, audited 2026-04-15  
> **Device:** u-blox ZED-F9P (FW: HPG 1.12, Protocol: 27.11, HW: 00190000)  
> **Audit Tool:** `tools/read_gps_config.py`  
> **Connection:** `/dev/ttyUSB0` @ 57600 baud (FTDI USB-to-UART adapter → UART1)

This document captures the **32 configuration changes from factory defaults** found on
a pre-configured ZED-F9P RTK base station. It serves as a reference for what a
properly configured base station should look like and why each change is necessary.

---

## Audit Summary

| Metric | Count |
|--------|-------|
| Total config keys attempted | 520 |
| Successfully compared (RAM vs Default) | 368 |
| Changed from factory default | **32** |
| At factory default | 336 |
| Unsupported/unreadable (normal for older FW) | 152 |

---

## 1. UART Port Settings (4 changes)

These changes configure the communication ports for base station operation.

### Baud Rates

| Setting | Current | Default | Rationale |
|---------|---------|---------|-----------|
| `CFG_UART1_BAUDRATE` | **57,600** | 38,400 | UART1 is the primary command/control port (connected via FTDI adapter). Raised from default to handle UBX command traffic + RTCM output at base station data rates. |
| `CFG_UART2_BAUDRATE` | **115,200** | 38,400 | UART2 is often used as a dedicated RTCM output port (e.g., connected to a radio modem for rover corrections). 115,200 provides headroom for the full RTCM message set at 1Hz. |

### Output Protocols

| Setting | Current | Default | Rationale |
|---------|---------|---------|-----------|
| `CFG_UART1OUTPROT_NMEA` | **0 (disabled)** | 1 (enabled) | NMEA sentences disabled on UART1 to reduce port traffic. For a base station, NMEA output is unnecessary — only RTCM corrections matter. Saves bandwidth. |
| `CFG_UART1OUTPROT_UBX` | **0 (disabled)** | 1 (enabled) | Unsolicited UBX message output disabled on UART1. The receiver still **responds** to polled UBX messages (CFG-VALGET, MON-VER, etc.), but won't spontaneously emit UBX data like NAV-PVT. This keeps the port clean for RTCM-only output. |

> **⚠️ Note on `CFG_UART1OUTPROT_UBX = 0`:** This is a deliberate choice to
> minimize serial traffic. Polled UBX command/response (e.g., from pyubx2) still
> works — the receiver responds to polls regardless of this setting. However,
> periodic UBX messages (like position updates) won't appear on UART1 unless this
> is re-enabled. The sp-rtk-base web app polls explicitly, so this works fine.

---

## 2. Base Station Mode — TMODE (7 changes)

These are the **core settings** that make this receiver operate as an RTK base station.

| Setting | Current | Default | Rationale |
|---------|---------|---------|-----------|
| `CFG_TMODE_MODE` | **2 (Fixed)** | 0 (Disabled) | The receiver is in **Fixed Base Station mode**. It uses a known, fixed position to generate RTCM correction data. This is the primary setting that enables base station operation. |
| `CFG_TMODE_ECEF_X` | **-245,790,204** | 0 | Fixed antenna position X (ECEF, cm). Set from a completed survey-in or manual entry. |
| `CFG_TMODE_ECEF_Y` | **-477,512,066** | 0 | Fixed antenna position Y (ECEF, cm). |
| `CFG_TMODE_ECEF_Z` | **342,909,332** | 0 | Fixed antenna position Z (ECEF, cm). |
| `CFG_TMODE_FIXED_POS_ACC` | **47,308** | 0 | Position accuracy estimate (0.1mm units) ≈ **4.7 meters**. This was the accuracy achieved at the end of the initial survey-in. For higher-precision applications, a longer survey-in or known coordinates would reduce this. |
| `CFG_TMODE_SVIN_MIN_DUR` | **60** | 0 | Minimum survey-in duration = **60 seconds**. Used when initially determining the base position. Once the position is determined and saved as Fixed mode, this value is only relevant if survey-in mode is re-enabled. |
| `CFG_TMODE_SVIN_ACC_LIMIT` | **300,000** | 0 | Survey-in accuracy limit = **30 meters** (300,000 × 0.1mm). This is a relatively loose limit, allowing the survey-in to complete quickly. For high-precision applications, values of 10,000–50,000 (1–5m) are typical. |

### TMODE Mode Values Reference

| Value | Mode | Description |
|-------|------|-------------|
| 0 | Disabled | No base station mode (receiver mode / rover) |
| 1 | Survey-In | Automatically determines position by averaging over time |
| 2 | Fixed | Uses a known, manually-set or previously-surveyed position |

---

## 3. RTCM Message Outputs (16 changes)

All RTCM messages are enabled at **rate = 1** (every navigation epoch) on both
**UART1** and **UART2**. This is the standard recommended RTK base station message
set per u-blox, SparkFun, and ArduSimple documentation.

### Enabled RTCM Messages

| RTCM Type | Name | Purpose |
|-----------|------|---------|
| **1005** | Stationary RTK Reference Station ARP | Broadcasts the antenna reference point (ARP) position to rovers. Essential for all RTK operation. |
| **1074** | GPS MSM4 | GPS L1/L2 observations (pseudorange + carrier phase). Core GPS corrections. |
| **1084** | GLONASS MSM4 | GLONASS observations. Enables multi-constellation RTK with GLONASS satellites. |
| **1094** | Galileo MSM4 | Galileo observations. Enables multi-constellation RTK with Galileo satellites. |
| **1124** | BeiDou MSM4 | BeiDou observations. Enables multi-constellation RTK with BeiDou satellites. |
| **1230** | GLONASS Code-Phase Biases | Provides GLONASS inter-frequency code biases. Required for proper GLONASS RTK processing. |
| **4072.0** | u-blox Proprietary (Ref Station PVT) | Reference station position/velocity/time. Used for u-blox moving base mode. |
| **4072.1** | u-blox Proprietary (Additional Ref Info) | Additional reference station information for moving base. |

### Message Type Classification

- **1005**: Station position (required for all RTK)
- **10x4 series (MSM4)**: Multi-Signal Messages type 4 — compact observations with
  pseudorange and carrier phase. MSM4 is the standard choice balancing data volume
  vs. information content. MSM7 would provide more data but at higher bandwidth cost.
- **1230**: GLONASS bias corrections (required when using GLONASS)
- **4072.x**: u-blox proprietary extensions (useful for u-blox rover interoperability)

### Configuration Keys

| Key | UART1 | UART2 |
|-----|-------|-------|
| `CFG_MSGOUT_RTCM_3X_TYPE1005_*` | 1 | 1 |
| `CFG_MSGOUT_RTCM_3X_TYPE1074_*` | 1 | 1 |
| `CFG_MSGOUT_RTCM_3X_TYPE1084_*` | 1 | 1 |
| `CFG_MSGOUT_RTCM_3X_TYPE1094_*` | 1 | 1 |
| `CFG_MSGOUT_RTCM_3X_TYPE1124_*` | 1 | 1 |
| `CFG_MSGOUT_RTCM_3X_TYPE1230_*` | 1 | 1 |
| `CFG_MSGOUT_RTCM_3X_TYPE4072_0_*` | 1 | 1 |
| `CFG_MSGOUT_RTCM_3X_TYPE4072_1_*` | 1 | 1 |

> **Note:** RTCM messages are NOT enabled on USB, I2C, or SPI ports (all at
> default 0). Only UART1 and UART2 are used for RTCM output.

---

## 4. Navigation Engine Settings (2 changes)

| Setting | Current | Default | Rationale |
|---------|---------|---------|-----------|
| `CFG_NAVSPG_DYNMODEL` | **2 (Stationary)** | 0 (Portable) | Tells the navigation engine the receiver is stationary. This significantly improves position stability for a fixed base station by applying tighter position filtering. **Essential for base stations.** |
| `CFG_NAVSPG_INFIL_MINELEV` | **15°** | 10° | Minimum satellite elevation mask raised from 10° to 15°. Satellites below 15° elevation are ignored. This rejects low-elevation satellites that suffer from higher atmospheric errors and multipath interference — particularly beneficial for a permanent base station installation. |

### Dynamic Model Reference

| Value | Model | Use Case |
|-------|-------|----------|
| 0 | Portable | General purpose (default) |
| 2 | Stationary | **Base stations**, fixed installations |
| 3 | Pedestrian | Walking |
| 4 | Automotive | Vehicles |
| 5 | Sea | Marine |
| 6-8 | Airborne | Aviation (1g/2g/4g) |

---

## 5. GNSS Signal Configuration (1 change)

| Setting | Current | Default | Rationale |
|---------|---------|---------|-----------|
| `CFG_SIGNAL_BDS_B2_ENA` | **0 (OFF)** | 1 (ON) | BeiDou B2 signal disabled. This may be intentional to reduce processing load or because B2 coverage/quality is poor in the deployment region. BeiDou B1 remains enabled, so BeiDou corrections (RTCM 1124) still function on the B1 signal. |

---

## 6. Miscellaneous Changes (2 changes)

| Setting | Current | Default | Rationale |
|---------|---------|---------|-----------|
| `CFG_SPI_ENABLED` | **1 (ON)** | 0 (OFF) | SPI interface enabled. Default is off. May have been enabled for a specific hardware connection or carrier board requirement. |
| `CFG_TP_DUTY_LOCK_TP1` | **100,000** | 10.0 | Time pulse 1 duty cycle when GNSS is locked changed from 10% to effectively 100%. This turns TP1 into a "position locked" indicator — HIGH when the receiver has a GNSS fix. Can be used to drive an LED or logic signal. |

---

## Recommended Base Station Configuration Checklist

Based on this reference receiver, the following changes from factory defaults are
recommended when configuring a new ZED-F9P as an RTK base station:

### Essential (Required for base station operation)

- [ ] Set `CFG_TMODE_MODE` = 1 (Survey-In) or 2 (Fixed) depending on workflow
- [ ] If Survey-In: Set `CFG_TMODE_SVIN_MIN_DUR` (e.g., 60–300 seconds)
- [ ] If Survey-In: Set `CFG_TMODE_SVIN_ACC_LIMIT` (e.g., 50,000 = 5m)
- [ ] If Fixed: Set `CFG_TMODE_ECEF_X/Y/Z` to known coordinates
- [ ] Set `CFG_NAVSPG_DYNMODEL` = 2 (Stationary)
- [ ] Enable RTCM 1005, 1074, 1084, 1094, 1124, 1230 on output port(s)

### Recommended (Optimization)

- [ ] Raise `CFG_UART1_BAUDRATE` to 57600+ (for RTCM throughput)
- [ ] Raise `CFG_UART2_BAUDRATE` to 115200 (if used for radio modem)
- [ ] Disable `CFG_UART1OUTPROT_NMEA` = 0 (reduce traffic)
- [ ] Raise `CFG_NAVSPG_INFIL_MINELEV` to 15° (reduce multipath)
- [ ] Enable RTCM 4072.0 and 4072.1 (for u-blox rover compatibility)

### Optional

- [ ] Disable `CFG_UART1OUTPROT_UBX` = 0 (suppress unsolicited UBX output)
- [ ] Disable `CFG_SIGNAL_BDS_B2_ENA` = 0 (if B2 not needed in region)
- [ ] Set `CFG_TP_DUTY_LOCK_TP1` = 100000 (use TP1 as lock indicator)
- [ ] Enable `CFG_SPI_ENABLED` = 1 (if SPI hardware connected)

---

## How to Run the Audit Tool

```bash
# Ensure no other process has the serial port open (e.g., stop the web app first)
fuser /dev/ttyUSB0

# Run the audit (compare current config vs factory defaults)
uv run python tools/read_gps_config.py --port /dev/ttyUSB0 --baud 57600

# Verbose mode (show per-poll debug info)
uv run python tools/read_gps_config.py --port /dev/ttyUSB0 --baud 57600 -v

# Show matching values too (not just differences)
uv run python tools/read_gps_config.py --port /dev/ttyUSB0 --baud 57600 --show-same

# JSON output
uv run python tools/read_gps_config.py --port /dev/ttyUSB0 --baud 57600 --json
```

> **⚠️ Important:** The web app (sp-rtk-base) must be stopped before running the audit
> tool. If both processes have the serial port open simultaneously, they compete for
> serial bytes and reads become unreliable. This is the same class of issue that the
> `threading.Lock` in `UbloxDriver` solves within the web app — but across processes,
> only one can reliably own the serial port at a time.

---

## Technical Notes

### Serial Port Contention

The ZED-F9P continuously streams data (RTCM, NMEA) on its output ports. When
multiple processes open the same serial device (`/dev/ttyUSB0`), Linux allows both to
read — but each byte is consumed by whichever process reads first. This means:

- Process A sends a CFG-VALGET poll
- The receiver responds with a CFG-VALGET answer
- Process B's UBXReader consumes the response bytes before Process A can read them
- Process A times out waiting for a response that was already consumed

**Solution:** Ensure exclusive serial port access. Stop the web app before running
standalone tools, or use the web app's API endpoints instead.

### UBXReader Buffer State

When performing many rapid serial polls (as the audit tool does), creating a **fresh
`UBXReader` for each poll** is important. The `UBXReader` maintains internal parsing
state, and calling `serial.reset_input_buffer()` between polls can corrupt that state
by truncating partially-parsed messages. The audit tool creates a new reader per poll
to avoid this issue.

### Firmware Compatibility

This audit was performed on **HPG 1.12 (Protocol 27.11)**. Of 520 config keys in the
pyubx2 database, 368 were readable on this firmware version. The 152 unreadable keys
are typically newer configuration options added in later firmware versions (HPG 1.13,
1.30, 1.32, etc.) — this is normal and expected.
