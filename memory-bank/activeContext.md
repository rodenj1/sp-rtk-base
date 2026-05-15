# Active Context

## Latest Change: Embedded Relay Package Renamed `sp-base-relay` → `sp-rtk-base-relay` (2026-05-14)

### Summary
The embedded relay-engine package was renamed from `sp-base-relay` to `sp-rtk-base-relay` (directory + distribution name + import package `sp_base_relay` → `sp_rtk_base_relay`). All sp-base references were updated to match.

### Files Updated in sp-base (outside packages/)
- **Source (7)**: `src/sp_base/services/{relay_service,event_bridge,metrics_service,__init__}.py`, `src/sp_base/models/config_models.py`, `src/sp_base/ui/pages/{input,settings}.py`
- **Tests (5)**: `tests/unit/test_{relay_service,event_bridge,metrics_service,api_metrics,config_models}.py`
- **Docs**: `README.md`, `docs/relay-engine-api-spec.md`, `docs/ublox_gps_webui_planning.md`, `tools/test_ntrip_caster.py`
- **Memory bank (6)**: `projectbrief.md`, `productContext.md`, `systemPatterns.md`, `techContext.md`, `activeContext.md`, `progress.md`

### Prometheus Gauge Rename
Two relay-engine-scoped gauges had names tied to the relay package and were updated:
- `sp_base_relay_running` → `sp_rtk_base_relay_running`
- `sp_base_relay_uptime_seconds` → `sp_rtk_base_relay_uptime_seconds`

In `MetricsService` these now use literal names (not `f"{ns}_..."`) since they represent the relay engine rather than the sp-base app. The remaining sp-base-specific gauges (`sp_base_input_*`, `sp_base_dest_*`, etc.) still use the configurable `namespace` prefix (defaults to `sp_base`). The `test_custom_namespace` test was updated to assert this split contract.

### Verification
- `uv sync` — clean (package resolved via path dependency `packages/sp-rtk-base-relay`)
- `uv run pytest tests/unit -q` — **480 passed**
- `uv run pyright src/sp_base` — **0 errors, 0 warnings**
- `grep -r "sp_base_relay\|sp-base-relay"` outside `packages/` — no matches

### Operator Impact
**Breaking for Grafana / alerting**: anyone scraping `/metrics` with dashboards or PromQL alerts referencing `sp_base_relay_running` or `sp_base_relay_uptime_seconds` must rename those queries to `sp_rtk_base_relay_*`. The shipped Grafana dashboard template lives in `packages/sp-rtk-base-relay/templates/grafana_dashboard.json` and was already updated as part of the package rename.

---

## Previous: Graceful Shutdown Fix — Ctrl+C Hangs (2026-04-17)

### Problem
After the previous bluetooth disconnect fix, the application would hang on Ctrl+C, requiring multiple SIGINT signals to kill the process. Background threads (relay engine, event bridge, NTRIP destinations, bluetooth D-Bus loop) kept running during uvicorn's shutdown sequence because there was no `on_shutdown` handler. Additionally, the WebSocket event handler had a 30-second timeout that blocked graceful shutdown, and the NTRIP socket was set to infinite blocking mode.

### Fixes Applied (3 changes)

1. **Added `on_shutdown` handler to `app.py`**:
   - Registered `app.on_shutdown(_shutdown)` that cleanly stops:
     - `event_bridge.stop()` — closes subscription, joins daemon thread
     - `relay_service.stop_relay()` — stops BroadcastHub → destinations → input threads
   - All background threads now receive stop signals before uvicorn exits

2. **Reduced WebSocket timeout in `api/events.py`** (30s → 5s):
   - The `asyncio.wait_for(event_queue.get(), timeout=...)` was 30 seconds
   - During shutdown, this blocked uvicorn from closing WebSocket connections
   - Reduced to 5 seconds — handler still loops and sends keepalive pings, but exits quickly on shutdown

3. **NTRIP socket send timeout** (`ntrip_destination.py`):
   - Changed `sock.settimeout(None)` (infinite blocking) to `sock.settimeout(30.0)`
   - Prevents `_send_data()` from blocking forever if the remote caster stops reading
   - 30-second timeout is generous enough for normal operation

### Files Changed
- `src/sp_base/app.py` — Added `_shutdown()` handler with `app.on_shutdown()`
- `src/sp_base/api/events.py` — WebSocket timeout 30s → 5s
- `packages/sp-rtk-base-relay/src/sp_rtk_base_relay/core/destinations/ntrip_destination.py` — Socket timeout `None` → `30.0`

### Tests: All pass (21 sp-base, 77 relay)

## Previous: Bluetooth Stale D-Bus Cache Bug Fix (2026-04-17)

### Problem
When the saved config has input source=bluetooth but the paired device was disconnected/power-cycled, `BluetoothManager._async_pair_device()` threw `InterfaceNotFoundError: org.bluez.Device1`. The only workaround was manually scanning from the Input page, which reset BlueZ's internal state. The root cause was **stale D-Bus introspection cache**: BlueZ removes device D-Bus objects on disconnect, but the `BluetoothManager._introspection_cache` still held the old XML, so `get_interface("org.bluez.Device1")` failed on the stale proxy.

