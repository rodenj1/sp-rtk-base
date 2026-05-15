# Progress

## Recent Changes

### 2026-05-14 — Relay Package Rename `sp-base-relay` → `sp-rtk-base-relay`
The embedded relay-engine package was renamed; all sp-base references updated:
- 7 src files, 5 test files, 4 docs, 6 memory-bank files
- Prometheus gauges `sp_base_relay_running` / `sp_base_relay_uptime_seconds` renamed to `sp_rtk_base_relay_*` (literal, no namespace prefix). Other gauges (`sp_base_input_*`, `sp_base_dest_*`) keep configurable `namespace`.
- **Operator action required**: rename Grafana/PromQL queries referencing the old metric names.
- Verified: `uv sync` clean, **480 unit tests pass**, **pyright 0 errors**, grep confirms zero remaining `sp_base_relay`/`sp-base-relay` references outside `packages/`.

## Completed Phases

### Phase 1 — Project Scaffold ✅
- FastAPI + NiceGUI application structure
- UV package management with pyproject.toml
- Basic app entry point, health endpoint, dark-themed UI layout
- Initial unit tests for app and main

### Phase 2 — Service Layer ✅
- `ConfigService`: YAML-based config persistence (~/.config/sp-base/config.yaml)
- `RelayService`: Wraps sp-rtk-base-relay engine with async start/stop/status
- `EventBridge`: Subscribes to relay engine events, maintains event buffer
- Pydantic config models (AppConfig, InputProfile, DestinationProfile, AppSettings)
- Full unit tests for all services and models

### Phase 3 — REST API Endpoints ✅
- `POST /api/destinations` — create (201/409)
- `GET /api/destinations` — list all
- `GET /api/destinations/{name}` — get single (200/404)
- `PUT /api/destinations/{name}` — update (200/404)
- `DELETE /api/destinations/{name}` — delete (200/404)
- `GET /api/settings` — get app settings
- `PUT /api/settings` — update app settings
- `GET /api/settings/input` — get input config
- `PUT /api/settings/input` / `PUT /api/input` — update input config
- `POST /api/relay/start` — start relay (200/400/409)
- `POST /api/relay/stop` — stop relay (200/409)
- `GET /api/relay/status` — relay status
- `GET /api/events` — poll recent events
- `WS /api/events/ws` — WebSocket event stream
- Full unit tests for all API endpoints

### Phase 4 — NiceGUI Browser Pages ✅
- Dashboard: relay status, start/stop, input metrics, throughput, event log
- Outputs: destination list, add/edit/delete dialogs, enable/disable toggle
- Settings: input source config (TCP/serial/Bluetooth), application settings
- Shared layout with navigation drawer

### Phase 5 — Integration & Observability ✅
- End-to-end integration tests (TCP simulator → relay → TCP server dest → test client)
- `MetricsService` with prometheus_client gauges
- `GET /metrics` Prometheus endpoint
- `tools/demo_with_simulator.py` demo script
- Lifecycle tests (start/stop, error cases)

### Phase 6 — Polish & Documentation ✅
- **README.md**: Quick-start, API table, Prometheus guide, project structure
- **Metrics toggle**: `metrics_enabled` in AppSettings, 404 when disabled
- **Form validation**: Required fields, port range (1-65535), numeric validation on Settings & Outputs pages
- **Error handling**: Dashboard error banner, per-destination errors/dropped, no-data warnings, try/except on all save operations
- **Responsive layout**: Mobile-first CSS (viewport meta, 44px touch targets, stacked cards on mobile, 2-column on tablet, overlay drawer)
- **Integration tests**: 11 destination management tests (CRUD + hot add/remove/toggle while running)

### Phase 6b — Full Hardening Pass ✅
- **A1 — Shared validators**: Extracted `ui/validators.py` with `is_non_empty`, `is_valid_port`, `is_numeric`, factory functions `required()`, `port_validation()`, `numeric_validation()`
- **A2 — Auto-start relay**: `init_services()` auto-start path fully tested (5 cases), `services/__init__.py` → 100%
- **A3 — Coverage push**: 167 → 220 tests, 59.4% → 63.16% overall coverage
- **A4 — Config import/export API**: `GET /api/config/export` (YAML download), `POST /api/config/import` (YAML upload + validation), 9 unit tests, router in `app.py`
- **A5 — WebSocket event log**: Dashboard event log now uses real-time JavaScript WebSocket to `/api/events/ws`, replacing 5s polling timer
- **A6 — NTRIP integration tests**: 4 tests with MockNtripCaster (v1.0 data flow, v2.0 data flow, auth failure, dual-destination fan-out)

