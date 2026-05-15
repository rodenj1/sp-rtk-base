# System Patterns

## Architecture Overview

SP-Base follows a **layered architecture** with clear separation between UI, API, services, and the relay engine:

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (Operator Console)                                  │
│  ├── Dashboard page     — relay status, destination cards    │
│  ├── Outputs page       — destination CRUD, start/stop       │
│  └── Settings page      — input source configuration         │
└───────────────┬──────────────────────────────────────────────┘
                │ HTTP / WebSocket
┌───────────────▼──────────────────────────────────────────────┐
│  sp-base package (FastAPI + NiceGUI)                          │
│                                                               │
│  ┌─ UI Layer (NiceGUI) ─────────────────────────────────┐    │
│  │  pages/dashboard.py, outputs.py, settings.py          │    │
│  │  components/status_card.py, destination_card.py, ...   │    │
│  └───────────────────────────┬───────────────────────────┘    │
│                              │                                │
│  ┌─ API Layer (FastAPI) ─────▼───────────────────────────┐    │
│  │  api/relay.py, outputs.py, inputs.py, events.py       │    │
│  │  api/websocket.py (WS /ws/events)                      │    │
│  │  models/ (Pydantic request/response models)            │    │
│  └───────────────────────────┬───────────────────────────┘    │
│                              │                                │
│  ┌─ Service Layer ───────────▼───────────────────────────┐    │
│  │  RelayService    — wraps RelayEngine lifecycle         │    │
│  │  ConfigService   — YAML profile persistence            │    │
│  │  EventBridge     — EventSubscription → WebSocket push  │    │
│  └───────────────────────────┬───────────────────────────┘    │
│                              │ Python API (in-process)        │
│  ┌───────────────────────────▼───────────────────────────┐    │
│  │  sp-rtk-base-relay v2.1 (PyPI dependency)                  │    │
│  │  ├── RelayEngine (facade API)                          │    │
│  │  ├── BroadcastHub (fan-out to destinations)            │    │
│  │  ├── EventBus (real-time events + ring buffer)         │    │
│  │  └── Destinations (SurePath, NTRIP, TCP Server)        │    │
│  └───────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────┘
```

## Key Design Patterns

### 1. Layered Architecture
**Purpose**: Clear separation of concerns between UI rendering, HTTP routing, business logic, and relay engine control.

```
UI (NiceGUI pages) → API (FastAPI routes) → Services → RelayEngine
```

Each layer only talks to the layer below it. The UI never calls RelayEngine directly.

### 2. Adapter Pattern — RelayService
**Purpose**: Wrap sp-rtk-base-relay's RelayEngine to provide app-level state management and async bridging.

```python
class RelayService:
    """Adapts threaded RelayEngine for async FastAPI context."""
    
    async def start_relay(self, input_config, destinations) -> None:
        await asyncio.to_thread(self._engine.start, destinations)
    
    async def stop_relay(self) -> None:
        await asyncio.to_thread(self._engine.stop)
    
    async def get_status(self) -> dict:
        status = await asyncio.to_thread(self._engine.get_status)
        return self._format_status(status)
```

### 3. Event Bridge Pattern — EventSubscription → WebSocket
**Purpose**: Bridge sp-rtk-base-relay's threaded EventSubscription to async WebSocket push for browser clients.

```python
class EventBridge:
    """Daemon thread consuming EventSubscription, pushing to async queue."""
    
    def _event_loop(self, sub: EventSubscription) -> None:
        """Background thread: relay events → asyncio queue."""
        while not sub.is_closed:
            event = sub.get_event(timeout=1.0)
            if event:
                self._async_queue.put_nowait(event)
    
    async def stream_events(self, websocket: WebSocket) -> None:
        """Async generator: pull from queue → send to WebSocket."""
        while True:
            event = await self._async_queue.get()
            await websocket.send_json(asdict(event))
```

### 4. Repository Pattern — ConfigService
**Purpose**: Persist configuration profiles (destinations, input source) to YAML files.

```python
class ConfigService:
    """YAML-based profile persistence."""
    
    def save_profiles(self, profiles: AppConfig) -> None: ...
    def load_profiles(self) -> AppConfig: ...
    def save_destination(self, dest: DestinationProfile) -> None: ...
    def remove_destination(self, name: str) -> None: ...
```

Storage location: `~/.config/sp-base/config.yaml`

### 5. Singleton Pattern — Service Instances
**Purpose**: Single instances of RelayService, ConfigService, EventBridge shared across the app via FastAPI dependency injection.

```python
# App-level singletons
relay_service = RelayService()
config_service = ConfigService()
event_bridge = EventBridge(relay_service)

# FastAPI dependency injection
def get_relay_service() -> RelayService:
    return relay_service