### Fixes Applied (3 layers of defense)

1. **Cache invalidation before introspection** (`bluetooth_manager.py`):
   - `_invalidate_device_cache()` method evicts device paths from the introspection cache
   - Called at the top of `_async_pair_device()` and `_async_trust_device()` — always re-introspects fresh from BlueZ

2. **Recovery scan + retry in `ensure_device_ready()`** (`bluetooth_manager.py`):
   - Extracted `_pair_and_trust()` helper for pair+trust sequence
   - If first attempt fails, runs `_recovery_scan()` (short BlueZ discovery to re-register device), invalidates cache, retries once
   - `_recovery_scan()` is non-fatal — swallows errors gracefully
   - After both attempts fail, raises `BluetoothError` with context from both failures

3. **Proper BluetoothManager cleanup on disconnect** (`bluetooth_input.py`):
   - `disconnect()` now calls `bt_manager.close()` to shut down the background D-Bus event loop
   - Sets `bt_manager = None` so the next `connect()` creates a fresh manager with empty cache
   - Handles `close()` errors gracefully (logged, not raised)

### Tests Added (11 new)
- `test_ensure_device_ready_with_mac` — pair+trust via MAC address
- `test_invalidate_device_cache_removes_entry` — cache eviction works
- `test_invalidate_device_cache_noop_when_not_cached` — safe when not cached
- `test_pair_device_invalidates_cache_before_introspection` — core fix validation
- `test_trust_device_invalidates_cache_before_introspection` — core fix validation
- `test_ensure_device_ready_retries_after_failure` — recovery scan + retry succeeds
- `test_ensure_device_ready_fails_after_retry` — proper error after both attempts fail
- `test_recovery_scan_is_non_fatal_on_failure` — scan errors don't crash
- `test_disconnect_closes_bluetooth_manager` — close() called + manager set to None
- `test_disconnect_handles_close_error_gracefully` — close() error swallowed
- `test_disconnect_without_connected_mac_still_closes_manager` — cleanup path without MAC

### Files Changed
- `packages/sp-rtk-base-relay/src/sp_rtk_base_relay/core/bluetooth_manager.py` — cache invalidation, recovery scan, retry logic
- `packages/sp-rtk-base-relay/src/sp_rtk_base_relay/core/input_sources/bluetooth_input.py` — disconnect cleanup
- `packages/sp-rtk-base-relay/tests/unit/test_bluetooth_manager.py` — 8 new tests
- `packages/sp-rtk-base-relay/tests/unit/test_bluetooth_input.py` — 3 new tests

### Relay Package Tests: 55 passed (up from 44)

## Previous: Local NTRIP Caster for Dev/Testing (2026-04-16)

### Local NTRIP Caster Setup
Created a lightweight Python-based NTRIP caster running in Docker for local development and testing of both NTRIP v1.0 and v2.0 protocols.

**What was built:**
- `docker/ntrip-caster/ntrip_caster.py` — Pure Python asyncio NTRIP caster (zero dependencies)
  - NTRIP v1.0: SOURCE/ICY protocol (server push), GET with ICY responses (client pull)
  - NTRIP v2.0: HTTP/1.1 POST (server push with Basic auth), HTTP/1.1 GET (client pull)
  - Dynamic mountpoint management (created on server connect, removed on disconnect)
  - Data broadcast from servers to all connected clients
  - Sourcetable generation (both v1 and v2 format)
  - Auth enforcement (password for servers, open for clients)
- `docker/ntrip-caster/Dockerfile` — Minimal python:3.12-alpine container
- `docker/ntrip-caster/docker-compose.yml` — Port 2101, env config
- `tools/test_ntrip_caster.py` — Comprehensive 9-test validation script
- `docs/local-ntrip-caster.md` — Full documentation

**Key decisions:**
- Originally tried Node-NTRIP (@ntrip/caster) but it has broken `process.binding('http_parser')` on Node 18/20 — connections silently reset
- Python asyncio approach: zero dependencies, full protocol control, easy to debug, fast build
- Env vars: CASTER_PORT (2101), SERVER_PASSWORD (testpass), LOG_LEVEL (info)

**Test results: ALL 9/9 PASSED** — TCP connectivity, v1+v2 sourcetable, v1+v2 auth (accept+reject), v1+v2 data push

## Previous: Input Page Configuration Helpers (2026-04-16)

### Serial Port Detection on Input Page
Enhanced the Input page serial source with the same port detection used on the GPS Config and Survey pages:
- **Dropdown select** replaces the plain text input for serial port path
- Reuses `GpsReceiverDriver.list_serial_ports()` from `drivers/base.py`
- **GPS auto-detect**: Known USB vendor IDs (u-blox, FTDI, Prolific, Silicon Labs) flagged with ⭐
- **Refresh button** to re-scan ports
- **`with_input=True`** allows manual path entry for custom/unmapped ports
- **Baud rate select** dropdown with standard rates (9600–921600)
- Saved port/baud restored from config on page load