### Phase 2 — GPS Device Configuration (In Progress)
- **Step 2.1 ✅ — Driver ABC & Models**: `device_models.py` (9 Pydantic models), `drivers/base.py` (GpsReceiverDriver ABC), `drivers/__init__.py` (registry/factory) — 37 tests
- **Step 2.2 ✅ — DeviceService**: Driver-agnostic orchestrator with connect/disconnect, state tracking, async wrappers, capability queries, mutual exclusion with relay, config commands — 27 tests, 89% coverage
- **Step 2.3 ✅ — u-blox Driver Implementation**: `drivers/ublox.py` (UbloxDriver) — connect/MON-VER, survey-in, fixed base, RTCM messages, save-to-flash, NAV-SVIN polling, ACK/NAK handling, serial port discovery — 32 tests, 93% driver coverage. Hardware-verified on ZED-F9P @ /dev/ttyUSB0
- **Step 2.4 ✅ — Device API Endpoints**: `api/device.py` — 10 REST endpoints (ports, connect, disconnect, status, capabilities, survey-in config, fixed-base config, RTCM config, save-to-flash, survey-in polling). HTTP guards: 400/409/422/502. `models/api_models.py` additions. 22 tests, pyright 0 errors
- **Step 2.5+2.6 ✅ — Device UI Page (combined)**: `ui/pages/device.py` at `/device` — Connection section (serial port dropdown with GPS auto-detect, baud rate, driver selector, connect/disconnect buttons, status indicator, device info card) + Base Config section (survey-in/fixed-base tabs, RTCM message checkboxes, live survey-in progress polling, save-to-flash). Capability-driven visibility. Added to navigation and app.py
- **Step 2.7 ✅ — Integration & Handoff**: `POST /api/device/handoff` endpoint — disconnects device, persists DeviceProfile & InputProfile, starts relay engine with same serial port. `DeviceProfile` + `InputProfile` models in `config_models.py`, `save_device_profile`/`save_input_config` in ConfigService, "Handoff & Start Relay" button on Device UI, 5 handoff tests (success, not-connected, relay-running, start-fails, with-destinations). 343 total unit tests passing

### Phase 3 — Advanced GPS Features (In Progress)
- **Step 3.2+3.3 ✅ — Survey-In Auto-Promote & Named Positions (Backend)**:
  - `SurveyInProgress` model gains `latitude`, `longitude`, `altitude_m` optional fields
  - `BaseStationPosition` Pydantic model for named position profiles (lat/lon/alt, accuracy, surveyed_at, source)
  - `AppConfig.base_positions` list for YAML persistence
  - `UbloxDriver.extract_svin_position()` — ECEF→WGS84 LLH iterative conversion from NAV-SVIN
  - `ConfigService` CRUD: `get_base_positions`, `get_base_position`, `save_base_position`, `delete_base_position`
  - 5 new API endpoints: `POST /api/device/promote-survey-in`, `GET /api/device/base-positions`, `POST /api/device/base-positions`, `DELETE /api/device/base-positions/{name}`, `POST /api/device/base-positions/{name}/restore`
  - 29 new unit tests; 372 total passing
- **Step 3.4 ✅ — Real-Time GPS Data Display (NAV-PVT)**:
  - `GpsFixType` enum + `GpsPosition` Pydantic model (fix type, RTK status, coords, accuracy, satellites, speed, heading, PDOP, timestamp)
  - `GpsReceiverDriver.get_position()` abstract method + `UbloxDriver` NAV-PVT implementation with full field parsing
  - `DeviceService.get_position()` async wrapper
  - `GET /api/device/position` endpoint (200/409)
  - Device UI "Live Position" card: color-coded fix badge, lat/lon/alt, accuracy, satellites, RTK status, speed, UTC time — auto-polls every 2s when connected
  - 33 new unit tests (model, driver, service, API); 405 total passing
