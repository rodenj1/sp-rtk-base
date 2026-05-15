# Technical Context

## Technology Stack

### Core Technologies
- **Python 3.10+**: Modern Python with full type hint support
- **UV Package Manager**: Fast Python package and project manager
- **FastAPI**: Async REST API framework with automatic OpenAPI docs
- **NiceGUI**: Python-native web UI framework (server-side rendering, no JS build)
- **sp-rtk-base-relay v2.1**: RTCM relay engine (PyPI dependency)
- **Pydantic v2**: Data validation and API models (included with FastAPI)
- **PyYAML**: Configuration persistence

### Key Dependencies (as of April 2026)
```toml
[project.dependencies]
sp-rtk-base-relay = ">=2.1.0"       # RelayEngine API for RTCM relay
fastapi = ">=0.135.0"           # REST API + WebSocket (latest: 0.135.1)
nicegui = ">=3.9.0"             # Python-native browser UI (latest: 3.9.0)
uvicorn = ">=0.42.0"            # ASGI server (latest: 0.42.0)
pyyaml = ">=6.0.2"              # Config persistence (latest: 6.0.2)
pydantic = ">=2.12.0"           # Data validation / API models (latest: 2.12.5)
```

### Development Dependencies (as of April 2026)
```toml
[dependency-groups]
dev = [
    "pyright>=1.1.396",          # Static type checker (latest: 1.1.396+)
    "pytest>=9.0.0",             # Testing framework (latest: 9.0.2)
    "pytest-asyncio>=1.3.0",     # Async test support (latest: 1.3.0)
    "pytest-cov>=7.1.0",         # Coverage plugin (latest: 7.1.0)
]
```

### Future Dependencies (Phase 2+)
```
pyubx2 >= 1.2.60       # u-blox UBX protocol for GPS configuration
pyubxutils              # GPS config backup/restore/compare
pyserial                # Serial port access (already a dep of sp-rtk-base-relay)
```

## Development Setup

### Package Management with UV
```bash
cd /opt/development/sp-rtk-base

# Install dependencies
uv sync

# Run the app
uv run sp-rtk-base

# Run tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/sp_rtk_base --cov-report=term-missing
```

### Project Structure
```
sp-rtk-base/                           # Monorepo root
├── pyproject.toml                 # sp-rtk-base package config (UV)
├── .python-version                # Python version (3.10+)
├── .gitignore
├── README.md
├── memory-bank/                   # Memory bank for sp-rtk-base
│   ├── projectbrief.md
│   ├── productContext.md
│   ├── activeContext.md
│   ├── systemPatterns.md
│   ├── techContext.md
│   └── progress.md
├── docs/                          # Architecture & planning docs
│   ├── relay-engine-api-spec.md   # sp-rtk-base-relay API reference
│   ├── ublox_gps_webui_planning.md # Full project planning
│   └── Tools for Mass Configuration & Back.md
├── src/sp_rtk_base/                   # Main package source
│   ├── __init__.py
│   ├── main.py                    # Entry point (sp-rtk-base)
│   ├── app.py                     # FastAPI + NiceGUI app factory
│   ├── cli/                       # CLI tools
│   │   ├── __init__.py
│   │   └── config_audit.py        # GPS config audit (sp-rtk-base-gps-audit)
│   ├── api/                       # REST API endpoints
│   │   ├── relay.py               # /api/relay/*
│   │   ├── outputs.py             # /api/outputs/*
│   │   ├── inputs.py              # /api/input/*
│   │   ├── events.py              # /api/events/*
│   │   └── websocket.py           # /ws/events
│   ├── ui/                        # NiceGUI pages
│   │   ├── layout.py              # Shared layout
│   │   ├── pages/
│   │   │   ├── dashboard.py       # Status dashboard
│   │   │   ├── outputs.py         # Destination management
│   │   │   └── settings.py        # Input source config
│   │   └── components/
│   │       ├── status_card.py
│   │       ├── destination_card.py
│   │       └── event_log.py
│   ├── services/                  # Business logic
│   │   ├── relay_service.py       # RelayEngine wrapper
│   │   ├── config_service.py      # YAML persistence
│   │   └── event_bridge.py        # Event → WebSocket bridge
│   └── models/                    # Pydantic API models
│       ├── relay_models.py
│       ├── output_models.py
│       └── status_models.py
├── tests/                         # Test suite
│   ├── conftest.py
│   ├── unit/
│   └── integration/
└── packages/                      # Local dev copies
    └── sp-rtk-base-relay/             # sp-rtk-base-relay source (dev reference)
```

