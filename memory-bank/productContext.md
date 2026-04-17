# Product Context

## Why SP-Base Exists

RTK GPS base station operators need a simple way to configure, control, and monitor their RTCM correction data relay without SSH-ing into a device and editing YAML files. SP-Base provides a browser-based operator console that wraps the sp-base-relay engine with an intuitive web interface.

## Problems It Solves

### 1. No Operator-Friendly Interface for Relay Control
- **Problem**: sp-base-relay v2.1 has a powerful RelayEngine API, but the only interface is CLI + YAML config files. Operators must SSH into the device, edit YAML, and restart the service to make changes.
- **Solution**: Web UI with point-and-click destination management, start/stop controls, and live status.
- **Impact**: Non-technical operators can manage the base station from any device with a browser.

### 2. No Real-Time Visibility
- **Problem**: Operators can't easily see if destinations are connected, if data is flowing, or if errors are occurring without checking logs or Prometheus metrics.
- **Solution**: Live dashboard with per-destination status cards, throughput metrics, and a real-time event log (WebSocket-fed).
- **Impact**: Immediate situational awareness — operators see problems as they happen.

### 3. Dynamic Destination Management
- **Problem**: Adding or removing a destination (e.g., a new NTRIP caster) requires editing config and restarting the service, which interrupts all active connections.
- **Solution**: Hot add/remove/start/stop destinations via the web UI using sp-base-relay v2.1's dynamic destination management.
- **Impact**: Zero-downtime changes to the destination list.

### 4. GPS Device Configuration (Phase 2+)
- **Problem**: Configuring a u-blox receiver for base station mode requires u-center (Windows-only GUI) or low-level UBX protocol knowledge.
- **Solution**: Web-based configuration wizard for survey-in mode, fixed base mode, and RTCM message selection (via PyUBX2).
- **Impact**: Cross-platform GPS configuration from any browser.

### 5. GPS Device Management (Phase 2+)
- **Problem**: Base station operators need to identify, configure, and monitor their u-blox GPS receiver (survey-in, RTCM output settings, base station mode) but currently rely on u-center (Windows-only) or manual UBX commands.
- **Solution**: sp-base provides a web-based interface for device identification (UBX-MON-VER), configuration, and backup/restore using PyUBX2, with smart port handling (separate UBX + RTCM ports = no relay interruption; shared port = serial handoff via `engine.stop()`/`engine.start()`).
- **Impact**: Complete browser-based management of the GPS base station — from initial configuration through ongoing monitoring — without needing Windows or u-center.

## User Experience Goals

### For Base Station Operators
- **Quick Start**: Open browser → see status → start/stop relay in one click
- **Self-Service**: Add NTRIP casters, configure SurePath connections without editing files
- **Confidence**: Live status indicators show data is flowing and connections are healthy
- **Low Barrier**: No SSH, no YAML editing, no command-line knowledge required

### For Field Technicians (Phase 2+)
- **Setup Wizard**: Walk through base station configuration step by step
- **Survey-In Monitoring**: Watch survey-in progress with live accuracy and duration
- **One-Stop Setup**: Configure GPS → select RTCM messages → add destinations → start relay

### For System Administrators
- **API-First**: REST API enables automation and integration with other tools
- **Familiar Patterns**: FastAPI + Pydantic — standard Python web patterns
- **Observable**: Prometheus metrics from sp-base-relay still available for monitoring stacks

## How It Should Work

### Phase 1 Operator Workflow
```
1. Open browser → navigate to http://base-station:8080
2. Settings page: select serial port /dev/ttyACM0, set baud rate
3. Outputs page: add SurePath destination, add RTK2go destination
4. Dashboard: click "Start Relay"
5. Dashboard: see live status — input connected, destinations connected, bytes flowing
6. Event log: "relay started", "surepath connected", "rtk2go connected"
7. Runtime: hot-add Onocoy destination → no interruption to existing streams
8. Runtime: pause RTK2go → surepath + onocoy continue
9. Stop relay when done
```

### Architecture Principle
The browser is an **operator console**, not the source of truth. Authoritative runtime state lives in:
- **sp-base-relay RelayEngine** for relay state
- **sp-base backend services** for configuration and orchestration

The browser requests actions and renders status — it does NOT directly manage threads, sockets, or serial ports.
