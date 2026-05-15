# u-blox GPS Base Station Web UI — Project Planning

## Purpose

Build a lightweight Python web UI to control and monitor a u-blox GPS device used as a base station.

The system should allow an operator to:

- Connect to a GPS source over serial, USB serial, or TCP
- Configure the receiver using Python functions (u-blox UBX protocol)
- Start and stop base-station streaming/runtime behavior
- Forward RTCM or related data to multiple downstream consumers
- Observe live runtime status, errors, connected clients, and configuration progress
- Keep the runtime engine reusable outside the web UI

This document captures the current planning decisions, architecture direction, resolved design questions, and next-step design work.

---

## Project Goal

Create a maintainable system with **two clear layers**:

1. **sp-rtk-base-relay** (existing package, enhanced in v2.1)
   - Owns RTCM data relay: input sources → fan-out → multiple output destinations
   - Provides `RelayEngine` facade API for programmatic control
   - Emits events via EventBus for real-time status updates
   - Exposes typed status snapshots
   - Supports dynamic destination management (hot add/remove/start/stop)
   - Continues to work standalone as a CLI + systemd service

2. **gps-webui** (new package)
   - Provides browser-based control plane and monitoring interface
   - Owns u-blox device configuration (via PyUBX2 library)
   - Orchestrates relay start/stop and GPS configuration workflows
   - Presents status, events, and diagnostics to the operator

---

## Architecture Overview — Corrected

### Key Architectural Decision: sp-rtk-base-relay is a Dependency

sp-rtk-base-relay is **not** being renamed or restructured. It remains its own package with the same purpose: **relay RTCM correction data from GPS input sources to multiple output destinations**.

The web UI imports sp-rtk-base-relay as a Python dependency and controls it **in-process** via the `RelayEngine` API.

### Responsibility Separation

| Responsibility | Owner |
|---|---|
| RTCM data relay (input → fan-out → destinations) | **sp-rtk-base-relay** |
| Input source management (serial, TCP, Bluetooth) | **sp-rtk-base-relay** |
| Destination management (SurePath, NTRIP, TCP server) | **sp-rtk-base-relay** |
| Per-destination message filtering | **sp-rtk-base-relay** |
| Prometheus metrics export | **sp-rtk-base-relay** |
| Event bus & status snapshots | **sp-rtk-base-relay** (v2.1) |
| Dynamic destination add/remove/start/stop | **sp-rtk-base-relay** (v2.1) |
| u-blox device configuration (UBX protocol) | **gps-webui** (via PyUBX2) |
| Base station mode setup (survey-in, fixed) | **gps-webui** |
| RTCM message selection on the GPS device | **gps-webui** (via PyUBX2) |
| Device backup/restore | **gps-webui** (via PyUBXUtils) |
| Web browser interface | **gps-webui** |
| FastAPI REST endpoints | **gps-webui** |
| WebSocket event bridge | **gps-webui** |
| Configuration persistence (profiles, settings) | **gps-webui** |

### Dependency Direction

```
gps-webui
  ├── depends on: sp-rtk-base-relay  (RelayEngine API for RTCM relay)
  ├── depends on: pyubx2         (u-blox UBX protocol messages)
  ├── depends on: pyubxutils     (backup/restore/compare configs)
  ├── depends on: fastapi        (REST API backend)
  ├── depends on: nicegui        (Python-driven browser UI)
  └── depends on: pyserial       (serial port access for GPS config)
```

**sp-rtk-base-relay never depends on gps-webui.** The dependency is strictly one-way.

### System Architecture Diagram

