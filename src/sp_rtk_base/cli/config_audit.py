"""ZED-F9P Configuration Audit Tool.

Reads the current (RAM) and factory-default configuration from a u-blox
Gen9 receiver via CFG-VALGET, then reports every setting that differs
from the factory default.

Usage (installed):
    sp-rtk-base-gps-audit [--port /dev/ttyUSB0] [--baud 57600]

Usage (development):
    uv run sp-rtk-base-gps-audit [--port /dev/ttyUSB0] [--baud 57600]

Non-destructive: only reads configuration, never writes.

Key insight: The UBXReader maintains internal buffer state. When doing
many rapid polls, reset_input_buffer() clears the OS buffer but leaves
the reader's internal buffer with partial data, causing hangs. Solution:
create a FRESH UBXReader for each poll operation.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
import time
from dataclasses import dataclass, field

import serial  # type: ignore[import-untyped]
from pyubx2 import (  # type: ignore[import-untyped]
    UBX_CONFIG_DATABASE,
    UBXMessage,
    UBXReader,
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Constants
LAYER_RAM = 0
LAYER_DEFAULT = 7
SERIAL_TIMEOUT = 0.5   # Short timeout — we poll frequently
MAX_READ_ATTEMPTS = 25  # Per poll — successes happen at read #0; keep low for speed
MAX_KEYS_PER_POLL = 12  # Small batches for reliability


# ---------------------------------------------------------------------------
# Configuration group definitions
# ---------------------------------------------------------------------------

CONFIG_GROUPS: list[tuple[str, str, list[str]]] = [
    ("UART1", "UART1 port settings", ["CFG_UART1_"]),
    ("UART1 Protocols", "UART1 input/output protocols",
     ["CFG_UART1INPROT_", "CFG_UART1OUTPROT_"]),
    ("UART2", "UART2 port settings", ["CFG_UART2_"]),
    ("UART2 Protocols", "UART2 input/output protocols",
     ["CFG_UART2INPROT_", "CFG_UART2OUTPROT_"]),
    ("USB", "USB interface settings", ["CFG_USB_"]),
    ("USB Protocols", "USB input/output protocols",
     ["CFG_USBINPROT_", "CFG_USBOUTPROT_"]),
    ("I2C", "I2C interface settings", ["CFG_I2C_"]),
    ("I2C Protocols", "I2C input/output protocols",
     ["CFG_I2CINPROT_", "CFG_I2COUTPROT_"]),
    ("SPI", "SPI interface settings", ["CFG_SPI_"]),
    ("SPI Protocols", "SPI input/output protocols",
     ["CFG_SPIINPROT_", "CFG_SPIOUTPROT_"]),
    ("RATE", "Navigation/measurement rate", ["CFG_RATE_"]),
    ("NAVSPG", "Navigation engine settings (dynamic model, fix mode, etc.)",
     ["CFG_NAVSPG_"]),
    ("NAVHPG", "High-precision navigation", ["CFG_NAVHPG_"]),
    ("NAV2", "Secondary navigation output", ["CFG_NAV2_"]),
    ("NAVMASK", "Navigation masks", ["CFG_NAVMASK_"]),
    ("TMODE", "Time/Base station mode (survey-in, fixed, disabled)",
     ["CFG_TMODE_"]),
    ("SIGNAL", "GNSS constellation & signal selection", ["CFG_SIGNAL_"]),
    ("SBAS", "SBAS augmentation settings", ["CFG_SBAS_"]),
    ("BDS", "BeiDou-specific settings", ["CFG_BDS_"]),
    ("QZSS", "QZSS-specific settings", ["CFG_QZSS_"]),
    ("NMEA", "NMEA output formatting", ["CFG_NMEA_"]),
    ("RTCM", "RTCM protocol settings", ["CFG_RTCM_"]),
    ("HW", "Hardware config (antenna, etc.)", ["CFG_HW_"]),
    ("TP", "Time pulse configuration", ["CFG_TP_"]),
    ("PM", "Power management", ["CFG_PM_"]),
    ("ITFM", "Interference monitor / jamming", ["CFG_ITFM_"]),
    ("ODO", "Odometer", ["CFG_ODO_"]),
    ("INFMSG", "Information message enables", ["CFG_INFMSG_"]),
    ("TXREADY", "TX ready signaling", ["CFG_TXREADY_"]),
    ("SEC", "Security", ["CFG_SEC_"]),
    ("MOT", "Motion detection", ["CFG_MOT_"]),
    ("MSGOUT: RTCM", "RTCM message output rates per port",
     ["CFG_MSGOUT_RTCM_"]),
    ("MSGOUT: NAV-PVT", "NAV-PVT output per port",
     ["CFG_MSGOUT_UBX_NAV_PVT_"]),
    ("MSGOUT: NAV-SVIN", "NAV-SVIN output per port",
     ["CFG_MSGOUT_UBX_NAV_SVIN_"]),
    ("MSGOUT: NAV-SAT", "NAV-SAT output per port",
     ["CFG_MSGOUT_UBX_NAV_SAT_"]),
    ("MSGOUT: NAV-STATUS", "NAV-STATUS output per port",
     ["CFG_MSGOUT_UBX_NAV_STATUS_"]),
    ("MSGOUT: NAV-HPPOSLLH", "NAV-HPPOSLLH output per port",
     ["CFG_MSGOUT_UBX_NAV_HPPOSLLH_"]),
    ("MSGOUT: MON-RF", "MON-RF output per port",
     ["CFG_MSGOUT_UBX_MON_RF_"]),
    ("MSGOUT: NMEA GGA", "NMEA GGA output per port",
     ["CFG_MSGOUT_NMEA_ID_GGA_"]),
    ("MSGOUT: NMEA RMC", "NMEA RMC output per port",
     ["CFG_MSGOUT_NMEA_ID_RMC_"]),
    ("MSGOUT: NMEA GSV", "NMEA GSV output per port",
     ["CFG_MSGOUT_NMEA_ID_GSV_"]),
    ("MSGOUT: NMEA GSA", "NMEA GSA output per port",
     ["CFG_MSGOUT_NMEA_ID_GSA_"]),
    ("MSGOUT: NMEA GLL", "NMEA GLL output per port",
     ["CFG_MSGOUT_NMEA_ID_GLL_"]),
    ("MSGOUT: NMEA VTG", "NMEA VTG output per port",
     ["CFG_MSGOUT_NMEA_ID_VTG_"]),
]


def get_keys_for_group(prefixes: list[str]) -> list[str]:
    """Return all config database keys matching any of the given prefixes."""
    keys: list[str] = []
    for key_name in sorted(UBX_CONFIG_DATABASE.keys()):
        for prefix in prefixes:
            if key_name.startswith(prefix):
                keys.append(key_name)
                break
    return keys


@dataclass
class ConfigValue:
    """A single configuration item with RAM and default values."""
    key_name: str
    ram_value: int | float | str | None = None
    default_value: int | float | str | None = None
    ram_read_ok: bool = False
    default_read_ok: bool = False

    @property
    def is_different(self) -> bool:
        if self.ram_value is None or self.default_value is None:
            return False
        return self.ram_value != self.default_value


@dataclass
class GroupResult:
    """Results for one configuration group."""
    name: str
    description: str
    items: list[ConfigValue] = field(default_factory=lambda: list[ConfigValue]())

    @property
    def differences(self) -> list[ConfigValue]:
        return [i for i in self.items if i.is_different]

    @property
    def successfully_read(self) -> int:
        return sum(1 for i in self.items if i.ram_read_ok and i.default_read_ok)

    @property
    def read_failures(self) -> int:
        return sum(
            1 for i in self.items if not i.ram_read_ok or not i.default_read_ok
        )


class ConfigReader:
    """Reads u-blox Gen9 configuration via CFG-VALGET.

    Creates a FRESH UBXReader for each poll to avoid internal buffer
    state corruption when reset_input_buffer() truncates partial
    messages mid-parse.
    """

    def __init__(self, port: str, baud: int, verbose: bool = False) -> None:
        self.port = port
        self.baud = baud
        self.verbose = verbose
        self._ser: serial.Serial | None = None  # type: ignore[no-any-unimported]

    def _make_reader(self) -> UBXReader:  # type: ignore[type-arg]
        """Create a fresh UBXReader — avoids stale internal buffer state."""
        assert self._ser is not None
        return UBXReader(
            self._ser,
            protfilter=7,   # NMEA + UBX + RTCM3
            quitonerror=0,  # ERR_IGNORE
        )

    def connect(self) -> str:
        """Connect with exclusive lock and return device identity string."""
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            timeout=SERIAL_TIMEOUT,
            exclusive=True,  # TIOCEXCL — kernel prevents other opens
        )
        # Advisory lock — gives clear error if another process has the port
        try:
            fcntl.flock(self._ser.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            self._ser.close()
            self._ser = None
            raise RuntimeError(
                f"Serial port {self.port} is locked by another process "
                "(is the web app running?)"
            )
        # Brief pause to let serial settle
        time.sleep(0.3)

        # Poll MON-VER
        poll_msg = UBXMessage("MON", "MON-VER", 0)
        self._ser.reset_input_buffer()
        self._ser.write(poll_msg.serialize())

        reader = self._make_reader()
        for _ in range(MAX_READ_ATTEMPTS):
            try:
                raw, parsed = reader.read()  # type: ignore[misc]
                if (parsed is not None
                        and getattr(parsed, "identity", "") == "MON-VER"):
                    return self._parse_mon_ver(parsed)
            except Exception:
                continue

        return "Unknown device (MON-VER timeout)"

    @staticmethod
    def _parse_mon_ver(parsed: object) -> str:
        """Parse MON-VER response into a human-readable string."""
        sw = getattr(parsed, "swVersion", b"")
        hw = getattr(parsed, "hwVersion", b"")
        if isinstance(sw, bytes):
            sw = sw.decode("ascii", errors="replace").strip("\x00 ")
        if isinstance(hw, bytes):
            hw = hw.decode("ascii", errors="replace").strip("\x00 ")

        fwver = ""
        protver = ""
        model = ""
        for i in range(30):
            ext = getattr(parsed, f"extension_{i:02d}", None)
            if ext is None:
                continue
            if isinstance(ext, bytes):
                ext_str = ext.replace(b"\x00", b"").decode(
                    "ascii", errors="replace"
                ).strip()
            else:
                ext_str = str(ext).strip("\x00 ")
            if "FWVER=" in ext_str:
                fwver = ext_str.split("=", 1)[1].strip()
            elif "PROTVER=" in ext_str:
                protver = ext_str.split("=", 1)[1].strip()
            elif "MOD=" in ext_str:
                model = ext_str.split("=", 1)[1].strip()
            elif any(m in ext_str for m in ("ZED-", "NEO-", "MAX-")):
                model = ext_str.strip()

        return f"{model} (FW: {fwver or sw}, Proto: {protver}, HW: {hw})"

    def disconnect(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    def _poll_valget(
        self, keys: list[str], layer: int
    ) -> dict[str, int | float | str]:
        """Poll CFG-VALGET for a list of keys on one layer.

        Key fix: creates a FRESH UBXReader each time to avoid
        stale internal buffer state from previous polls.
        """
        assert self._ser is not None

        keys_any: list[str | int] = list(keys)
        msg = UBXMessage.config_poll(layer, 0, keys_any)

        # Flush OS serial buffer, write immediately, create fresh reader
        self._ser.reset_input_buffer()
        self._ser.write(msg.serialize())  # type: ignore[union-attr]
        reader = self._make_reader()

        results: dict[str, int | float | str] = {}

        for i in range(MAX_READ_ATTEMPTS):
            try:
                raw, parsed = reader.read()  # type: ignore[misc]
                if parsed is None:
                    continue
                identity = getattr(parsed, "identity", "")
                if identity == "CFG-VALGET":
                    for key_name in keys:
                        val = getattr(parsed, key_name, None)
                        if val is not None:
                            results[key_name] = val
                    if self.verbose:
                        print(f"\n    [L{layer}] got {len(results)}/{len(keys)}"
                              f" keys at read #{i}")
                    return results
                elif identity == "ACK-NAK":
                    if self.verbose:
                        print(f"\n    [L{layer}] NAK at read #{i}"
                              " (keys unsupported)")
                    return results
            except Exception:
                continue

        if self.verbose:
            print(f"\n    [L{layer}] timeout ({MAX_READ_ATTEMPTS} reads)")
        return results

    def poll_keys(
        self, keys: list[str], layer: int
    ) -> dict[str, int | float | str]:
        """Poll keys in batches, returning combined results."""
        if not self._ser:
            raise RuntimeError("Not connected")

        all_results: dict[str, int | float | str] = {}
        for chunk_start in range(0, len(keys), MAX_KEYS_PER_POLL):
            chunk = keys[chunk_start:chunk_start + MAX_KEYS_PER_POLL]
            chunk_results = self._poll_valget(chunk, layer)
            all_results.update(chunk_results)
        return all_results

    def read_group(
        self, name: str, description: str, prefixes: list[str]
    ) -> GroupResult:
        """Read a configuration group from both RAM and DEFAULT layers."""
        keys = get_keys_for_group(prefixes)
        if not keys:
            return GroupResult(name=name, description=description)

        ram_values = self.poll_keys(keys, LAYER_RAM)
        default_values = self.poll_keys(keys, LAYER_DEFAULT)

        items: list[ConfigValue] = []
        for key in keys:
            item = ConfigValue(key_name=key)
            if key in ram_values:
                item.ram_value = ram_values[key]
                item.ram_read_ok = True
            if key in default_values:
                item.default_value = default_values[key]
                item.default_read_ok = True
            items.append(item)

        return GroupResult(name=name, description=description, items=items)


# ---------------------------------------------------------------------------
# Value formatters & annotations
# ---------------------------------------------------------------------------

ENUM_MAPS: dict[str, dict[int, str]] = {
    "CFG_TMODE_MODE": {0: "Disabled", 1: "Survey-In", 2: "Fixed"},
    "CFG_TMODE_POS_TYPE": {0: "ECEF", 1: "LLH"},
    "CFG_NAVSPG_DYNMODEL": {
        0: "Portable", 2: "Stationary", 3: "Pedestrian",
        4: "Automotive", 5: "Sea", 6: "Airborne 1g",
        7: "Airborne 2g", 8: "Airborne 4g", 9: "Wrist",
        10: "Bike", 11: "Lawn mower", 12: "E-scooter",
    },
    "CFG_NAVSPG_FIXMODE": {1: "2D Only", 2: "3D Only", 3: "Auto 2D/3D"},
    "CFG_NAVSPG_UTCSTANDARD": {
        0: "Auto", 3: "USNO (GPS)", 5: "SU (GLONASS)", 6: "NTSC (BDS)",
    },
    "CFG_RATE_NAV_PRIO": {0: "Speed", 1: "Accuracy"},
}

ANNOTATIONS: dict[str, str] = {
    "CFG_UART1_BAUDRATE":
        "Default 38400. Changed for faster throughput on base station.",
    "CFG_UART2_BAUDRATE":
        "Default 38400. Often raised for RTCM3 correction data I/O.",
    "CFG_TMODE_MODE":
        "Default 0 (Disabled). Changed for base station operation.",
    "CFG_TMODE_POS_TYPE":
        "Position storage. ECEF=0 (from survey-in), LLH=1 (manual).",
    "CFG_TMODE_SVIN_MIN_DUR":
        "Min survey-in duration (s). Longer = more reliable average.",
    "CFG_TMODE_SVIN_ACC_LIMIT":
        "Survey-in accuracy (0.1mm). Lower = more precise but slower.",
    "CFG_NAVSPG_DYNMODEL":
        "Default Portable(0). Stationary(2) typical for base stations.",
    "CFG_RATE_MEAS":
        "Measurement period (ms). Default 1000 = 1Hz.",
    "CFG_RATE_NAV":
        "Nav solutions per measurement. Usually 1.",
    "CFG_UART1OUTPROT_NMEA":
        "NMEA output on UART1. Often disabled to reduce port traffic.",
    "CFG_UART1OUTPROT_UBX":
        "UBX output on UART1. Needed for pyubx2 communication.",
    "CFG_UART1OUTPROT_RTCM3X":
        "RTCM3 output on UART1. Enabled for base station RTCM output.",
    "CFG_UART2OUTPROT_RTCM3X":
        "RTCM3 output on UART2. Often used as dedicated RTCM output.",
}


def format_value(key: str, value: int | float | str | None) -> str:
    """Format a config value with human-readable annotation."""
    if value is None:
        return "N/A"
    if key in ENUM_MAPS and isinstance(value, int):
        enum_str = ENUM_MAPS[key].get(value, f"Unknown({value})")
        return f"{value} ({enum_str})"
    if "BAUDRATE" in key and isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, int) and any(
        p in key for p in ("INPROT_", "OUTPROT_")
    ):
        return f"{value} ({'enabled' if value else 'disabled'})"
    if isinstance(value, int) and key.endswith("_ENA"):
        return f"{value} ({'ON' if value else 'OFF'})"
    return str(value)


def get_annotation(key: str) -> str:
    """Return annotation for why a key might differ."""
    if key in ANNOTATIONS:
        return ANNOTATIONS[key]
    if "RTCM_3X_TYPE" in key:
        port = key.rsplit("_", 1)[-1] if "_" in key else ""
        return f"RTCM msg output rate. Non-zero = enabled on {port} port."
    if "MSGOUT" in key:
        port = key.rsplit("_", 1)[-1] if "_" in key else ""
        return f"Message output rate on {port} port."
    return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ZED-F9P Config Audit — compare RAM vs factory defaults"
    )
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=57600)
    parser.add_argument("--show-same", action="store_true",
                        help="Also show values that match defaults")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show per-poll debug info")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    args = parser.parse_args()

    print("=" * 70)
    print("  ZED-F9P Configuration Audit Tool")
    print("  Comparing current (RAM) config vs factory defaults")
    print("=" * 70)
    print()

    reader = ConfigReader(args.port, args.baud, verbose=args.verbose)

    print(f"Connecting to {args.port} @ {args.baud}...")
    try:
        identity = reader.connect()
    except Exception as exc:
        print(f"✗ Connection failed: {exc}")
        sys.exit(1)
    print(f"✓ Connected: {identity}")
    print()

    all_results: list[GroupResult] = []
    total_keys = 0
    total_diffs = 0
    total_read_ok = 0

    for i, (name, desc, prefixes) in enumerate(CONFIG_GROUPS):
        key_count = len(get_keys_for_group(prefixes))
        if key_count == 0:
            continue
        progress = f"[{i+1}/{len(CONFIG_GROUPS)}]"
        sys.stdout.write(
            f"\r  Reading {progress} {name} ({key_count} keys)...          "
        )
        sys.stdout.flush()

        try:
            result = reader.read_group(name, desc, prefixes)
            all_results.append(result)
            total_keys += len(result.items)
            total_diffs += len(result.differences)
            total_read_ok += result.successfully_read
        except Exception as exc:
            print(f"\n  ⚠ Failed to read {name}: {exc}")

    reader.disconnect()
    total_failures = total_keys - total_read_ok
    print(f"\r{'':70}")
    print(
        f"✓ Scanned {total_keys} config keys across"
        f" {len(all_results)} groups"
    )
    print(f"  Successfully compared: {total_read_ok}")
    if total_failures > 0:
        print(
            f"  Unsupported/timeout:   {total_failures}"
            " (normal for older FW)"
        )
    print()

    if args.json:
        import json
        output: dict[str, list[dict[str, object]]] = {}
        for group in all_results:
            diffs: list[dict[str, object]] = []
            for item in group.items:
                if item.is_different or (
                    args.show_same and item.ram_read_ok
                ):
                    diffs.append({
                        "key": item.key_name,
                        "current": item.ram_value,
                        "default": item.default_value,
                        "changed": item.is_different,
                    })
            if diffs:
                output[group.name] = diffs
        print(json.dumps(output, indent=2, default=str))
        return

    # Print results
    print("=" * 70)
    if total_diffs > 0:
        print(
            f"  DIFFERENCES FROM FACTORY DEFAULTS: {total_diffs} change(s)"
        )
    else:
        print("  NO DIFFERENCES DETECTED (in readable keys)")
    print("=" * 70)
    print()

    for group in all_results:
        has_diffs = len(group.differences) > 0
        readable = group.successfully_read
        if not has_diffs and not args.show_same:
            continue
        if readable == 0 and not has_diffs:
            continue

        marker = "⚡" if has_diffs else "✓"
        diff_info = (
            f" — {len(group.differences)} change(s)"
            if has_diffs else " — all defaults"
        )
        read_info = f" [{readable}/{len(group.items)} readable]"
        print(
            f"{marker} {group.name}: {group.description}"
            f"{diff_info}{read_info}"
        )
        print("-" * 70)

        for item in group.items:
            if not item.ram_read_ok and not item.default_read_ok:
                continue
            if not item.is_different and not args.show_same:
                continue

            if item.is_different:
                current_str = format_value(item.key_name, item.ram_value)
                default_str = format_value(
                    item.key_name, item.default_value
                )
                print(f"  ▸ {item.key_name}")
                print(f"      Current: {current_str}")
                print(f"      Default: {default_str}")
                annotation = get_annotation(item.key_name)
                if annotation:
                    print(f"      Why:     {annotation}")
                print()
            elif args.show_same and item.ram_read_ok:
                current_str = format_value(item.key_name, item.ram_value)
                print(f"    {item.key_name}: {current_str}")

        print()

    # Summary
    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  Total keys attempted:   {total_keys}")
    print(f"  Successfully compared:  {total_read_ok}")
    print(f"  Changed from default:   {total_diffs}")
    print(f"  At factory default:     {total_read_ok - total_diffs}")
    print(f"  Unsupported/unreadable: {total_failures}")
    print()

    changed_groups = [g for g in all_results if g.differences]
    if changed_groups:
        print("  Groups with changes:")
        for g in changed_groups:
            print(f"    • {g.name}: {len(g.differences)} change(s)")
    else:
        print("  ✓ All readable settings match factory defaults!")

    print()


if __name__ == "__main__":
    main()