- **Step 3.5 ✅ — Dashboard Integration**:
  - GPS Device card on main Dashboard: connection state badge, device model, live position summary (fix type + RTK + satellites + lat/lon + accuracy)
  - Smart fallback messaging: "Acquiring fix..." / "No GPS device connected"
  - Navigation links to `/device` page (connect or manage)
  - Auto-refreshes with existing dashboard poll timer; non-blocking error handling
  - 0 new tests needed (UI-only change); 405 total still passing
- **Step 3.6 ✅ — GNSS Constellation Selection**:
  - `GnssConstellation` enum (GPS, GLONASS, GALILEO, BEIDOU, SBAS, QZSS), `GnssSystemConfig`, `GnssConfig` models
  - `GpsReceiverDriver.get_gnss_config()` / `configure_gnss()` abstract methods
  - `UbloxDriver`: CFG-GNSS poll/parse/build with gnssId ↔ constellation mapping, ACK/NAK handling
  - `DeviceService.get_gnss_config()` / `configure_gnss()` async wrappers with state management
  - `GET /api/device/gnss`, `PUT /api/device/gnss` API endpoints
  - Device UI GNSS constellation card (capability-gated)
  - 31 new unit tests; 436 total passing
- **Step 3.7 ✅ — Survey-In Progress Visualization**:
  - Real-time ECharts convergence chart (`ui.echart`) in survey-in progress card
  - Dual Y-axis: logarithmic accuracy (mm) + linear observations; X-axis: elapsed time (s)
  - Green dashed target line (markLine) at configured accuracy limit
  - Dynamic line color: red → yellow → green as accuracy converges
  - Gradient area fill, dark theme, smooth animation, responsive width (260px height)
  - Client-side data accumulation; chart cleared on new survey, updated every 2s poll
  - Pure UI change — no new backend, API, or tests needed; 436 tests still passing
- **Step 3.8 ✅ — Current Base Config Read-Back**:
  - `BaseMode` enum + `CurrentBaseConfig` model in `device_models.py`
  - `GpsReceiverDriver.get_base_config()` abstract method; u-blox CFG-VALGET TMODE implementation
  - `DeviceService.get_base_config()` async wrapper
  - `GET /api/device/base-config` REST endpoint (200/409)
  - Device UI "Current Base Config" card: mode badge, lat/lon/alt/acc labels, Refresh/Load/Save buttons
  - GPS serial ports sorted to top of port list
  - 7 new tests (3 service + 4 API); 443 total passing; pyright 0 errors
- **Step 3.9 ✅ — GPS Configuration Audit Tool**:
  - `sp_base/cli/config_audit.py` — reads all u-blox Gen9 config keys from RAM and factory-default layers via CFG-VALGET, reports differences
  - 40+ configuration groups: UART/USB/I2C/SPI ports & protocols, TMODE, SIGNAL, RTCM, NMEA, NAVSPG, rates, power mgmt, etc.
  - Uses pyubx2 `UBX_CONFIG_DATABASE` for complete key coverage; fresh UBXReader per poll to avoid buffer corruption
  - Human-readable formatting: enum maps, annotations explaining each change, `--json` output, `--show-same` flag
  - Bundled as `sp-base-gps-audit` console_scripts entry point in pyproject.toml
  - Exclusive serial port locking (TIOCEXCL + fcntl.flock) in both UbloxDriver and audit tool
  - Documentation: `docs/zed-f9p-base-station-config-reference.md`
  - `tools/read_gps_config.py` retained as thin wrapper for dev convenience
- **Step 3.10 ✅ — UI Restructuring & Cross-Page State Sync**:
  - Broke monolithic Device page into 6 dedicated pages: Dashboard (relay-only), Input, Outputs, Survey-In, Settings, Advanced GPS
  - Sidebar navigation with section separators reflecting setup workflow order
  - Cross-page state sync: auto-detect already-connected GPS when navigating between pages (`_on_page_load()` deferred timers)
  - "Reload Device Data/Config" buttons on both GPS pages for manual re-read without disconnect
  - Serial port refresh button tooltips for clarity
  - Coverage config updated: UI/CLI code excluded from measurement (92.28% on measured code)
  - Old `device.py` deleted; `app.py` updated with new page registrations
  - 480 unit tests passing, 0 pyright errors