## sp-rtk-base-relay API Surface (v2.1)

The primary integration point. Full spec in `docs/relay-engine-api-spec.md`.

### Core Imports
```python
from sp_rtk_base_relay import RelayEngine
from sp_rtk_base_relay import EventBus, EventSubscription, RelayEvent
from sp_rtk_base_relay import RelayStatus, DestinationStatus, InputStatus
from sp_rtk_base_relay.config import (
    InputConfig,
    DestinationConfig,
    DestinationFilterConfig,
    SurePathDestinationConfig,
    NtripDestinationConfig,
    TcpServerDestinationConfig,
)
from sp_rtk_base_relay.exceptions import ServiceError, ConfigurationError
```

### Key API Methods
```python
engine = RelayEngine(input_config)    # Create (stopped)
engine.start(destinations)            # Start relay
engine.stop()                         # Stop relay (releases serial port)
engine.is_running                     # bool property

engine.add_destination(config)        # Hot-add → returns name
engine.remove_destination(name)       # Hot-remove
engine.start_destination(name)        # Resume paused destination
engine.stop_destination(name)         # Pause destination
engine.get_destination_names()        # List all names

engine.get_status()                   # → RelayStatus (frozen snapshot)
engine.subscribe_events()             # → EventSubscription
engine.get_recent_events(count)       # → list[RelayEvent] from ring buffer
```

## FastAPI + NiceGUI Integration

NiceGUI runs on top of FastAPI — they share the same ASGI application and uvicorn server. This means:

- FastAPI routes and NiceGUI pages coexist on the same port
- FastAPI handles `/api/*` and `/ws/*` routes
- NiceGUI handles `/` and UI page routes
- Both use the same uvicorn event loop

```python
from fastapi import FastAPI
from nicegui import app, ui

# FastAPI app is accessible via nicegui's app object
# Or create FastAPI first and mount NiceGUI on it

@app.get("/api/relay/status")
async def get_status():
    return relay_service.get_status()

@ui.page("/")
def dashboard():
    ui.label("SP-Base Dashboard")
```

## Configuration Persistence

### YAML Config File
Location: `~/.config/sp-rtk-base/config.yaml`

```yaml
input:
  source: serial
  config:
    port: /dev/ttyACM0
    baudrate: 115200

destinations:
  - name: surepath
    type: surepath
    enabled: true
    filter:
      mode: pass_all
    config:
      host: surepath.example.com
      port: 50010
      username: myuser
      password: mypass

  - name: rtk2go
    type: ntrip
    enabled: true
    filter:
      mode: pass_all
    config:
      caster: rtk2go.com
      port: 2101
      mountpoint: MY_MOUNT
      password: mypassword

settings:
  auto_start: false
  status_poll_interval: 2.0
```

## Code Quality Standards

### Type Hints & Linting (STRICT)
- Python 3.10+ with modern type hints: `dict`, `list`, `X | None` (NOT `Dict`, `List`, `Optional` — these are deprecated)
- `from __future__ import annotations` in all source files
- **Pyright strict mode**: Zero errors and warnings. Always fix properly — never suppress with `# type: ignore` or pyright exclusions unless absolutely unavoidable
- **Pylance strict mode**: Resolve all issues. Look for the correct solution (proper typing, protocol classes, overloads) rather than suppressing
- PEP8 standards throughout

