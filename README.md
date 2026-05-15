# SP-Base

Web UI and REST API for configuring and monitoring a u-blox GPS RTK base station and its RTCM correction data relay.

SP-Base wraps the [sp-rtk-base-relay](packages/sp-rtk-base-relay/) engine with a browser-based operator console, adds full u-blox device configuration (survey-in, fixed base, GNSS constellations, RTCM message selection), and exposes everything through a REST API — all from a phone, tablet, or desktop browser.

## Features

### Relay Control
- **Dashboard** — real-time relay status, input metrics, throughput stats, GPS device summary, live event log
- **Destination Management** — add/edit/delete/hot-toggle SurePath, NTRIP, and TCP Server outputs (zero-downtime)
- **Input Sources** — configure TCP, serial, or Bluetooth RTCM input (serial port auto-detect with GPS flagging, Bluetooth scan + pair + test-connection)
- **Config Import/Export** — YAML download/upload with validation

### GPS Device Management (u-blox)
- **Connect / Disconnect** — serial port auto-detect (u-blox / FTDI / Prolific / Silicon Labs flagged with ⭐), driver selector, MON-VER device info
- **Survey-In** — configure duration + accuracy target, live convergence chart (ECharts), auto-promote to fixed base on completion + save-to-flash
- **Fixed Base** — read-back current config, edit/commit coordinates, save-to-flash
- **Named Position Profiles** — save surveyed or manual base positions to YAML, restore them directly to device RAM + flash
- **Live Position** — NAV-PVT display (fix type, RTK status, lat/lon/alt, accuracy, satellites, speed, heading, PDOP) auto-polled every 2 s
- **GNSS Constellation Selection** — toggle GPS / GLONASS / Galileo / BeiDou / SBAS / QZSS
- **RTCM Message Selection** — per-port RTCM3 message enable/disable
- **Save to Flash** — persist any configuration change for reboot survival
- **Device → Relay Handoff** — disconnect device and start the relay on the same serial port with one click

### Monitoring & API
- **REST API** — full programmatic control (relay, destinations, settings, events, device, config)
- **Prometheus Metrics** — `GET /metrics` endpoint for Grafana / Prometheus monitoring
- **WebSocket Events** — real-time event streaming at `WS /api/events/ws`
- **Responsive UI** — mobile-first layout, 44 px touch targets, tablet/desktop breakpoints

## Quick Start

### Prerequisites