```text
┌─────────────────────────────────────────────────────────────────────┐
│  Browser (Operator Console)                                         │
│  ├── Device page      — connect/disconnect GPS, view device info    │
│  ├── Base Config page — survey-in, fixed mode, RTCM msg selection   │
│  ├── Outputs page     — manage NTRIP/SurePath/TCP destinations      │
│  └── Status page      — live status, events, throughput, errors     │
└───────────────┬─────────────────────────────────────────────────────┘
                │ HTTP / WebSocket
┌───────────────▼─────────────────────────────────────────────────────┐
│  gps-webui package                                                   │
│  ├── FastAPI REST endpoints (/api/status, /api/device/connect, ...) │
│  ├── WebSocket event bridge (/ws/events)                             │
│  ├── NiceGUI operator pages                                          │
│  ├── Orchestration service layer                                     │
│  │   ├── DeviceService (serial port ownership, UBX config)          │
│  │   ├── RelayService (wraps sp-rtk-base-relay RelayEngine)             │
│  │   └── ProfileService (saved configs, persistence)                │
│  └── u-blox config helpers (built on PyUBX2)                        │
│      ├── configure_survey_in(...)                                    │
│      ├── configure_fixed_base(...)                                   │
│      ├── configure_rtcm_messages(...)                                │
│      └── save_device_config(...)                                     │
└───────┬────────────────────────────┬────────────────────────────────┘
        │ Python API (in-process)    │ Serial/USB (when relay stopped)
┌───────▼──────────────┐    ┌───────▼──────────────────────────┐
│  sp-rtk-base-relay v2.1  │    │  u-blox GPS Receiver (ZED-F9P)   │
│  ├── RelayEngine     │    │  ├── RTCM output (serial/USB)    │
│  ├── BroadcastHub    │◄───│  ├── UBX config interface        │
│  ├── EventBus        │    │  └── NMEA position data          │
│  ├── Destinations    │    └──────────────────────────────────┘
│  │   ├── SurePath    │
│  │   ├── NTRIP       │──────▶ NTRIP Casters (RTK2go, Onocoy, ...)
│  │   └── TCP Server  │──────▶ LAN Clients
│  └── Prometheus      │──────▶ Monitoring Stack
└──────────────────────┘
```

---

## Resolved Design Decisions

### Decision 1: sp-rtk-base-relay stays sp-rtk-base-relay
**Decision**: sp-rtk-base-relay is not renamed. It remains a standalone relay package enhanced in v2.1 to be embeddable.

**Reasoning**: The package already has a clear, well-tested purpose. Renaming or restructuring it would cause unnecessary disruption. Instead, v2.1 adds a `RelayEngine` facade that makes it easy to control programmatically.

### Decision 2: In-process integration (DR-8)
**Decision**: gps-webui imports sp-rtk-base-relay as a Python dependency and controls it in the same process.

**Reasoning**:
- Simplest integration — direct Python method calls
- Full access to all objects with zero latency
- Single process to manage/deploy
- Threading model coexists naturally with async FastAPI

**Rejected alternative**: Out-of-process IPC (HTTP API between processes) — too complex for limited benefit.

### Decision 3: Programmatic configuration (DR-9)
**Decision**: gps-webui builds `Config`, `DestinationConfig`, and `InputConfig` dataclasses directly in Python.

**Reasoning**:
- Type-safe — validation happens at object creation time
- No temporary YAML files to manage
- Natural for in-process usage
- The config dataclasses already exist with `__post_init__` validation

**Persistence**: The UI can serialize config to YAML for persistence (saving settings across restarts), but YAML is not required for runtime operation.

### Decision 4: Polling + Event Bus for status (DR-10)
**Decision**: The UI uses both snapshot polling and a push-based event stream from sp-rtk-base-relay.

**Implementation**:
- `RelayEngine.get_status()` → returns typed `RelayStatus` snapshot (polled ~1s by UI)
- `RelayEngine.subscribe_events()` → returns `EventSubscription` for real-time `RelayEvent` stream
- `RelayEngine.get_recent_events()` → returns ring buffer of recent events for page-load context

**WebSocket bridge**: The gps-webui package bridges `EventSubscription` to a WebSocket endpoint (`/ws/events`) so the browser gets real-time updates.

**Reasoning**: Polling provides current state for dashboard display. Events provide "what just happened" for the activity log. Together they give the operator full situational awareness.

### Decision 5: Hot add/remove destinations (DR-11)
**Decision**: Destinations can be added and removed while the relay is running, without interrupting other destinations.

**Implementation**: `RelayEngine.add_destination(config)` and `RelayEngine.remove_destination(name)` modify the destination list while the hub continues broadcasting.