### Testing (MANDATORY)
- **Pytest** is the testing framework — no unittest
- **Target: >90% unit test code coverage** — enforced via `--cov-fail-under=90`
- Write tests as you develop each module — not as an afterthought
- Tests must be in `tests/unit/` and `tests/integration/`
- Use `pytest-asyncio` for async test functions
- Use `pytest-cov` for coverage reporting
- Mock external dependencies (RelayEngine, serial ports) in unit tests
- FastAPI `TestClient` for API endpoint tests

### Linting Resolution Philosophy
- **Always fix the root cause** — don't add `# type: ignore`, `# noqa`, or pyright suppression comments
- If a type error exists, find the proper type annotation, protocol, or cast
- If a linting warning exists, refactor the code to eliminate it
- Only suppress as absolute last resort with a comment explaining WHY

### Other Standards
- UV package management
- Pydantic v2 models for all API request/response schemas
- Consistent error handling with typed exceptions

## Async/Thread Bridging

### Pattern: asyncio.to_thread() for RelayEngine calls
```python
# RelayEngine methods are synchronous (thread-safe but blocking)
# FastAPI handlers are async
# Bridge with asyncio.to_thread()

async def start_relay():
    await asyncio.to_thread(engine.start, destinations)
```

### Pattern: Daemon thread for EventSubscription → asyncio.Queue
```python
# EventSubscription.get_event() blocks (thread-safe)
# WebSocket handler is async
# Bridge with daemon thread + asyncio.Queue

def _consume_events(sub, queue):
    """Daemon thread."""
    while not sub.is_closed:
        event = sub.get_event(timeout=1.0)
        if event:
            queue.put_nowait(event)

async def ws_handler(websocket):
    """Async WebSocket handler."""
    while True:
        event = await async_queue.get()
        await websocket.send_json(...)
```

## Testing Patterns

### Unit Tests (mocked RelayEngine)
```python
@pytest.fixture
def mock_engine():
    engine = MagicMock(spec=RelayEngine)
    engine.is_running = False
    engine.get_status.return_value = mock_relay_status()
    return engine

def test_start_relay(mock_engine):
    service = RelayService(engine=mock_engine)
    service.start_relay(input_config, destinations)
    mock_engine.start.assert_called_once()
```

### API Tests (FastAPI TestClient)
```python
from fastapi.testclient import TestClient

def test_get_status(client: TestClient, mock_relay_service):
    response = client.get("/api/relay/status")
    assert response.status_code == 200
```

### Integration Tests
- Full workflow: configure → start → add destination → monitor → stop
- WebSocket event streaming
- Config persistence round-trip

## Hardware Environment (Phase 2+ Reference)

### Test GPS Receiver — u-blox ZED-F9P
| Field | Value |
|---|---|
| Module | ZED-F9P (High Precision GNSS) |
| Firmware | HPG 1.12 |
| Protocol | 27.11 |
| Software | EXT CORE 1.00 (61b2dd) |
| Hardware | 00190000 |
| Constellations | GPS, GLONASS, Galileo, BeiDou, QZSS |

### Port Configuration (Validated April 2026)
- **UBX Config Port**: `/dev/ttyUSB0` via FTDI FT232 USB-to-UART adapter @ 57600 baud
  - Vendor: 0403 (Future Technology Devices International)
  - Product: 6001 (FT232 Serial UART IC)
  - Outputs RTCM + responds to UBX commands
- **RTCM Relay Port**: Bluetooth SPP (dedicated RTCM output)
- **ESP Device**: `/dev/ttyACM0` (Espressif USB JTAG — NOT the GPS)

### PyUBX2 Validation Results
- PyUBX2 1.2.60 successfully communicates with ZED-F9P
- UBX-MON-VER poll/response confirmed working
- Baud rate: 57600 (non-standard — must be configured, not auto-detected at common rates)
- Current test config: Scenario 2 — separate UBX and RTCM ports (can query/configure GPS while relay runs uninterrupted)