- **Step 3.10.1 ✅ — Input Page Configuration Helpers**:
  - Serial port dropdown with GPS auto-detect (⭐ markers), refresh button, baud rate select — reuses `GpsReceiverDriver.list_serial_ports()`
  - Bluetooth device scan via `BluetoothManager` from sp-rtk-base-relay, discovered device cards (name + MAC + paired badge)
  - PIN code field, Test Connection button (pair + trust + RFCOMM discovery), auto-fill channel on success
  - Graceful fallback when `dbus-fast` not installed (manual entry still works)
  - Source-specific UI builders replace generic `FieldDef` pattern for serial and bluetooth
  - TCP fields unchanged; 480 tests still passing; pyright 0 errors
- **Step 3.11 ✅ — Survey-In Page Redesign**:
  - Consolidated Base Station Mode tabs + Current Base Config into clean 4-card layout
  - Card 1: Connection & Live Position (unchanged)
  - Card 2: Survey-In standalone — confirmation dialog, auto-pipeline (promote → flash → refresh) on completion
  - Card 3: Fixed Base Position (merged) — read-only by default, Edit/Commit/Cancel mode, Save Position dialog, Load Saved picker
  - Card 4: Saved Positions — Restore now commits directly (RAM + flash)
  - All position operations (edit, load, restore) write to device RAM + save to flash for reboot survival
  - Removed old tabs, manual promote button, "Current Base Config" card, "Refresh"/"Load into Fixed Base"/"Save as Position Profile" buttons
  - 480 unit tests passing, 0 pyright errors

## Current Metrics
- **Unit tests**: 480 passed
- **Integration tests**: 20+ (end-to-end + destination management + NTRIP)
- **Coverage**: 92.28% (UI/CLI excluded from measurement — NiceGUI can't be unit tested)
- **Pyright**: 0 errors, 0 warnings (strict mode)
- **Python**: 3.10+ compatible
- **API endpoints**: 35 (health, relay, destinations, settings, events, metrics, config, device×19)
- **CLI tools**: `sp-base` (web app), `sp-base-gps-audit` (config audit)
- **UI pages**: 6 (Dashboard, Input, Outputs, Survey-In, Settings, Advanced GPS)

## What's Left to Build

### Remaining Phase 1 Items
- [ ] Visual browser testing of responsive layout
- [x] Docker containerization (local NTRIP caster — `docker/ntrip-caster/`)
- [ ] CI/CD pipeline

### Phase 2 — GPS Device Configuration ✅
- [x] u-blox driver (connect, configure, poll, serial discovery)
- [x] Device API endpoints (11 endpoints, 27 tests)
- [x] Device UI page (connect/disconnect, device info, serial port picker)
- [x] Base Config UI (survey-in, fixed mode, RTCM message selection, save-to-flash)
- [x] Integration & handoff (device→relay handoff API, config persistence, UI button)

### Phase 3 — Advanced GPS Configuration (In Progress)
- [x] Survey-in auto-promote → fixed base (backend: API + driver + ECEF→LLH)
- [x] Named base station position profiles (backend: models, CRUD, API)
- [x] Device UI update (auto-promote button, position profile management section) — browser validated
- [x] Live position display (NAV-PVT) — model, driver, service, API, UI with 2s auto-poll
- [x] Dashboard integration — GPS device card with connection state, position summary, nav links
- [x] GNSS constellation selection — models, driver, service, API, UI, 31 tests
- [x] Survey-in progress visualization (chart/graph) — ECharts convergence chart
- [ ] Config backup/restore (PyUBXUtils)
- [ ] Authentication/authorization

## Known Issues
- UI pages contribute significant uncovered lines (NiceGUI can't be unit tested)
- Integration tests require actual TCP ports — may conflict in CI
- WebSocket event timeout path (30s) not testable in fast unit tests

## Resolved Issues
- **~~Bluetooth auto-start crash~~** (fixed 2026-04-17): Stale D-Bus introspection cache caused `InterfaceNotFoundError: org.bluez.Device1` after device disconnect/power-cycle. Fixed with 3-layer defense: (1) cache invalidation before pair/trust introspection, (2) recovery scan + retry in `ensure_device_ready()`, (3) proper `BluetoothManager.close()` on disconnect to prevent stale cache reuse. 11 new tests added; relay package: 55 tests passing.