**Reasoning**: The operator should be able to add RTK2go output without interrupting the SurePath stream. Stop-and-restart would cause brief interruptions to all destinations.

### Decision 6: Per-destination start/stop (DR-12)
**Decision**: Individual destinations can be started and stopped independently.

**Implementation**: `RelayEngine.start_destination(name)` and `RelayEngine.stop_destination(name)` control specific destination threads.

**Reasoning**: An operator may want to pause NTRIP publishing while keeping the SurePath connection active. This is a common operational need.

### Decision 7: Serial port handoff between relay and GPS configuration
**Decision**: The relay engine and GPS configuration are mutually exclusive on the serial port. When the relay is stopped, the serial port is released and available for GPS configuration commands.

**Workflow**:
```
1. UI starts → relay is NOT running (serial port available)
2. User configures GPS device → UI uses PyUBX2 on serial port directly
3. User starts relay → UI calls engine.start() → serial port owned by relay
4. User stops relay → UI calls engine.stop() → serial port released
5. User can configure GPS device again
```

**Reasoning**: Dual ownership of a serial port causes race conditions. Clean separation avoids complex synchronization. The existing sp-rtk-base-relay engine already supports clean start/stop lifecycle.

### Decision 8: Separate packages, one repository initially
**Decision**: gps-webui is a separate Python package from sp-rtk-base-relay. During development, both may live in the same repository for convenience.

**Repository layout** (initial):
```
repo/
  packages/
    sp-rtk-base-relay/    (existing, enhanced to v2.1)
    gps-webui/        (new)
```

Or they may remain in separate repositories. This is a development convenience decision, not an architectural one.

---

## u-blox Configuration Tools — Technology Selection

Based on research documented in "Tools for Mass Configuration & Backup.md", the following tools are recommended for the gps-webui package:

### Primary: PyUBX2 (Python library)
**Source**: [semuconsulting/pyubx2](https://github.com/semuconsulting/pyubx2) (PyPI: `pyubx2`)

**Why**: Provides full programmatic control over u-blox receivers via UBX protocol. Supports:
- All UBX message types (GET/SET/POLL)
- Gen9+ configuration database (CFG-VALGET, CFG-VALSET, CFG-VALDEL)
- RTCM3 and NMEA parsing
- ACK/NAK handling
- Cross-platform, actively maintained

**Usage in gps-webui**:
```python
from pyubx2 import UBXMessage, UBXReader
import serial

# Open serial connection to GPS
stream = serial.Serial('/dev/ttyACM0', 115200, timeout=1)
ubr = UBXReader(stream)

# Configure survey-in mode
msg = UBXMessage(
    "CFG", "CFG-VALSET",
    SET,
    layers=1,  # RAM only
    cfgData=[
        ("CFG-TMODE-MODE", 1),          # Survey-in mode
        ("CFG-TMODE-SVIN_MIN_DUR", 120),  # 120 seconds minimum
        ("CFG-TMODE-SVIN_ACC_LIMIT", 50000),  # 5.0m accuracy
    ]
)
stream.write(msg.serialize())

# Read ACK/NAK
raw, parsed = ubr.read()
```

### Secondary: PyUBXUtils (CLI backup/restore tools)
**Source**: [semuconsulting/pyubxutils](https://github.com/semuconsulting/pyubxutils) (PyPI: `pyubxutils`)

**Why**: Provides ready-made CLI tools for device backup/restore that the UI can invoke:
- `ubxsave` — full config backup to .ubx file
- `ubxload` — atomic config restore from file
- `ubxcompare` — diff two config files
- `ubxbase` — quick RTK base station setup

**Usage in gps-webui**: The UI's "Backup/Restore" page can call these tools programmatically or shell out to their CLI.

### Reference: PyGPSClient
**Source**: [semuconsulting/PyGPSClient](https://github.com/semuconsulting/PyGPSClient)

**Why**: Provides a reference implementation of a Python GUI for u-blox devices. Built on tkinter with PyUBX2 under the hood. Useful as a reference for:
- UBX configuration dialog patterns
- GNSS data display approaches
- NTRIP server/client integration

---

## Operational Lifecycle — How It All Works Together

### Scenario: Operator Sets Up a New Base Station

```
Step 1: Power on GPS, connect USB cable to Raspberry Pi
Step 2: Open browser, navigate to gps-webui (e.g., http://pi:8080)

Step 3: Device Page
  - Select serial port: /dev/ttyACM0
  - Click "Connect"
  - UI opens serial port via PyUBX2, reads MON-VER → shows device info
  - Shows: ZED-F9P, firmware 1.32, protocol 27.31

Step 4: Base Config Page
  - Choose "Survey-In" mode
  - Set minimum duration: 300 seconds
  - Set accuracy target: 2.0 meters
  - Select RTCM messages: 1005, 1077, 1087, 1097, 1127, 1230
  - Click "Configure"
  - UI sends UBX CFG-VALSET commands via PyUBX2
  - Progress: "Configuring... ACK received... Survey-in started..."
  - Wait for survey-in to complete (live progress shown)

Step 5: Outputs Page
  - Add destination: SurePath (host, port, username, password)
  - Add destination: RTK2go (caster, mount, password)
  - Each shows as a card with enable/disable toggle

Step 6: Start Relay
  - Click "Start Streaming"
  - UI disconnects PyUBX2 from serial port
  - UI creates RelayEngine with serial input config
  - UI calls engine.start() with destination configs
  - Relay begins: GPS → sp-rtk-base-relay → SurePath + RTK2go

Step 7: Monitor
  - Status page shows live:
    - Input: connected, 1.2 KB/s
    - SurePath: connected, 500 bytes/s, 0 errors
    - RTK2go: connected, 480 bytes/s, 0 errors
  - Event log: "10:30:00 — relay started", "10:30:01 — surepath connected", ...

Step 8: Runtime Changes
  - Operator adds Onocoy destination → hot-added, no interruption
  - Operator pauses RTK2go → stopped, surepath+onocoy continue
  - Operator resumes RTK2go → restarted

Step 9: Reconfigure (if needed)
  - Click "Stop Streaming" → relay stops, serial port released
  - Go to Base Config page → modify settings → reconfigure
  - Click "Start Streaming" → relay restarts
```

---

## sp-rtk-base-relay RelayEngine API Surface

This is the exact API that gps-webui will use. Defined in detail in `docs/v2.1-architecture-plan.md`.

### Lifecycle
```python
from sp_rtk_base_relay.engine import RelayEngine
from sp_rtk_base_relay.config import InputConfig, DestinationConfig

engine = RelayEngine(input_config=InputConfig(source="serial", config={...}))
engine.start(destinations=[dest_config_1, dest_config_2])
engine.stop()
```

### Destination Management
```python
engine.add_destination(new_dest_config)       # Hot-add while running
engine.remove_destination("onocoy")           # Hot-remove while running
engine.stop_destination("rtk2go")             # Pause one destination
engine.start_destination("rtk2go")            # Resume one destination
engine.get_destination_names()                # List all destinations
```

### Status & Events
```python
from sp_rtk_base_relay.core.status import RelayStatus
from sp_rtk_base_relay.core.events import RelayEvent, EventSubscription

status: RelayStatus = engine.get_status()     # Typed snapshot
events: list[RelayEvent] = engine.get_recent_events(count=50)
sub: EventSubscription = engine.subscribe_events()

# In gps-webui's WebSocket handler:
for event in sub:
    await websocket.send_json(asdict(event))
```

---

## gps-webui Package Structure

```text
gps-webui/
  src/gps_webui/
    main.py                    # FastAPI + NiceGUI app entry point
    api/
      status.py                # GET /api/status, GET /api/events/recent
      device.py                # POST /api/device/connect, /disconnect
      config.py                # POST /api/device/configure/survey-in, /fixed
      outputs.py               # POST/DELETE /api/outputs, toggle enable
      relay.py                 # POST /api/relay/start, /stop
      websocket.py             # WS /ws/events
    ui/
      pages/
        device_page.py         # Device connection and info
        base_config_page.py    # Survey-in, fixed mode, RTCM selection
        outputs_page.py        # Destination management
        status_page.py         # Live status, events, throughput
    services/
      device_service.py        # Serial port + PyUBX2 GPS configuration
      relay_service.py         # Wraps sp-rtk-base-relay RelayEngine
      profile_service.py       # Saved configs, persistence
    device/
      ublox_controller.py      # UBX config helpers (survey-in, fixed, RTCM)
      ublox_status.py          # Parse NAV-PVT, NAV-SVIN for status display
      device_info.py           # Parse MON-VER for device identification
    models/
      device_models.py         # Pydantic models for device config
      output_models.py         # Pydantic models for destination config
      status_models.py         # Pydantic models for API responses
```

---

## Core Design Principle (Unchanged)

The browser should act as an **operator console**, not the source of truth.

The authoritative runtime state lives in:
- **sp-rtk-base-relay** for relay state (via RelayEngine / EventBus)
- **gps-webui backend** for device configuration state

The browser:
- Requests actions
- Renders status and events
- Does NOT directly manage threads, sockets, or serial ports

---

## Runtime Model

### 1. Snapshot State (from sp-rtk-base-relay)
`RelayEngine.get_status()` returns a frozen `RelayStatus` dataclass:
- Input source: connected/disconnected, source type, bytes/sec
- Per-destination: connected, bytes_sent, errors, queue_depth, last_error
- Hub: running, uptime, frames_parsed, no_data_warnings

### 2. Event Stream (from sp-rtk-base-relay)
`RelayEngine.subscribe_events()` provides real-time `RelayEvent` objects:
- `hub.started`, `hub.stopped`
- `destination.connected`, `destination.error`, `destination.added`, `destination.removed`
- `input.connected`, `input.disconnected`, `input.reconnected`

### 3. Device Status (from gps-webui, via PyUBX2)
When relay is stopped and PyUBX2 has the serial port:
- Fix status (NAV-PVT: fixType, carrSoln)
- Position (latitude, longitude, altitude)
- Satellite count (numSV)
- Survey-in progress (NAV-SVIN: dur, meanAcc, active, valid)

### 4. Prometheus Metrics (from sp-rtk-base-relay)
Per-destination counters/gauges for monitoring and alerting.

---

## API Direction

### Relay Control
- `POST /api/relay/start` — start the relay engine
- `POST /api/relay/stop` — stop the relay engine
- `GET /api/relay/status` — get RelayStatus snapshot

### Device Control
- `POST /api/device/connect` — open serial port, query device info
- `POST /api/device/disconnect` — close serial port
- `GET /api/device/info` — device model, firmware version, protocol version

### GPS Configuration (requires relay stopped)
- `POST /api/device/configure/survey-in` — configure survey-in mode
- `POST /api/device/configure/fixed` — configure fixed base mode
- `POST /api/device/configure/rtcm-messages` — select RTCM output messages
- `POST /api/device/save-config` — save config to device flash
- `POST /api/device/backup` — backup full config to file
- `POST /api/device/restore` — restore config from file

### Output/Destination Management
- `GET /api/outputs` — list all destinations
- `POST /api/outputs` — add a new destination (hot-add)
- `DELETE /api/outputs/{name}` — remove a destination (hot-remove)
- `POST /api/outputs/{name}/start` — start a destination
- `POST /api/outputs/{name}/stop` — stop a destination

### Live Events
- `WS /ws/events` — WebSocket stream of RelayEvent + device events
- `GET /api/events/recent` — recent events from ring buffer

---

## Testing Direction

### sp-rtk-base-relay tests (existing + v2.1 additions)
- All existing 956+ tests unchanged
- New v2.1 tests (~200): EventBus, RelayStatus, dynamic destinations, RelayEngine facade

### gps-webui tests
**Unit tests** (no hardware):
- API route validation and response mapping
- Service layer orchestration logic
- UBX message construction (mocked serial port)
- WebSocket event forwarding
- State machine transitions (disconnected → connected → configuring → ready)

**Integration tests** (with mock GPS):
- Full workflow: connect → configure → start relay → monitor → stop
- Hot add/remove destinations during relay
- Error handling: device disconnect mid-config, destination failure

**Hardware tests** (with real GPS):
- Actual UBX configuration of ZED-F9P
- Real RTCM data relay to test casters
- Survey-in completion with live GPS signal

---

## Open Questions Remaining

### 1. Threading vs asyncio boundary in gps-webui
sp-rtk-base-relay uses threads internally. FastAPI is async. The gps-webui service layer needs to bridge:
- Thread-based EventSubscription → async WebSocket push
- Sync RelayEngine calls → async FastAPI endpoints

Likely approach: `asyncio.to_thread()` for RelayEngine calls, dedicated asyncio task consuming EventSubscription.

### 2. Persistence scope for gps-webui
What should be persisted:
- Saved device profiles (serial port, baud rate)
- Saved output profiles (NTRIP caster configs)
- Base station position (once surveyed)
- Last known good relay configuration
- UI preferences

Storage options: SQLite, JSON files, or YAML files. Decision deferred to gps-webui design phase.

### 3. Security / authentication
- Local-only access? LAN only?
- API key or session auth?
- Protected by reverse proxy?

Decision deferred — not critical for initial development.

### 4. Packaging and release workflow
- Monorepo or separate repos?
- Shared version numbers or independent?
- PyPI publishing?

Decision deferred to later planning.

---

## Design Patterns In Use

### In sp-rtk-base-relay (existing + v2.1)
| Pattern | Usage |
|---|---|
| **Strategy** | Input sources (InputSource ABC) and destinations (BaseDestination ABC) |
| **Factory** | InputSourceFactory, DestinationFactory (registry-based) |
| **Fan-Out** | BroadcastHub → N destination queues |
| **Observer/Pub-Sub** | EventBus with subscriber queues (v2.1) |
| **Facade** | RelayEngine wraps BroadcastHub + destinations + events (v2.1) |
| **Circuit Breaker** | Per-destination exponential backoff retry |

### In gps-webui (planned)
| Pattern | Usage |
|---|---|
| **Layered Architecture** | UI → API → Service → Core engine |
| **Adapter** | RelayService wraps RelayEngine for web context |
| **State Machine** | Device states: disconnected → connected → configuring → ready |
| **Supervisor** | Service layer owns all worker lifecycles |
| **Repository** | Persistence of profiles, settings, event history |

---

## Proposed Near-Term Next Steps

### Step 1: sp-rtk-base-relay v2.1 (prerequisite)
Implement the v2.1 enhancements as defined in `docs/v2.1-architecture-plan.md`:
1. Event Bus system
2. Typed status snapshots
3. Dynamic destination management
4. RelayEngine facade
5. Integration tests & documentation

### Step 2: gps-webui skeleton
Create the gps-webui package with:
- FastAPI + NiceGUI app scaffold
- Basic device connection page (serial port selection, connect button)
- Status endpoint consuming RelayEngine.get_status()
- WebSocket event bridge

### Step 3: GPS configuration workflow
Add u-blox configuration support:
- Survey-in mode configuration
- Fixed base mode configuration
- RTCM message selection
- Config save to device flash

### Step 4: Output management UI
Build the destination management interface:
- Add/remove destinations via web forms
- Enable/disable toggle per destination
- Live status per destination

### Step 5: Full monitoring dashboard
Complete the status page:
- Real-time throughput graphs
- Event log display
- Error/warning indicators
- Survey-in progress visualization

---

## Summary

The architecture direction is:

- **sp-rtk-base-relay** remains the standalone RTCM relay engine, enhanced in v2.1 with embeddable API, events, and dynamic destination management
- **gps-webui** is a new separate package that depends on sp-rtk-base-relay for relay functionality and PyUBX2 for GPS device configuration
- **FastAPI + NiceGUI** for the web layer — Python-native, backend-first philosophy
- **In-process integration** — gps-webui imports and controls sp-rtk-base-relay directly via RelayEngine
- **Clean separation** — relay logic stays in sp-rtk-base-relay, device config stays in gps-webui
- **Serial port handoff** — relay owns the port when running, PyUBX2 owns it when relay is stopped
- **Event-driven status** — EventBus for real-time updates, snapshot polling for current state

This architecture preserves sp-rtk-base-relay's standalone utility while making it a powerful foundation for the GPS base station management UI.