- Python 3.10+
- [UV](https://docs.astral.sh/uv/) package manager
- (Optional) BlueZ + `dbus-fast` on Linux for Bluetooth RTCM input

### Install & Run

```bash
# Clone and install dependencies
git clone https://github.com/rodenj1/sp-rtk-base.git
cd sp-rtk-base
uv sync

# Start the application
uv run sp-rtk-base
```

Open **http://localhost:8080** in your browser.

### Demo Mode

Run with a simulated RTCM source and mock destinations (no hardware needed):

```bash
uv run python tools/demo_with_simulator.py
```

This starts:
- **TCP source simulator** — streams synthetic RTCM3 data on port 19800
- **Mock NTRIP caster** — accepts NTRIP v1.0 connections (ephemeral port)
- **SP-Base** — pre-configured with TCP input, a `tcp_server` destination on port 19876, and an NTRIP destination pointing to the mock caster

The relay auto-starts on launch. Open **http://localhost:8080** and you'll see live data flowing on the Dashboard immediately.

### Local NTRIP Caster (Dev / Testing)

A lightweight Python asyncio NTRIP caster is included for developing and testing NTRIP v1.0 and v2.0 destinations without depending on a public caster:

```bash
cd docker/ntrip-caster
docker compose up
```

See [`docs/local-ntrip-caster.md`](docs/local-ntrip-caster.md) for auth, mountpoint, and protocol details.

## UI Pages

| Route | Page | Purpose |
|-------|------|---------|
| `/` | **Dashboard** | Relay status, input/output metrics, GPS device summary, event log |
| `/input` | **Input** | Input source config (TCP / serial / Bluetooth) with discovery helpers |
| `/outputs` | **Outputs** | Destination CRUD + enable/disable toggles |
| `/survey` | **Survey-In** | Connect to GPS, run survey-in with live chart, fixed-base config, position profiles |
| `/settings` | **Settings** | Application settings (auto-start, dark mode, metrics toggle) |
| `/gps-config` | **Advanced GPS** | RTCM message selection, GNSS constellations, save-to-flash, relay handoff |

## API Endpoints

### Health
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check |

### Relay
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/relay/status` | Relay engine status |
| `POST` | `/api/relay/start` | Start the relay |
| `POST` | `/api/relay/stop` | Stop the relay |

### Destinations
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/destinations` | List all destinations |
| `POST` | `/api/destinations` | Create a destination |
| `GET` | `/api/destinations/{name}` | Get destination details |
| `PUT` | `/api/destinations/{name}` | Update a destination |
| `DELETE` | `/api/destinations/{name}` | Delete a destination |

### Settings
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/settings` | Get application settings |
| `PUT` | `/api/settings` | Update application settings |
| `GET` | `/api/input` | Get input source config |
| `PUT` | `/api/input` | Update input source config |

### Events
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/events` | Poll recent events |
| `WS` | `/api/events/ws` | WebSocket event stream |

### Metrics & Config
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/api/config/export` | Download full YAML configuration |
| `POST` | `/api/config/import` | Upload and validate a YAML configuration |

### GPS Device (u-blox)
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/device/ports` | List available serial ports (GPS-flagged) |
| `POST` | `/api/device/connect` | Connect to GPS receiver |
| `POST` | `/api/device/disconnect` | Disconnect from GPS receiver |
| `GET` | `/api/device/status` | Device connection state + MON-VER info |
| `GET` | `/api/device/capabilities` | List driver capabilities |
| `GET` | `/api/device/position` | Live NAV-PVT position snapshot |
| `GET` | `/api/device/survey-in` | Survey-in progress (NAV-SVIN) |
| `POST` | `/api/device/configure/survey-in` | Start survey-in mode |
| `POST` | `/api/device/configure/fixed-base` | Configure fixed-base coordinates |
| `POST` | `/api/device/configure/rtcm` | Configure RTCM output messages |
| `GET` | `/api/device/base-config` | Read current base mode (survey/fixed/disabled) |
| `GET` | `/api/device/gnss` | Get GNSS constellation config |
| `PUT` | `/api/device/gnss` | Update GNSS constellation config |
| `POST` | `/api/device/save` | Save current config to device flash |
| `POST` | `/api/device/promote-survey-in` | Promote completed survey-in to fixed base |
| `POST` | `/api/device/handoff` | Disconnect device and start relay on same serial port |
| `GET` | `/api/device/base-positions` | List saved base-station position profiles |
| `POST` | `/api/device/base-positions` | Save a named base-station position |
| `DELETE` | `/api/device/base-positions/{name}` | Delete a saved position |
| `POST` | `/api/device/base-positions/{name}/restore` | Restore a saved position to the device |

## Prometheus Integration

SP-Base serves Prometheus metrics at `GET /metrics` on the same port (8080).

Example `prometheus.yml` scrape config:

```yaml
scrape_configs:
  - job_name: "sp-rtk-base"
    scrape_interval: 10s
    static_configs:
      - targets: ["localhost:8080"]
```

Key metrics include:
- `sp_rtk_base_relay_running` — relay engine state (1/0)
- `sp_rtk_base_relay_uptime_seconds` — engine uptime
- `sp_rtk_base_input_connected` — input source connection state
- `sp_rtk_base_input_bytes_received` — total bytes from input
- `sp_rtk_base_active_destinations` / `sp_rtk_base_total_destinations`
- `sp_rtk_base_dest_connected{destination="name"}` — per-destination status
- `sp_rtk_base_dest_bytes_sent{destination="name"}` — per-destination throughput

## Development

### Run Tests

```bash
# Unit tests
uv run pytest tests/unit/

# Integration tests (real relay engine + TCP simulator)
uv run pytest tests/integration/ --no-cov

# All tests
uv run pytest
```

### Type Checking

```bash
uv run pyright src/
```

### Quality Snapshot
- **Unit tests**: 480 passing
- **Integration tests**: 20+ (end-to-end + destination management + NTRIP)
- **Coverage**: 92.28% on measured code (NiceGUI UI pages excluded — they can't be meaningfully unit-tested)
- **Pyright (strict)**: 0 errors, 0 warnings
- **Python**: 3.10+ with modern type hints (`dict`, `list`, `X | None`)

### Project Structure

```
sp-rtk-base/
├── src/sp_rtk_base/
│   ├── api/              # FastAPI REST endpoints
│   │   ├── config.py        # YAML import/export
│   │   ├── destinations.py  # Destination CRUD
│   │   ├── device.py        # GPS device endpoints
│   │   ├── events.py        # Events + WebSocket
│   │   ├── health.py
│   │   ├── metrics.py       # Prometheus
│   │   ├── relay.py
│   │   └── settings.py
│   ├── models/           # Pydantic config, device & API models
│   ├── services/         # Business logic
│   │   ├── config_service.py
│   │   ├── device_service.py
│   │   ├── event_bridge.py
│   │   ├── metrics_service.py
│   │   ├── relay_service.py
│   │   └── drivers/         # GPS driver layer
│   │       ├── base.py      # GpsReceiverDriver ABC
│   │       └── ublox.py     # u-blox driver (PyUBX2)
│   └── ui/               # NiceGUI browser UI
│       ├── layout.py        # Shared navigation layout
│       ├── validators.py    # Shared form validators
│       ├── components/      # Reusable UI components
│       └── pages/           # Dashboard, Input, Outputs, Survey, Settings, GPS Config
├── tests/
│   ├── unit/             # Fast unit tests with mocks
│   ├── integration/      # End-to-end tests with real relay
│   └── fixtures/         # TCP simulators, mock NTRIP caster, test helpers
├── packages/
│   └── sp-rtk-base-relay/    # RTCM relay engine (workspace dependency)
├── docker/
│   └── ntrip-caster/     # Local NTRIP caster for dev/testing
├── docs/                 # Architecture, planning, device config reference
└── tools/                # Demo and hardware test scripts
```

## Architecture

SP-Base is built on **FastAPI** (REST API + WebSocket) and **NiceGUI** (browser UI) sharing a single ASGI server on port 8080. Core components:

- **sp-rtk-base-relay** — RTCM relay engine with TCP / serial / Bluetooth inputs and SurePath / NTRIP / TCP Server outputs, hot destination management, and in-process event bus
- **GPS driver layer** — abstract `GpsReceiverDriver` base with a u-blox implementation via **PyUBX2** (UBX-MON-VER, CFG-VALSET/VALGET, NAV-PVT, NAV-SVIN, CFG-GNSS, survey-in, fixed-base, RTCM selection, save-to-flash)
- **Services** — async orchestrators bridging the synchronous relay engine and GPS driver to FastAPI's event loop via `asyncio.to_thread()` and daemon-thread event queues
- **Operator console** — 6-page workflow (Dashboard → Input → Outputs → Survey-In → Settings → Advanced GPS) driven by a shared navigation layout; the browser renders status and requests actions, while authoritative runtime state lives in the relay engine and backend services

Graceful shutdown is wired through `app.on_shutdown` so Ctrl+C cleanly stops the event bridge, relay engine, destination threads, and any active GPS connection before uvicorn exits.

## License

MIT
