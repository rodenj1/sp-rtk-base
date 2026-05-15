#!/usr/bin/env python3
# pyright: reportUnknownMemberType=false
"""Demo script: run sp-rtk-base with simulated RTCM source and mock destinations.

Starts:
  1. A TCP source simulator streaming synthetic RTCM3 data
  2. A mock NTRIP caster that accepts and logs incoming data
  3. The full sp-rtk-base NiceGUI application pre-configured with:
     - TCP input pointing to the simulator
     - A tcp_server destination (port 19876)
     - An NTRIP destination pointing to the mock caster
     - auto_start=True so the relay starts automatically

Usage:
    uv run python tools/demo_with_simulator.py

Then open http://localhost:8080 — the relay is already running!
Dashboard shows live metrics, events, and per-destination status.
"""

from __future__ import annotations

import atexit
import os
import sys
import tempfile
from pathlib import Path

import yaml

# Ensure the project src and tests directories are on the path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root))

from tests.fixtures.mock_ntrip_caster import MockNtripCaster  # noqa: E402
from tests.fixtures.tcp_destination_client import TCPDestinationClient  # noqa: E402
from tests.fixtures.tcp_source_simulator import TCPSourceSimulator  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SIMULATOR_PORT = 19800
SIMULATOR_DATA_RATE = 4000  # bytes per second — realistic RTCM rate
TCP_DEST_PORT = 19876
NTRIP_PASSWORD = "demo123"
NTRIP_MOUNTPOINT = "DEMO_MOUNT"


def _build_demo_config(
    source_port: int,
    tcp_dest_port: int,
    ntrip_port: int,
    ntrip_password: str,
    ntrip_mountpoint: str,
) -> dict[str, object]:
    """Build the demo YAML config as a dict."""
    return {
        "input": {
            "source": "tcp",
            "config": {
                "host": "127.0.0.1",
                "port": source_port,
            },
        },
        "destinations": [
            {
                "name": "demo-tcp-server",
                "type": "tcp_server",
                "enabled": True,
                "filter": {"mode": "pass_all", "message_ids": []},
                "config": {
                    "host": "0.0.0.0",
                    "port": tcp_dest_port,
                    "max_clients": 5,
                },
            },
            {
                "name": "demo-ntrip",
                "type": "ntrip",
                "enabled": True,
                "filter": {"mode": "pass_all", "message_ids": []},
                "config": {
                    "caster": "127.0.0.1",
                    "port": ntrip_port,
                    "mountpoint": ntrip_mountpoint,
                    "password": ntrip_password,
                    "username": "",
                    "version": "1.0",
                    "connection_timeout": 15,
                    "retry_initial_delay": 5,
                    "retry_max_delay": 30,
                    "retry_multiplier": 2.0,
                },
            },
        ],
        "settings": {
            "auto_start": True,
            "status_poll_interval": 2.0,
            "metrics_enabled": True,
        },
    }


def main() -> None:
    """Start simulators, write config, and launch the app."""
    # 1. Start the TCP source simulator
    sim = TCPSourceSimulator(port=SIMULATOR_PORT, data_rate_bps=SIMULATOR_DATA_RATE)
    sim.start()
    atexit.register(sim.stop)

    # 2. Start the mock NTRIP caster
    ntrip_caster = MockNtripCaster(
        port=0,  # ephemeral port
        password=NTRIP_PASSWORD,
        accept_auth=True,
    )
    ntrip_caster.start()
    atexit.register(ntrip_caster.stop)

    # 3. Write pre-configured demo config to a temp file
    config_data = _build_demo_config(
        source_port=SIMULATOR_PORT,
        tcp_dest_port=TCP_DEST_PORT,
        ntrip_port=ntrip_caster.port,
        ntrip_password=NTRIP_PASSWORD,
        ntrip_mountpoint=NTRIP_MOUNTPOINT,
    )

    tmp_dir = tempfile.mkdtemp(prefix="sp_rtk_base_demo_")
    config_path = Path(tmp_dir) / "config.yaml"
    config_path.write_text(
        yaml.dump(config_data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    # Point sp-rtk-base at the demo config
    os.environ["SP_RTK_BASE_CONFIG"] = str(config_path)

    # 4. Start a TCP client that connects to the tcp_server destination
    #    (without a connected client, tcp_server shows 0 bytes sent)
    tcp_client = TCPDestinationClient()
    tcp_client.host = "127.0.0.1"
    tcp_client.port = TCP_DEST_PORT

    def _connect_tcp_client() -> None:
        """Connect to the TCP server dest after a delay (relay needs to start first)."""
        import time

        time.sleep(5.0)  # Wait for relay + tcp_server to start
        try:
            tcp_client.connect(timeout=10.0)
            # Read data continuously in background
            tcp_client.wait_for_data(min_bytes=0, timeout=3600.0)
        except Exception:
            pass  # Demo client — errors are OK

    import threading

    client_thread = threading.Thread(
        target=_connect_tcp_client, daemon=True, name="demo-tcp-client"
    )
    client_thread.start()
    atexit.register(tcp_client.disconnect)

    # 5. Print banner
    print()
    print("=" * 65)
    print("  SP-Base Demo — Fully Pre-Configured")
    print("=" * 65)
    print()
    print(f"  📡 RTCM source simulator: 127.0.0.1:{SIMULATOR_PORT}")
    print(f"     Data rate: {SIMULATOR_DATA_RATE} bytes/sec")
    print()
    print("  📤 Destinations (auto-configured):")
    print(f"     • TCP Server: 0.0.0.0:{TCP_DEST_PORT}  (connect with any TCP client)")
    print(f"     • NTRIP v1.0: 127.0.0.1:{ntrip_caster.port}/{NTRIP_MOUNTPOINT}")
    print()
    print("  🚀 Auto-start: ENABLED — relay starts on launch!")
    print()
    print("  🌐 Open http://localhost:8080 — Dashboard shows live data")
    print(f"  📊 Prometheus: http://localhost:8080/metrics")
    print()
    print(f"  Config: {config_path}")
    print()
    print("  Press Ctrl+C to stop everything")
    print("=" * 65)
    print()

    # 6. Launch the sp-rtk-base app (blocks until shutdown)
    from sp_rtk_base.app import init_app

    # pyright: ignore[reportUnknownMemberType]
    from nicegui import ui  # type: ignore[import-untyped]

    init_app()

    ui.run(
        title="SP-Base (Demo)",
        host="0.0.0.0",
        port=8080,
        favicon="📡",
        dark=True,
        reload=False,
        show=False,
        storage_secret="sp-rtk-base-demo-secret",
    )


if __name__ == "__main__":
    main()
