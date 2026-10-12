# X20P identity capture (read-only), hermes-01

Task [#255](https://github.com/rodenj1/sp-rtk-base/issues/255), under [map #247](https://github.com/rodenj1/sp-rtk-base/issues/247). Captured 2026-10-12 ~19:44 UTC on hermes-01 from the ArduSimple simpleRTK4 Optimum connected to native USB. **Hardware-observed** evidence from one board, one firmware build and one transport (USB).

## Identity (MON-VER)

| Field | Value |
|---|---|
| swVersion | `EXT HPG 2.02 (43e74c)` |
| hwVersion | `000B0000` |
| ROM | `ROM BASE 0x00A9D329` (ROM 1.11 per the release notes) |
| FWVER / PROTVER | `HPG 2.02` / `50.10` |
| MOD | `ZED-X20P` (resolves at confirmed tier 1) |
| GNSS extensions | `GPS;GAL;BDS`, `SBAS;QZSS`, `NAVIC`. **No GLONASS.** |
| USB | 1546:01ab, bcdDevice 2.02 (matches FWVER), `/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00` |

This matches the 2.02 row of the protocol research's firmware matrix (R02: `EXT HPG 2.02 (43e74c3)`, 50.10). R11 lists only ROM 1.11 for the 2.11 image, so this unit's ROM is in the set R11 lists. That is not an upgrade recommendation; upgrades are out of scope.

## Configuration keys (CFG-VALGET, RAM, one key per request)

139 keys were polled: the 112 app keys plus 27 supplemental keys. **121 were ACKed and 18 NAKed, with 0 timeouts.** Every NAK matches what the key/parser contract predicted for 2.02:

| NAKed | Count | Catalog prediction for 2.02 |
|---|---|---|
| `CFG_MSGOUT_RTCM_3X_TYPE4072_{0,1}_{I2C,SPI,UART1,UART2,USB}` | 10 | absent from 2.02 (documented from 2.10) |
| `CFG_MSGOUT_UBX_RXM_RTCM_{UART1,UART2,USB}` | 3 | absent from all X20 catalogs |
| `CFG_NAVCOR_ENABLE_GAL_HAS`, `CFG_NAVCOR_ENABLE_HOST`, `CFG_RTCM_DF028_OUT`, `CFG_RTCM_MSM_OUT_RANGERATE_SIGN`, `CFG_SIGNAL_QZSS_L1CB_ENA` | 5 | 2.10/2.11 additions |

No key that the catalog lists as absent for 2.02 was ACKed. The RXM-COR output keys (`CFG_MSGOUT_UBX_RXM_COR_*`, 0x209106B6–BA) are all **ACKed** (value 0, off).

This confirms on hardware that **the app's current `begin_correction_input` batch would be NAKed** on this unit, since it includes `CFG_MSGOUT_UBX_RXM_RTCM_*`. So would its 60-cell RTCM matrix read, since that includes 4072. Both need capability-gated key sets.

RAM state as found (not changed):
- `CFG_TMODE_MODE=1` (survey-in), with SVIN_MIN_DUR 3600 s and ACC_LIMIT 50.
- 1005 is enabled on UART1, and no MSM output is enabled.
- UART1 runs at 115200 and UART2 at 38400. UBX, NMEA and RTCM3 input and output are on for USB.
- Signals: GPS, GAL, BDS and SBAS are on; GLO, QZSS and the NavIC constellation are off. Bands on: GPS L5, GAL E5a/E6, BDS B1C/B2a/B3 and NavIC L5. `CFG_SIGNAL_PLAN=2`. B2 (B2I) is off.
- Dynamics 0 (portable), MINELEV 10, measurement rate 1000 ms.

The full table is in `out/capture.json`.

## Default output and status

- **Passive, 60 s, nothing transmitted:** 600 NMEA sentences at 1 Hz on USB: GGA, GLL, RMC, VTG, three GSA, and GSV for GP/GA/GB. **No UBX or RTCM** was emitted unprompted.
- MON-COMMS lists two ports: 0x0100 (UART1, ~179 MB transmitted) and 0x0300 (USB, the console port for this capture).
- **No sky view:** NAV-PVT fixType 0 with numSV 0, NAV-SAT shows 0 SVs, and NAV-SIG shows 0 signals. The receiver clock was unset (2025-06-19, invalid). Signal-use, RXM-COR and correction behaviour are **not observable** until the antenna has a sky view.

## Transmit ledger

`out/tx-ledger.json` contains **144 frames: 5 zero-payload polls (MON-VER, MON-COMMS, NAV-PVT, NAV-SAT, NAV-SIG) and 139 CFG-VALGET** (version 0, RAM, position 0, one key each). The first transmission was at t=60.0 s, after the passive window.

`capture.py` enforces an allowlist gate before every write. An offline check confirmed the gate blocks VALSET, VALDEL, CFG-CFG, CFG-RST, VALGET on the Flash layer, multi-key VALGET, a poll with a payload, and RTCM (8 of 8 blocked).

**Exception to "only allowlisted bytes":** before the capture, ModemManager opened ttyACM0 the moment it appeared and probed it for an AT port (journal 19:40:20–32, "Failed to find primary AT port"). Its AT strings are not UBX and cannot change configuration. ModemManager now ignores this device (see host changes).

## Host changes on hermes-01 (approved by John)

1. Installed `linux-modules-extra-6.8.0-146-generic`, then ran `modprobe cdc_acm`. The device appeared as `/dev/ttyACM0`.
2. Added `/etc/udev/rules.d/77-ublox-mm-ignore.rules`, which sets `ID_MM_DEVICE_IGNORE=1` for 1546:01ab, then rebound cdc_acm. Afterwards no process held the port.

## Reproduce

`sudo <venv>/python capture.py /dev/ttyACM0 out keys.json 60`, using a venv with pyserial and pyubx2 1.3.0. `keys.json` lists the polled keys and their IDs. `out/` contains `rx-raw.ubx` (every received frame), `rx-log.json`, `tx-ledger.json` and `capture.json`.
