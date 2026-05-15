# SP-Base Project Brief

## Project Overview

SP-Base is a Python web UI and API for controlling and monitoring a u-blox GPS device used as an RTK base station. It provides an operator console for configuring RTCM correction data relay, managing output destinations, and monitoring real-time system status.

The package depends on `sp-rtk-base-relay` (installed via PyPI) as its relay engine, and adds a browser-based control plane on top.

## Core Requirements

### Phase 1: Relay Control & Monitoring (Current Focus)
- Configure RTCM input source (serial, TCP, Bluetooth)
- Configure output destinations (SurePath, NTRIP casters, TCP server)
- Start/stop the relay engine
- Hot add/remove/start/stop individual destinations
- Live status dashboard (input status, per-destination metrics, throughput)
- Real-time event log (WebSocket-fed)
- Persist destination profiles and input config (YAML)

### Phase 2: GPS Device Configuration (Future)
- Connect to u-blox GPS receiver via serial port (PyUBX2)
- Configure survey-in mode (duration, accuracy target)
- Configure fixed base mode (known coordinates)
- Select RTCM output messages
- Serial port handoff (stop relay → configure GPS → restart relay)

### Phase 3: Advanced GPS Configuration (Future)
- Device info display (model, firmware, protocol version)
- Config backup/restore (via PyUBXUtils)
- Live position display (NAV-PVT: lat/lon/alt, fix type, satellite count)
- Survey-in progress visualization (NAV-SVIN)

### Phase 4: Full GPS Setup from Scratch (Future)
- Initial device setup wizard
- GNSS constellation selection
- Navigation rate configuration
- Power management settings
- Save configuration to device flash

## Project Goals

1. **Primary Goal (Phase 1)**: Provide a web-based operator console for configuring, starting, stopping, and monitoring the sp-rtk-base-relay RTCM relay engine
2. **Primary Goal (Phase 2)**: Enable u-blox GPS device configuration via the web UI (survey-in, fixed base, RTCM message selection)
3. **Integration Goal**: Use sp-rtk-base-relay v2.1 RelayEngine API as an in-process Python dependency
4. **Operational Goal**: Provide real-time status visibility with live event streaming
5. **Development Goal**: Maintain >90% unit test coverage following Python 3.10+ standards

## Success Criteria

### Phase 1
- Operator can configure input source and destinations via web UI
- Operator can start/stop relay and individual destinations
- Live dashboard shows input status, per-destination metrics, errors
- Event log shows real-time relay events via WebSocket
- Destination profiles persist across restarts (YAML)
- >90% unit test coverage
- Zero pylance/pyright issues in strict mode

## Target Users
- RTK GPS base station operators
- GNSS professionals managing correction data relay
- Field technicians setting up base stations

## Technical Constraints
- Python 3.10+ with type hints and PEP8 standards
- UV package management framework
- >90% unit test coverage using Pytest
- Resolve all pylance/pyright linting issues (strict mode)
- sp-rtk-base-relay as a PyPI dependency (not vendored)
- Single-process deployment (FastAPI + NiceGUI + RelayEngine in one process)
