# SP-Base

Web UI and REST API for controlling and monitoring a u-blox GPS RTK base station RTCM correction data relay.

SP-Base wraps the [sp-base-relay](packages/sp-base-relay/) engine with a browser-based dashboard and a full REST API, making it easy to configure input sources, manage output destinations, start/stop the relay, and monitor throughput — all from a phone, tablet, or desktop browser.

## Features

- **Dashboard** — real-time relay status, input metrics, throughput stats, event log
- **Destination Management** — add/edit/delete SurePath, NTRIP, and TCP Server outputs
- **Settings** — configure TCP, serial, or Bluetooth RTCM input sources
- **REST API** — full programmatic control (relay, destinations, settings, events)
- **Prometheus Metrics** — `GET /metrics` endpoint for Grafana/Prometheus monitoring
- **WebSocket Events** — real-time event streaming at `WS /api/events/ws`

## Quick Start

### Prerequisites

- Python 3.10+
- [UV](https://docs.astral.sh/uv/) package manager

### Install & Run

```bash
# Clone and install dependencies
git clone https://github.com/rodenj1/sp-base.git
cd sp-base
uv sync

# Start the application
uv run sp-base
```

Open **http://localhost:8080** in your browser.

### Demo Mode

Run with simulated RTCM source and mock destinations (no hardware needed):

```bash
uv run python tools/demo_with_simulator.py
```

This starts:
- **TCP source simulator** — streams synthetic RTCM3 data on port 19800
- **Mock NTRIP caster** — accepts NTRIP v1.0 connections (ephemeral port)
- **SP-Base** — pre-configured with TCP input, tcp_server destination (port 19876), and NTRIP destination pointing to the mock caster

The relay auto-starts on launch. Open **http://localhost:8080** and you'll see live data flowing on the Dashboard immediately.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/relay/status` | Relay engine status |
| `POST` | `/api/relay/start` | Start the relay |
| `POST` | `/api/relay/stop` | Stop the relay |
| `GET` | `/api/destinations` | List all destinations |
| `POST` | `/api/destinations` | Create a destination |
| `GET` | `/api/destinations/{name}` | Get destination details |
| `PUT` | `/api/destinations/{name}` | Update a destination |
| `DELETE` | `/api/destinations/{name}` | Delete a destination |
| `GET` | `/api/settings` | Get application settings |
| `PUT` | `/api/settings` | Update application settings |
| `GET` | `/api/settings/input` | Get input source config |
| `PUT` | `/api/settings/input` | Update input source config |
| `GET` | `/api/events` | Poll recent events |
| `WS` | `/api/events/ws` | WebSocket event stream |
| `GET` | `/metrics` | Prometheus metrics |

## Prometheus Integration

SP-Base serves Prometheus metrics at `GET /metrics` on the same port (8080).

Example `prometheus.yml` scrape config:

```yaml
scrape_configs:
  - job_name: "sp-base"
    scrape_interval: 10s
    static_configs:
      - targets: ["localhost:8080"]
```

Key metrics include:
- `sp_base_relay_running` — relay engine state (1/0)
- `sp_base_relay_uptime_seconds` — engine uptime
- `sp_base_input_connected` — input source connection state
- `sp_base_input_bytes_received` — total bytes from input
- `sp_base_active_destinations` / `sp_base_total_destinations`
- `sp_base_dest_connected{destination="name"}` — per-destination status
- `sp_base_dest_bytes_sent{destination="name"}` — per-destination throughput

## Development

### Run Tests

```bash
# Unit tests
uv run pytest tests/unit/

# Integration tests (uses real relay engine with TCP simulator)
uv run pytest tests/integration/ --no-cov

# All tests
uv run pytest
```

### Type Checking

```bash
uv run pyright src/
```

### Project Structure

```
sp-base/
├── src/sp_base/
│   ├── api/            # FastAPI REST endpoints
│   ├── models/         # Pydantic config & API models
│   ├── services/       # Business logic (config, relay, events, metrics)
│   └── ui/             # NiceGUI browser pages
│       ├── components/ # Reusable UI components
│       └── pages/      # Dashboard, Outputs, Settings
├── tests/
│   ├── unit/           # Fast unit tests with mocks
│   ├── integration/    # End-to-end tests with real relay
│   └── fixtures/       # TCP simulators & test helpers
├── packages/
│   └── sp-base-relay/  # RTCM relay engine (local dependency)
└── tools/              # Demo scripts
```

## Architecture

SP-Base is built with **FastAPI** (REST API) and **NiceGUI** (browser UI) sharing a single ASGI server on port 8080. The **sp-base-relay** package provides the core RTCM relay engine with support for TCP, serial, and Bluetooth inputs, and SurePath, NTRIP, and TCP Server output destinations.

## License

MIT