### Bluetooth Discovery + Test Connection on Input Page
Added interactive Bluetooth device management when source=bluetooth:
- **Scan for Devices** button — creates a `BluetoothManager` (from sp-rtk-base-relay) and scans for ~8 seconds
- **Discovered device cards** — shows name, MAC address, "Paired" badge; click to auto-fill address field
- **PIN Code field** — defaults to "0000", editable for devices with different PINs
- **Test Connection button** — calls `BluetoothManager.ensure_device_ready()` (pair + trust + RFCOMM discovery)
- On success: auto-fills RFCOMM channel, shows ✓ with device details
- On failure: shows ✗ with error message
- **Graceful fallback**: If `dbus-fast` is not installed, shows warning but still allows manual address entry
- All BlueZ D-Bus operations wrapped with `asyncio.to_thread()` for async compatibility

### Implementation Details
- **Approach A1**: Reused `GpsReceiverDriver.list_serial_ports()` directly — no new backend needed
- **Approach B1**: Imported `BluetoothManager` from sp-rtk-base-relay directly — no new service wrapper
- TCP source fields remain unchanged (simple host + port text inputs)
- The old generic `FieldDef` pattern replaced with source-specific builders: `_build_tcp_fields()`, `_build_serial_fields()`, `_build_bluetooth_fields()`
- `_save_input()` gathers values differently per source type (text inputs, select widgets, bt_state dict)

## Previous: Survey-In Page Redesign (2026-04-16)

### Survey-In Page Simplification
Consolidated the cluttered Base Station Mode tabs + Current Base Config card into a clean 4-card layout.

## Previous: UI Restructuring + Cross-Page State Sync + Reload Buttons (2026-04-15)

### UI Restructuring — Sidebar Reorganization
Broke down the cluttered single-page device UI into a logical multi-page workflow:

**New sidebar navigation (top-to-bottom order):**
1. **Dashboard** (`/`) — Relay status only (GPS device card removed since relay uses the serial port)
2. **Input** (`/input`) — Input source configuration (TCP/serial/Bluetooth)
3. **Outputs** (`/outputs`) — Destination CRUD and management
4. **Survey-In** (`/survey`) — Full survey workflow: connect, survey-in, fixed base, save/restore positions, live position display
5. **Settings** (`/settings`) — App settings only (auto-start, dark mode, metrics)
6. **Advanced GPS** (`/gps-config`) — RTCM messages, GNSS constellations, save-to-flash, relay handoff

Sections are separated with visual dividers in the sidebar. Old `device.py` page deleted.

### Cross-Page State Sync
Both Survey-In and GPS Config pages auto-detect if the GPS device is already connected (via the singleton DeviceService) when the page loads:
- **gps_config.py**: `_on_page_load()` timer auto-loads RTCM and GNSS configs
- **survey.py**: `_on_page_load()` timer starts position polling and reads base config
- Uses `ui.timer(interval=0.1, callback=..., once=True)` for deferred async execution after page render

### Reload Device Data Buttons
Both GPS pages now have explicit reload buttons (only visible when connected):
- **GPS Config page**: "Reload Device Config" button — re-reads RTCM and GNSS configs from receiver
- **Survey-In page**: "Reload Device Data" button — re-polls position, ensures polling timer active, re-reads base config
- Serial port refresh button now has tooltip: "Refresh serial port list"

### Coverage Config Update
UI pages, layout, components, and CLI audit tool excluded from coverage measurement (NiceGUI presentation code can't be unit tested). Coverage: 92.28% on measured code.

## Key Files Changed
- `src/sp_base/ui/layout.py` — New nav items with separators
- `src/sp_base/ui/pages/input.py` — New (extracted from settings)
- `src/sp_base/ui/pages/survey.py` — New (survey workflow + positions + live position + reload button)
- `src/sp_base/ui/pages/gps_config.py` — New (RTCM + GNSS + flash + handoff + reload button)
- `src/sp_base/ui/pages/dashboard.py` — Slimmed (relay-only)
- `src/sp_base/ui/pages/settings.py` — Slimmed (app settings only)
- `src/sp_base/app.py` — Updated page registrations
- `src/sp_base/ui/pages/device.py` — Deleted
- `pyproject.toml` — Coverage omit for UI/CLI code

## Key Metrics
- **Unit tests**: 480 passed
- **Coverage**: 92.28% (non-UI code)
- **Pyright**: 0 errors, 0 warnings (strict mode)
- **UI pages**: 6 (Dashboard, Input, Outputs, Survey-In, Settings, Advanced GPS)
- **API endpoints**: 35+

## Next Steps
- Hardware testing of cross-page navigation with live GPS
- Config backup/restore (PyUBXUtils)
- Docker containerization
- CI/CD pipeline
