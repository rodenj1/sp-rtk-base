# SP-Base-Relay v2.1 — Relay Engine API Technical Specification

**Version**: 2.1.0  
**Date**: March 2026  
**Audience**: GPS Base Station Web UI (gps-webui) developers  
**Package**: `sp-base-relay`

---

## Table of Contents

1. [Installation & Imports](#1-installation--imports)
2. [Configuration Objects](#2-configuration-objects)
3. [RelayEngine API Reference](#3-relayengine-api-reference)
4. [Event System](#4-event-system)
5. [Status Snapshots](#5-status-snapshots)
6. [Serial Port Handoff Pattern](#6-serial-port-handoff-pattern)
7. [Error Handling](#7-error-handling)
8. [Threading Model & Safety](#8-threading-model--safety)
9. [Complete Integration Examples](#9-complete-integration-examples)

---

## 1. Installation & Imports

### Install as dependency

```bash
uv add sp-base-relay          # or: pip install sp-base-relay
```

### Primary imports (public API)

```python
# The engine facade — this is the main entry point
from sp_base_relay import RelayEngine

# Event system
from sp_base_relay import EventBus, EventSubscription, RelayEvent

# Status snapshots
from sp_base_relay import RelayStatus, DestinationStatus, InputStatus

# Configuration dataclasses (build programmatically — no YAML needed)
from sp_base_relay.config import (
    InputConfig,
    DestinationConfig,
    DestinationFilterConfig,
    SurePathDestinationConfig,
    NtripDestinationConfig,
    TcpServerDestinationConfig,
)

# Exceptions
from sp_base_relay.exceptions import (
    ServiceError,
    ConfigurationError,
)
```

### Package version

```python
from sp_base_relay import __version__  # "2.1.0"
```

---

## 2. Configuration Objects

All configuration is done by constructing Python dataclass instances. **No YAML file is required for embedded use.**

### 2.1 InputConfig

Defines the RTCM input source (serial port, TCP, or Bluetooth).

```python
@dataclass
class InputConfig:
    source: str               # One of: "tcp", "serial", "usb_serial", "bluetooth"
    config: dict[str, Any]    # Source-specific key-value configuration
```

#### Serial Input (most common for GPS base stations)

```python
input_config = InputConfig(
    source="serial",
    config={
        "port": "/dev/ttyACM0",       # Required — serial device path
        "baudrate": 115200,            # Default: 115200
        "bytesize": 8,                # Default: 8
        "parity": "N",                # Default: "N"
        "stopbits": 1,                # Default: 1
        "timeout": 1.0,               # Default: 1.0
        "rtscts": False,              # Default: False
        "xonxoff": False,             # Default: False
    },
)
```

#### TCP Input (e.g., RTKBase str2str)

```python
input_config = InputConfig(
    source="tcp",
    config={
        "host": "127.0.0.1",          # Required
        "port": 5015,                 # Required
        "timeout": 5.0,               # Default: 5.0
        "buffer_size": 4096,           # Default: 4096
    },
)
```

#### Bluetooth Input

```python
input_config = InputConfig(
    source="bluetooth",
    config={
        "address": "AA:BB:CC:DD:EE:FF",  # Bluetooth MAC address
        "channel": 1,                      # RFCOMM channel
    },
)
```

> **⚠️ Important — Serial Port Exclusivity**: Only one process can open a serial port at a time. When `RelayEngine` is running, it owns the serial port. You must call `engine.stop()` before using PyUBX2 for device configuration. See [Section 6](#6-serial-port-handoff-pattern).

### 2.2 DestinationConfig

Each destination requires a `DestinationConfig` wrapping a type-specific config object.

```python
@dataclass
class DestinationConfig:
    name: str                          # Unique name (alphanumeric + hyphens/underscores)
    type: str                          # One of: "surepath", "ntrip", "tcp_server"
    enabled: bool                      # Whether destination is active
    filter: DestinationFilterConfig    # Message filtering rules
    config: DestinationSpecificConfig  # Type-specific config (see below)
```

### 2.3 DestinationFilterConfig

Controls which RTCM message types are forwarded to a destination.

```python
@dataclass
class DestinationFilterConfig:
    mode: str = "pass_all"        # "pass_all" | "allowlist" | "blocklist"
    message_ids: list[int] = []   # RTCM message type IDs (e.g., [1005, 1077, 1087])
```

| Mode | Behavior |
|---|---|
| `"pass_all"` | Forward all data unchanged (no RTCM parsing — best performance) |
| `"allowlist"` | Only forward RTCM messages with IDs in `message_ids` |
| `"blocklist"` | Forward everything EXCEPT messages with IDs in `message_ids` |

### 2.4 SurePathDestinationConfig

```python
@dataclass
class SurePathDestinationConfig:
    host: str                           # Required — server hostname/IP
    port: int = 50010                   # Server port
    username: str                       # Required — auth username
    password: str                       # Required — auth password
    connection_timeout: int = 10        # Seconds
    read_timeout: int = 30              # Seconds
    heartbeat_timeout: int = 30         # Seconds
    retry_initial_delay: int = 15       # Initial retry delay (seconds)
    retry_max_delay: int = 60           # Maximum retry delay (seconds)
    retry_multiplier: float = 2.0       # Exponential backoff multiplier
```

**Example:**
```python
dest_surepath = DestinationConfig(
    name="surepath",
    type="surepath",
    enabled=True,
    filter=DestinationFilterConfig(mode="pass_all"),
    config=SurePathDestinationConfig(
        host="surepath.example.com",
        port=50010,
        username="myuser",
        password="mypass",
    ),
)
```

### 2.5 NtripDestinationConfig

```python
@dataclass
class NtripDestinationConfig:
    caster: str                         # Required — caster hostname (e.g., "rtk2go.com")
    port: int = 2101                    # Caster port
    mountpoint: str                     # Required — mount point name
    password: str                       # Required — stream password
    username: str = ""                  # Username (often empty for NTRIP)
    version: str = "2.0"               # "1.0" or "2.0"
    connection_timeout: int = 15        # Seconds
    retry_initial_delay: int = 10       # Initial retry delay (seconds)
    retry_max_delay: int = 120          # Maximum retry delay (seconds)
    retry_multiplier: float = 2.0       # Exponential backoff multiplier
```

**Example:**
```python
dest_rtk2go = DestinationConfig(
    name="rtk2go",
    type="ntrip",
    enabled=True,
    filter=DestinationFilterConfig(mode="pass_all"),
    config=NtripDestinationConfig(
        caster="rtk2go.com",
        port=2101,
        mountpoint="MY_MOUNT",
        password="mypassword",
    ),
)
```

### 2.6 TcpServerDestinationConfig

Starts a local TCP server that clients can connect to for RTCM data.

```python
@dataclass
class TcpServerDestinationConfig:
    host: str = "0.0.0.0"              # Bind address
    port: int = 5016                   # Listen port
    max_clients: int = 10              # Maximum simultaneous client connections
```

**Example:**
```python
dest_tcp = DestinationConfig(
    name="local-tcp",
    type="tcp_server",
    enabled=True,
    filter=DestinationFilterConfig(mode="pass_all"),
    config=TcpServerDestinationConfig(
        host="0.0.0.0",
        port=5016,
        max_clients=10,
    ),
)
```

---

## 3. RelayEngine API Reference

`RelayEngine` is the **single entry point** for all relay operations. Import it from the package root.

```python
from sp_base_relay import RelayEngine
```

### 3.1 Constructor

```python
engine = RelayEngine(input_config: InputConfig)
```

Creates a **stopped** engine. Does NOT open the serial port or connect to anything yet.

- The `EventBus` is created immediately and available via `engine.event_bus`.
- You can `subscribe_events()` before calling `start()` to capture startup events.

### 3.2 Lifecycle Methods

#### `engine.start(destinations=None)`

```python
def start(self, destinations: list[DestinationConfig] | None = None) -> None
```

Starts the relay:
1. Creates the input source (opens serial port / connects TCP)
2. Creates destination objects from configs
3. Starts the BroadcastHub (begins reading input and routing to destinations)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `destinations` | `list[DestinationConfig] \| None` | `None` | Initial destinations. Pass `None` or `[]` to start with no destinations (add them later). |

**Raises:**
- `ServiceError` — if engine is already running
- `ConfigurationError` — if a destination config is invalid

**Events emitted:** `engine.started`

#### `engine.stop()`

```python
def stop(self) -> None
```

Gracefully stops everything:
1. Stops all destination threads
2. Stops the BroadcastHub
3. Disconnects and releases the input source (serial port)

Safe to call when already stopped (no-op). **The engine can be restarted** after stopping.

**Events emitted:** `engine.stopped`

#### `engine.is_running` (property)

```python
@property
def is_running(self) -> bool
```

Returns `True` if the engine is actively relaying data.

### 3.3 Destination Management (Hot Add/Remove/Start/Stop)

All destination management methods require the engine to be running. They raise `ServiceError` if called on a stopped engine.

#### `engine.add_destination(config) -> str`

```python
def add_destination(self, config: DestinationConfig) -> str
```

Hot-adds a new destination while the relay is running. The destination starts receiving data immediately.

**Returns:** The destination name (string).

**Raises:**
- `ServiceError` — engine not running
- `ConfigurationError` — invalid config or duplicate name

**Events emitted:** `destination.added`

#### `engine.remove_destination(name)`

```python
def remove_destination(self, name: str) -> None
```

Stops and removes a destination. It will no longer receive data.

**Raises:**
- `ServiceError` — engine not running
- `KeyError` — destination name not found

**Events emitted:** `destination.removed`

#### `engine.stop_destination(name)`

```python
def stop_destination(self, name: str) -> None
```

Pauses a destination (keeps it registered, sets `enabled=False`, stops its thread). Data is NOT queued while stopped.

**Raises:**
- `ServiceError` — engine not running
- `KeyError` — destination name not found

**Events emitted:** `destination.stopped`

#### `engine.start_destination(name)`

```python
def start_destination(self, name: str) -> None
```

Resumes a previously stopped destination (sets `enabled=True`, restarts thread).

**Raises:**
- `ServiceError` — engine not running
- `KeyError` — destination name not found

**Events emitted:** `destination.started`

#### `engine.get_destination_names() -> list[str]`

```python
def get_destination_names(self) -> list[str]
```

Returns names of all registered destinations. Returns `[]` if engine not running.

### 3.4 Status & Events

#### `engine.get_status() -> RelayStatus`

```python
def get_status(self) -> RelayStatus
```

Returns a **frozen** (immutable) snapshot of the entire relay system. Safe to pass between threads, serialize to JSON, etc. See [Section 5](#5-status-snapshots) for field details.

**Raises:** `ServiceError` — engine not running

#### `engine.subscribe_events() -> EventSubscription`

```python
def subscribe_events(self) -> EventSubscription
```

Creates a new event subscription. Can be called **before or after** `start()`. See [Section 4](#4-event-system) for consumption patterns.

**Returns:** An `EventSubscription` — call `.close()` when done.

#### `engine.get_recent_events(count=50) -> list[RelayEvent]`

```python
def get_recent_events(self, count: int = 50) -> list[RelayEvent]
```

Returns up to `count` recent events from the ring buffer (oldest first). Works even when engine is stopped (returns events from before stop).

#### `engine.event_bus` (property)

```python
@property
def event_bus(self) -> EventBus
```

Direct access to the EventBus. Advanced usage — prefer `subscribe_events()` and `get_recent_events()`.

---

## 4. Event System

### 4.1 RelayEvent

Every event is an immutable `RelayEvent` frozen dataclass:

```python
@dataclass(frozen=True)
class RelayEvent:
    event_type: str              # Dot-notation category (e.g., "destination.connected")
    message: str                 # Human-readable description
    timestamp: float             # time.time() epoch
    payload: dict[str, Any]      # Event-specific context
```

### 4.2 Event Types

| Constant | Value | When Emitted | Typical Payload |
|---|---|---|---|
| `ENGINE_STARTED` | `"engine.started"` | `engine.start()` completes | `destination_count`, `input_source` |
| `ENGINE_STOPPED` | `"engine.stopped"` | `engine.stop()` completes | — |
| `HUB_STARTED` | `"hub.started"` | BroadcastHub starts | — |
| `HUB_STOPPED` | `"hub.stopped"` | BroadcastHub stops | — |
| `INPUT_CONNECTED` | `"input.connected"` | Input source connects | — |
| `INPUT_DISCONNECTED` | `"input.disconnected"` | Input source disconnects | — |
| `INPUT_RECONNECTING` | `"input.reconnecting"` | Reconnection attempt begins | — |
| `INPUT_RECONNECTED` | `"input.reconnected"` | Reconnection succeeds | — |
| `INPUT_NO_DATA_WARNING` | `"input.no_data_warning"` | No data received for configured timeout | — |
| `DESTINATION_ADDED` | `"destination.added"` | Hot-add via `add_destination()` | `name` |
| `DESTINATION_REMOVED` | `"destination.removed"` | Hot-remove via `remove_destination()` | `name` |
| `DESTINATION_STARTED` | `"destination.started"` | Destination thread starts | `name` |
| `DESTINATION_STOPPED` | `"destination.stopped"` | Destination thread stops | `name` |
| `DESTINATION_CONNECTED` | `"destination.connected"` | Destination connects to remote | `name` |
| `DESTINATION_DISCONNECTED` | `"destination.disconnected"` | Destination loses connection | `name` |
| `DESTINATION_ERROR` | `"destination.error"` | Destination encounters an error | `name`, `error` |
| `DESTINATION_RECONNECTING` | `"destination.reconnecting"` | Reconnection attempt | `name` |
| `DESTINATION_RECONNECTED` | `"destination.reconnected"` | Reconnection succeeds | `name` |

Import constants from:
```python
from sp_base_relay.core.events import (
    ENGINE_STARTED, ENGINE_STOPPED,
    HUB_STARTED, HUB_STOPPED,
    INPUT_CONNECTED, INPUT_DISCONNECTED, INPUT_NO_DATA_WARNING,
    DESTINATION_ADDED, DESTINATION_REMOVED,
    DESTINATION_CONNECTED, DESTINATION_DISCONNECTED,
    DESTINATION_ERROR,
)
```

### 4.3 EventSubscription — Consumption Patterns

#### Pattern 1: Polling (recommended for UI)

```python
sub = engine.subscribe_events()

# In a periodic timer or asyncio task:
event = sub.get_event(timeout=0.1)  # Non-blocking with short timeout
if event is not None:
    update_ui(event)
```

#### Pattern 2: Drain all pending (batch processing)

```python
events = sub.drain()  # Returns list[RelayEvent], never blocks
for event in events:
    process(event)
```

#### Pattern 3: Context manager (auto-close)

```python
with engine.subscribe_events() as sub:
    event = sub.get_event(timeout=5.0)
    # subscription auto-closes on exit
```

#### Pattern 4: Background thread iteration

```python
import threading

def event_listener(sub: EventSubscription):
    for event in sub:  # Blocks until event or subscription closed
        handle_event(event)

sub = engine.subscribe_events()
t = threading.Thread(target=event_listener, args=(sub,), daemon=True)
t.start()

# Later, to stop:
sub.close()  # Breaks the iteration
```

#### Subscription lifecycle

- `sub.is_closed` — check if closed
- `sub.pending_count` — events waiting in queue
- `sub.close()` — safe to call multiple times
- Queue size: 500 events per subscriber (events dropped if consumer is too slow)

### 4.4 Ring Buffer

The EventBus maintains a ring buffer of the last 200 events. Use `engine.get_recent_events(count)` to retrieve them — useful for showing recent activity when a UI page first loads.

---

## 5. Status Snapshots

### 5.1 RelayStatus

Returned by `engine.get_status()`. All fields are **read-only** (frozen dataclass).

```python
@dataclass(frozen=True)
class RelayStatus:
    running: bool                              # Is the hub actively running?
    uptime_seconds: float | None               # Seconds since hub started, or None
    input: InputStatus                         # Input source snapshot
    destinations: list[DestinationStatus]      # Per-destination snapshots
    active_destination_count: int              # Destinations currently connected
    total_destination_count: int               # Total registered destinations
    bytes_received: int                        # Total bytes from input (hub-level)
    chunks_distributed: int                    # Total data chunks sent to destinations
    frames_parsed: int                         # Total RTCM frames parsed
    no_data_warnings: int                      # No-data warning count
```

### 5.2 InputStatus

```python
@dataclass(frozen=True)
class InputStatus:
    connected: bool                            # Is input source connected?
    source_type: str                           # "serial", "tcp", "bluetooth"
    bytes_received: int                        # Total bytes read
    messages_received: int                     # Total read operations with data
    seconds_since_last_data: float             # Seconds since last data (-1.0 if never)
    reconnect_attempts: int                    # Total connection attempts
    reconnect_successes: int                   # Total successful connections
    connected_since: float | None              # Epoch timestamp or None
```

### 5.3 DestinationStatus

```python
@dataclass(frozen=True)
class DestinationStatus:
    name: str                                  # Unique destination name
    destination_type: str                      # "surepath", "ntrip", "tcp_server"
    enabled: bool                              # Is destination enabled?
    running: bool                              # Is destination thread running?
    connected: bool                            # Has active connection?
    filter_mode: str                           # "all", "parsed", "raw"
    bytes_sent: int                            # Total bytes sent
    messages_sent: int                         # Total messages sent
    messages_dropped: int                      # Messages dropped (queue full, etc.)
    messages_filtered: int                     # Messages filtered out
    errors: int                                # Total error count
    last_error: str | None                     # Most recent error message
    queue_depth: int                           # Items waiting in send queue
    connected_since: float | None              # Epoch timestamp or None
    uptime_seconds: float | None               # Connection uptime or None
    connection_attempts: int                   # Total connection attempts
    successful_connections: int                # Total successful connections
```

### 5.4 Recommended Polling Pattern for UI

```python
import asyncio

async def status_poller(engine: RelayEngine):
    """Poll relay status every 2 seconds and push to UI."""
    while engine.is_running:
        status = engine.get_status()
        
        # Update UI dashboard
        await update_dashboard(
            input_connected=status.input.connected,
            bytes_in=status.input.bytes_received,
            seconds_since_data=status.input.seconds_since_last_data,
            destinations=[
                {
                    "name": d.name,
                    "type": d.destination_type,
                    "connected": d.connected,
                    "enabled": d.enabled,
                    "bytes_sent": d.bytes_sent,
                    "errors": d.errors,
                    "last_error": d.last_error,
                    "queue_depth": d.queue_depth,
                }
                for d in status.destinations
            ],
        )
        
        await asyncio.sleep(2.0)
```

---

## 6. Serial Port Handoff Pattern

The GPS receiver's serial port can only be opened by **one process at a time**. When the relay owns it, PyUBX2 cannot configure the device, and vice versa.

### Protocol

```
                 ┌─────────────────────┐
                 │   gps-webui owns    │
                 │   serial port       │
                 │   (PyUBX2 config)   │
                 └──────────┬──────────┘
                            │
                   engine.start()
                            │
                 ┌──────────▼──────────┐
                 │   sp-base-relay     │
                 │   owns serial port  │
                 │   (relay running)   │
                 └──────────┬──────────┘
                            │
                   engine.stop()
                            │
                 ┌──────────▼──────────┐
                 │   gps-webui owns    │
                 │   serial port       │
                 │   (PyUBX2 config)   │
                 └─────────────────────┘
```

### Implementation

```python
# User clicks "Configure GPS" in web UI
engine.stop()                      # Releases serial port
# ... wait for engine.is_running == False ...
configure_gps_with_pyubx2(port)    # PyUBX2 opens the port

# User clicks "Start Relay" in web UI
close_pyubx2_connection()          # Release port first!
engine.start(destinations)         # Re-opens serial port
```

> **⚠️ Critical**: Always verify the engine has fully stopped (check `engine.is_running == False`) before attempting to open the serial port with PyUBX2. The `engine.stop()` call is synchronous — when it returns, the port is released.

---

## 7. Error Handling

### 7.1 Exception Hierarchy

```
SPBaseRelayError (base)
├── ConfigurationError      # Invalid config values, duplicate destination names
├── ConnectionError         # Network/serial connection failures
├── AuthenticationError     # Auth failures (SurePath, NTRIP)
├── InputSourceError        # Input source creation/read failures
├── DataProcessingError     # RTCM parsing failures
├── ServiceError            # Engine lifecycle errors (start when running, etc.)
└── DestinationError        # Destination-specific errors
    └── NtripError          # NTRIP protocol errors
```

### 7.2 What Each Method Can Raise

| Method | Exceptions |
|---|---|
| `RelayEngine(input_config)` | `ConfigurationError` (invalid input config) |
| `engine.start(dests)` | `ServiceError` (already running), `ConfigurationError` (bad dest config) |
| `engine.stop()` | None (always safe) |
| `engine.add_destination(config)` | `ServiceError` (not running), `ConfigurationError` (invalid/duplicate) |
| `engine.remove_destination(name)` | `ServiceError` (not running), `KeyError` (not found) |
| `engine.start_destination(name)` | `ServiceError` (not running), `KeyError` (not found) |
| `engine.stop_destination(name)` | `ServiceError` (not running), `KeyError` (not found) |
| `engine.get_status()` | `ServiceError` (not running) |
| `engine.subscribe_events()` | None (always safe, works before start) |
| `engine.get_recent_events()` | None (always safe) |
| `engine.get_destination_names()` | None (returns `[]` if not running) |

### 7.3 Recommended Error Handling Pattern

```python
from sp_base_relay.exceptions import ServiceError, ConfigurationError

try:
    engine.start(destinations)
except ConfigurationError as e:
    show_error(f"Configuration error: {e}")
except ServiceError as e:
    show_error(f"Service error: {e}")

# Destination management
try:
    engine.add_destination(new_dest_config)
except ConfigurationError as e:
    show_error(f"Invalid destination: {e}")  # e.g., duplicate name
except ServiceError:
    show_error("Engine must be running to add destinations")
```

---

## 8. Threading Model & Safety

### 8.1 Internal Threading

When `engine.start()` is called, these threads are created:

| Thread | Purpose | Lifetime |
|---|---|---|
| Input Thread | Reads data from serial/TCP/BT source | engine.start → engine.stop |
| Broadcast Thread | Parses RTCM frames, distributes to queues | engine.start → engine.stop |
| Destination Thread × N | One per destination, sends data to remote | Per-destination lifecycle |

### 8.2 Thread Safety Guarantees

| Operation | Thread-Safe? | Notes |
|---|---|---|
| `engine.start()` / `engine.stop()` | Call from main thread | Not designed for concurrent calls |
| `engine.add_destination()` | ✅ Yes | Lock-protected destination list |
| `engine.remove_destination()` | ✅ Yes | Lock-protected destination list |
| `engine.start_destination()` | ✅ Yes | Lock-protected |
| `engine.stop_destination()` | ✅ Yes | Lock-protected |
| `engine.get_status()` | ✅ Yes | Returns frozen snapshot |
| `engine.get_destination_names()` | ✅ Yes | Lock-protected |
| `engine.subscribe_events()` | ✅ Yes | Lock-protected subscriber list |
| `engine.get_recent_events()` | ✅ Yes | deque is GIL-atomic |
| `EventSubscription.get_event()` | ✅ Yes | queue.Queue is thread-safe |
| `EventSubscription.drain()` | ✅ Yes | queue.Queue is thread-safe |
| `EventSubscription.close()` | ✅ Yes | Safe to call from any thread |

### 8.3 Recommended UI Architecture

```
┌──────────────────────────────────────────┐
│  gps-webui (FastAPI + NiceGUI)           │
│                                          │
│  Main Thread: FastAPI/NiceGUI event loop │
│       │                                  │
│       ├── RelayEngine (owns all relay    │
│       │   threads internally)            │
│       │                                  │
│       ├── Status Poller (async task)     │
│       │   └── engine.get_status() every  │
│       │       2 seconds                  │
│       │                                  │
│       └── Event Listener (daemon thread) │
│           └── sub.get_event() loop       │
│               → push to UI via websocket │
└──────────────────────────────────────────┘
```

---

## 9. Complete Integration Examples

### 9.1 Minimal — Start Relay with One Destination

```python
from sp_base_relay import RelayEngine
from sp_base_relay.config import (
    InputConfig,
    DestinationConfig,
    DestinationFilterConfig,
    NtripDestinationConfig,
)

# Build configuration programmatically
input_config = InputConfig(
    source="serial",
    config={"port": "/dev/ttyACM0", "baudrate": 115200},
)

dest_config = DestinationConfig(
    name="rtk2go",
    type="ntrip",
    enabled=True,
    filter=DestinationFilterConfig(mode="pass_all"),
    config=NtripDestinationConfig(
        caster="rtk2go.com",
        port=2101,
        mountpoint="MY_MOUNT",
        password="mypassword",
    ),
)

# Create and start
engine = RelayEngine(input_config)
engine.start([dest_config])

# ... relay is running ...

engine.stop()
```

### 9.2 Full UI Integration — Lifecycle + Events + Status

```python
import asyncio
import threading
from sp_base_relay import RelayEngine, EventSubscription, RelayEvent
from sp_base_relay.config import (
    InputConfig, DestinationConfig, DestinationFilterConfig,
    NtripDestinationConfig, TcpServerDestinationConfig,
)
from sp_base_relay.core.events import (
    DESTINATION_CONNECTED, DESTINATION_DISCONNECTED, DESTINATION_ERROR,
    INPUT_NO_DATA_WARNING, ENGINE_STARTED, ENGINE_STOPPED,
)
from sp_base_relay.exceptions import ServiceError, ConfigurationError


class RelayManager:
    """Manages the relay engine for the web UI."""

    def __init__(self, serial_port: str, baudrate: int = 115200) -> None:
        self.input_config = InputConfig(
            source="serial",
            config={"port": serial_port, "baudrate": baudrate},
        )
        self.engine = RelayEngine(self.input_config)
        self._event_sub: EventSubscription | None = None
        self._event_thread: threading.Thread | None = None

    def start(self, destination_configs: list[DestinationConfig]) -> None:
        """Start the relay with given destinations."""
        # Subscribe to events BEFORE start to capture startup events
        self._event_sub = self.engine.subscribe_events()
        self._event_thread = threading.Thread(
            target=self._event_loop,
            args=(self._event_sub,),
            daemon=True,
        )
        self._event_thread.start()

        self.engine.start(destination_configs)

    def stop(self) -> None:
        """Stop the relay and clean up."""
        self.engine.stop()
        if self._event_sub:
            self._event_sub.close()

    def add_destination(self, config: DestinationConfig) -> str:
        """Add a new destination while running."""
        return self.engine.add_destination(config)

    def remove_destination(self, name: str) -> None:
        """Remove a destination while running."""
        self.engine.remove_destination(name)

    def toggle_destination(self, name: str, enabled: bool) -> None:
        """Enable/disable a destination."""
        if enabled:
            self.engine.start_destination(name)
        else:
            self.engine.stop_destination(name)

    def get_dashboard_data(self) -> dict:
        """Get data for the UI dashboard."""
        if not self.engine.is_running:
            return {"running": False, "destinations": []}

        status = self.engine.get_status()
        return {
            "running": True,
            "uptime": status.uptime_seconds,
            "input": {
                "connected": status.input.connected,
                "type": status.input.source_type,
                "bytes": status.input.bytes_received,
                "last_data_age": status.input.seconds_since_last_data,
            },
            "destinations": [
                {
                    "name": d.name,
                    "type": d.destination_type,
                    "enabled": d.enabled,
                    "connected": d.connected,
                    "bytes_sent": d.bytes_sent,
                    "messages_sent": d.messages_sent,
                    "errors": d.errors,
                    "last_error": d.last_error,
                    "queue_depth": d.queue_depth,
                    "uptime": d.uptime_seconds,
                }
                for d in status.destinations
            ],
            "totals": {
                "bytes_received": status.bytes_received,
                "chunks_distributed": status.chunks_distributed,
                "active_destinations": status.active_destination_count,
                "total_destinations": status.total_destination_count,
            },
        }

    def get_event_log(self, count: int = 50) -> list[dict]:
        """Get recent events for the event log panel."""
        return [
            {
                "type": e.event_type,
                "message": e.message,
                "time": e.timestamp,
                "payload": e.payload,
            }
            for e in self.engine.get_recent_events(count)
        ]

    def _event_loop(self, sub: EventSubscription) -> None:
        """Background thread that processes relay events."""
        while not sub.is_closed:
            event = sub.get_event(timeout=1.0)
            if event is None:
                continue
            self._handle_event(event)

    def _handle_event(self, event: RelayEvent) -> None:
        """Route events to appropriate UI handlers."""
        match event.event_type:
            case "destination.connected":
                notify_ui(f"✅ {event.payload.get('name', '')} connected")
            case "destination.disconnected":
                notify_ui(f"⚠️ {event.payload.get('name', '')} disconnected")
            case "destination.error":
                notify_ui(f"❌ {event.payload.get('name', '')}: {event.payload.get('error', '')}")
            case "input.no_data_warning":
                notify_ui("⚠️ No GPS data received — check connection")
            case "engine.started":
                notify_ui("🟢 Relay engine started")
            case "engine.stopped":
                notify_ui("🔴 Relay engine stopped")
```

### 9.3 Serial Port Handoff with PyUBX2

```python
class GPSManager:
    """Coordinates relay engine and PyUBX2 device configuration."""

    def __init__(self, port: str) -> None:
        self.port = port
        self.relay = RelayManager(port)
        self._saved_destinations: list[DestinationConfig] = []

    def start_relay(self, destinations: list[DestinationConfig]) -> None:
        """Start relaying RTCM data."""
        self._saved_destinations = destinations
        self.relay.start(destinations)

    def stop_relay(self) -> None:
        """Stop relay — releases serial port."""
        self.relay.stop()

    def configure_gps(self, settings: dict) -> None:
        """Configure GPS via PyUBX2. Stops relay first if needed."""
        was_running = self.relay.engine.is_running
        
        if was_running:
            self.relay.stop()
            # Port is now released — engine.stop() is synchronous

        # Open port with PyUBX2 and configure
        import serial
        from pyubx2 import UBXReader, UBXMessage
        
        with serial.Serial(self.port, 115200) as ser:
            # ... apply u-blox configuration via UBX protocol ...
            pass

        # Restart relay if it was running before
        if was_running and self._saved_destinations:
            self.relay.start(self._saved_destinations)
```

---

## Appendix: Quick Reference Card

```
# Create engine
engine = RelayEngine(InputConfig(source="serial", config={...}))

# Lifecycle
engine.start([dest1, dest2])     # Start with destinations
engine.stop()                     # Stop everything, release serial port
engine.is_running                 # True/False

# Destinations (hot add/remove while running)
engine.add_destination(config)    # Returns name
engine.remove_destination("name") # Raises KeyError if not found
engine.stop_destination("name")   # Pause (keep registered)
engine.start_destination("name")  # Resume
engine.get_destination_names()    # ["surepath", "rtk2go", ...]

# Status (frozen snapshot)
status = engine.get_status()      # → RelayStatus
status.input.connected            # bool
status.destinations[0].bytes_sent # int

# Events (push notifications)
sub = engine.subscribe_events()   # Get subscription
event = sub.get_event(timeout=1)  # Poll for event
events = sub.drain()              # Get all pending
sub.close()                       # Unsubscribe

# Recent events (ring buffer)
events = engine.get_recent_events(50)  # Last 50 events
```