```

### 6. Observer Pattern — NiceGUI Reactivity
**Purpose**: UI components react to state changes via NiceGUI's built-in reactivity system and periodic polling.

- Status dashboard polls `RelayService.get_status()` every ~2 seconds via `ui.timer`
- Event log receives push updates via WebSocket connection
- Destination cards update on poll cycle

## Threading & Async Model

```
┌─────────────────────────────────────────────────┐
│  Main Thread: uvicorn → FastAPI + NiceGUI       │
│    ├── async REST endpoint handlers              │
│    ├── async WebSocket handlers                  │
│    ├── NiceGUI page rendering                    │
│    └── ui.timer callbacks (status polling)       │
│                                                  │
│  RelayEngine threads (managed by sp-rtk-base-relay): │
│    ├── Input thread (serial/TCP reader)          │
│    ├── Broadcast thread (RTCM router)            │
│    └── Destination threads × N                   │
│                                                  │
│  Event Bridge (daemon thread):                   │
│    └── EventSubscription.get_event() loop        │
│        → asyncio queue → WebSocket push          │
└─────────────────────────────────────────────────┘
```

**Bridging async ↔ threaded:**
- `asyncio.to_thread()` for calling RelayEngine methods from async handlers
- Daemon thread for consuming EventSubscription (blocking) → asyncio.Queue (async)
- `ui.timer()` for periodic status polling in NiceGUI

## Component Relationships

### Dependency Graph
```
sp-base
  ├── sp-rtk-base-relay (>=2.1.0)  — RelayEngine, EventBus, config dataclasses
  ├── fastapi                    — REST API + WebSocket
  ├── nicegui                    — Browser UI (shares ASGI server with FastAPI)
  ├── uvicorn                    — ASGI server
  ├── pyyaml                     — Config persistence
  └── pydantic                   — API models (included with FastAPI)
```

### Internal Module Dependencies
```
main.py (entry point)
  ├── app.py (FastAPI app factory + NiceGUI init)
  │   ├── api/relay.py      → services/relay_service.py → sp_rtk_base_relay.RelayEngine
  │   ├── api/outputs.py    → services/relay_service.py + services/config_service.py
  │   ├── api/inputs.py     → services/config_service.py
  │   ├── api/events.py     → services/relay_service.py
  │   ├── api/websocket.py  → services/event_bridge.py
  │   └── ui/pages/
  │       ├── dashboard.py  → api endpoints (HTTP fetch / NiceGUI bindings)
  │       ├── outputs.py    → api endpoints
  │       └── settings.py   → api endpoints
  └── services/
      ├── relay_service.py   → sp_rtk_base_relay.RelayEngine (in-process)
      ├── config_service.py  → YAML file I/O
      └── event_bridge.py    → sp_rtk_base_relay.EventSubscription → asyncio.Queue
```

## Architecture Decisions (Phase 2+)

### DR-15: Device Info/Config Lives in sp-base
- **Decision**: All PyUBX2 interaction (device querying, GPS configuration) belongs in sp-base, NOT sp-rtk-base-relay
- **Rationale**: sp-rtk-base-relay is a pure RTCM relay. Adding PyUBX2 would add unnecessary complexity and an unrelated dependency.
- **Impact**: sp-rtk-base-relay stays focused; sp-base owns all u-blox device management

### DR-16: Two-Port Architecture Support
- **Separate ports** (e.g., FTDI UART for UBX, Bluetooth for RTCM): No relay interruption needed for device queries/config
- **Shared port** (single USB/UART for both): Must use serial port handoff (stop relay → UBX session → restart)
- **Impact**: sp-base must detect which configuration is in use and adapt behavior accordingly

#### Config A: Separate Ports (Preferred — No Relay Interruption)
```
UBX Config Port: /dev/ttyUSB0 (FTDI FT232 @ 57600)  →  PyUBX2 (anytime)
RTCM Relay Port: Bluetooth serial                     →  sp-rtk-base-relay (continuous)
```
- sp-base can query/configure GPS at any time without stopping relay
- Relay runs uninterrupted on a dedicated RTCM output port
- This is the current test hardware configuration

#### Config B: Shared Port (Requires Serial Handoff)
```
Shared Port: /dev/ttyACM0 @ 115200  →  Either PyUBX2 OR sp-rtk-base-relay
```
- sp-base must: `engine.stop()` → PyUBX2 session → `engine.start()`
- RelayEngine.stop() is synchronous, releases port immediately

### DR-17: Serial Port Handoff Pattern
- Already implemented in RelayEngine (`engine.stop()` is synchronous, releases port immediately)
- Documented in API spec with code examples
- sp-base uses this for shared-port configurations

```
Relay stopped  → sp-base owns serial port (PyUBX2 for GPS config)
Relay running  → sp-rtk-base-relay owns serial port (data relay)
```

## Phase 2+ Startup Flow

When GPS device configuration is added, the sp-base startup sequence will be:

```
1. Query device info (UBX-MON-VER) — identify GPS module
2. Check if relay port is different from UBX port
   → Separate: start relay immediately, query device anytime
   → Shared: defer relay until after device config
3. Check base station configuration
4. Offer survey backup if configured
5. Start relay with user-configured destinations
```

## Error Handling Strategy

| Error Source | Handling |
|---|---|
| Invalid destination config | ConfigurationError → 400 response with details |
| Engine already running | ServiceError → 409 Conflict response |
| Destination not found | KeyError → 404 response |
| Serial port unavailable | ConnectionError → 503 with retry guidance |
| WebSocket disconnect | Clean close, auto-reconnect on client side |
| Relay engine crash | EventBridge detects, updates UI status |
