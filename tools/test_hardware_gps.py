#!/usr/bin/env python3
"""Manual hardware test for u-blox GPS receiver.

Usage:
    uv run python tools/test_hardware_gps.py [--port /dev/ttyUSB0] [--baud 57600]

Connects to a u-blox receiver, reads MON-VER, and displays device info.
Also lists available serial ports for discovery.
"""

from __future__ import annotations

import argparse


def list_ports() -> None:
    """List all available serial ports with GPS detection."""
    from sp_base.services.drivers.base import GpsReceiverDriver

    ports = GpsReceiverDriver.list_serial_ports()
    if not ports:
        print("No serial ports found.")
        return

    print(f"\n{'Port':<20} {'Description':<35} {'Manufacturer':<15} {'VID:PID':<12} {'GPS?'}")
    print("-" * 90)
    for p in ports:
        vid_pid = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else "----:----"
        gps_flag = "✓ GPS" if p.is_gps else ""
        print(f"{p.port:<20} {p.description[:34]:<35} {p.manufacturer[:14]:<15} {vid_pid:<12} {gps_flag}")


def test_connect(port: str, baud: int) -> None:
    """Connect to a u-blox receiver and display device info."""
    from sp_base.services.drivers.ublox import UbloxDriver

    driver = UbloxDriver()

    print(f"\nConnecting to {port} @ {baud}...")
    try:
        info = driver.connect(port, baud)
    except (ConnectionError, TimeoutError) as exc:
        print(f"✗ Connection failed: {exc}")
        return

    print(f"✓ Connected!\n")
    print(f"  Vendor:    {info.vendor}")
    print(f"  Model:     {info.model}")
    print(f"  Firmware:  {info.firmware_version}")
    print(f"  Protocol:  {info.protocol_version}")
    print(f"  Hardware:  {info.hardware_version}")

    caps = driver.get_capabilities()
    print(f"\n  Capabilities ({len(caps)}):")
    for cap in sorted(caps, key=lambda c: c.value):
        print(f"    - {cap.value}")

    driver.disconnect()
    print(f"\n✓ Disconnected.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test u-blox GPS hardware connection")
    parser.add_argument("--port", default="/dev/ttyUSB0", help="Serial port (default: /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=57600, help="Baud rate (default: 57600)")
    parser.add_argument("--list-only", action="store_true", help="Only list serial ports")
    args = parser.parse_args()

    print("=" * 50)
    print("SP-Base — u-blox GPS Hardware Test")
    print("=" * 50)

    list_ports()

    if not args.list_only:
        test_connect(args.port, args.baud)


if __name__ == "__main__":
    main()
