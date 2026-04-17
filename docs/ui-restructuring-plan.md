# UI Restructuring Plan

**Date:** 2026-04-15
**Status:** Approved — Ready for Implementation

## Problem

The current UI has 4 pages (Dashboard, Device, Outputs, Settings) and the Device page in particular is a 1,423-line monolith that combines GPS connection, live position, survey-in, fixed base config, RTCM messages, GNSS constellations, save-to-flash, saved positions, and relay handoff all on one page. The Dashboard also includes a GPS device card that's rarely useful (the GPS is typically disconnected while the relay is running).

## Goals

1. **Declutter** — break the monolith Device page into focused pages
2. **Workflow order** — sidebar follows the natural operator workflow
3. **Relay focus** — Dashboard should only show relay status
4. **Survey-in dedicated** — survey workflow gets its own page
5. **Port persistence** — remember last-used serial port and baud rate

---

## New Navigation Structure

### Sidebar Order (top to bottom)

```
📊 Dashboard          /            — Relay status only (landing page)
─── Configuration ───
📥 Input              /input       — Input source config (TCP/serial/BT)
📤 Outputs            /outputs     — Destination management (CRUD)
─── Survey ───
📍 Survey-In          /survey      — GPS connection + survey workflow + positions
─── System ───
⚙  Settings           /settings    — Application settings only
🔧 Advanced GPS       /gps-config  — RTCM, GNSS, save-to-flash, handoff
```

### Navigation Sections in Drawer

The left drawer will have section headers (small grey labels) separating logical groups:

- Top: Dashboard (always first, landing page)
- "Configuration" section: Input, Outputs
- "Survey" section: Survey-In
- "System" section: Settings, Advanced GPS

---

## Page-by-Page Specification

### 1. Dashboard (`/`) — `pages/dashboard.py`

**Purpose:** Relay engine status and monitoring only.

**Content (keep):**
- Relay control bar (Start/Stop buttons, status indicator, uptime)
- Input Source metrics card (connected, source type, bytes received, messages)
- Throughput metrics card (bytes in, frames parsed, chunks out)
- Destinations summary card (active/total count, errors, dropped)
- Per-destination detail rows
- Error banner (disconnected input, destination errors)
- Event log (WebSocket-fed real-time + backfill)

**Content (remove):**
- ❌ GPS Device card (`_render_device_card()` and all related code)
- ❌ `get_device_service()` import and usage

**Changes:**
- Remove ~70 lines of GPS device rendering code
- Remove `device_card` variable and `_render_device_card()` function
- Remove `get_device_service` import

---

### 2. Input (`/input`) — `pages/input.py` *(NEW)*

**Purpose:** Configure the RTCM input source. This is the first thing an operator configures.

**Content (extracted from Settings page):**
- Source type selector (TCP / serial / Bluetooth)
- Source-specific config fields (host/port for TCP, serial port/baud for serial, address/channel for Bluetooth)
- Save Input Config button
- Uses existing `SOURCE_TYPES`, `SOURCE_FIELDS` definitions
- Uses existing `get_config_service()` for persistence

**Source:** Extracted from `pages/settings.py` "Input Source Section" (lines 57-119)

---

### 3. Outputs (`/outputs`) — `pages/outputs.py`

**Purpose:** Destination management.

**Changes:** None — this page stays exactly as-is.

---

### 4. Survey-In (`/survey`) — `pages/survey.py` *(NEW)*

**Purpose:** The complete survey-in workflow — connect to GPS, run a survey, save/restore positions.

**Sections:**

#### Section A: Connection & Live Position (combined)
- Serial port dropdown (with GPS auto-detect ⭐ markers)
- Baud rate selector
- Driver selector
- Connect / Disconnect / Cancel buttons
- Status indicator row
- Device info (compact — model, firmware, protocol, hardware, capability badges)
- Live position display: fix type badge, lat/lon/alt, H/V accuracy, satellites, PDOP, RTK status, speed, UTC time
- **Port/baud auto-loaded from saved config** on page load
- **Port/baud saved to config** on successful connect

#### Section B: Base Station Mode
- Tabs: Survey-In | Fixed Base
- **Survey-In tab:**
  - Min duration (seconds) input
  - Accuracy limit (mm) input
  - Start Survey-In button
  - Survey-in progress card (status label, progress bar, duration/accuracy/observations labels)
  - ECharts convergence chart (accuracy + observations over time)
  - Position display when survey valid (lat/lon/alt)
  - Promote to Fixed Base button (when valid)
  - Save Position Profile button (when valid)
- **Fixed Base tab:**
  - Latitude / Longitude / Altitude / Accuracy inputs
  - Apply Fixed Position button

#### Section C: Current Base Config & Saved Positions (combined)
- **Current Base Config:**
  - Mode badge (Disabled/Survey-In/Fixed)
  - Lat/Lon/Alt/Accuracy read-back from device
  - Refresh button
  - Load into Fixed Base button (copies coords to Fixed Base form above)
  - Save as Position Profile button
- **Saved Positions:**
  - List of previously saved position profiles
  - Each entry shows: name, lat/lon/alt, accuracy, source badge
  - Restore button (sends position to device as fixed base)
  - Delete button
  - "No saved positions yet" placeholder when empty

**Source:** Extracted from `pages/device.py`:
- Connection section (lines 58-103)
- Current Base Config section (lines 108-138)
- Live Position section (lines 143-187)
- Base Station Mode section (lines 191-441)
- Saved Positions section (lines 606-613)
- All related event handlers

---

### 5. Settings (`/settings`) — `pages/settings.py`

**Purpose:** Application-level settings only.

**Content (keep):**
- Auto-start relay toggle
- Prometheus metrics toggle
- Status poll interval

**Content (remove):**
- ❌ Input Source section (moved to `/input` page)
- ❌ `InputProfile` import
- ❌ `SOURCE_TYPES`, `SOURCE_FIELDS` definitions
- ❌ Validator imports only needed for input fields

**Changes:**
- Remove ~65 lines of input source config
- Page becomes much simpler (~50 lines)

---

### 6. Advanced GPS (`/gps-config`) — `pages/gps_config.py` *(NEW)*

**Purpose:** Advanced GPS receiver configuration — RTCM messages, GNSS constellations, save to flash, relay handoff.

**Sections:**

#### Section A: Connection
- Same serial port picker / baud / driver / connect / disconnect as Survey-In
- Shared `DeviceService` state — if connected on Survey-In, shows connected here too
- **Port/baud auto-loaded from saved config**
- Device info card (when connected)

#### Section B: RTCM Message Output
- Multi-port grouped table (USB, UART1, UART2, I2C columns)
- Per-message checkboxes per port
- Rate selector
- Quick-toggle per-port buttons, Clear All
- Load from Device / Apply RTCM Config buttons

#### Section C: GNSS Constellations
- GPS/GLONASS/Galileo/BeiDou/SBAS/QZSS toggle switches
- Load from Device / Apply GNSS Config buttons
- Capability-gated visibility

#### Section D: Save to Flash
- Save to Flash button
- Description text

#### Section E: Handoff to Relay
- Handoff & Start Relay button
- Description text

**Source:** Extracted from `pages/device.py`:
- Connection section (shared)
- RTCM Message Output section (lines 443-558)
- GNSS Constellations section (lines 560-593)
- Save to Flash section (lines 596-604)
- Handoff to Relay section (lines 615-626)
- All related event handlers

---

## Backend Changes

### Port/Baud Persistence

**Model change** — Add to `AppConfig` or `AppSettings` in `config_models.py`:
```python
class DeviceConnectionSettings(BaseModel):
    """Persisted GPS device connection preferences."""
    last_port: str = ""
    last_baud_rate: int = 115200
    last_driver: str = "ublox"
```

Add `device_connection: DeviceConnectionSettings` field to `AppConfig`.

**ConfigService** — Add methods:
```python
def get_device_connection_settings(self) -> DeviceConnectionSettings: ...
def save_device_connection_settings(self, settings: DeviceConnectionSettings) -> None: ...
```

**UI behavior:**
- On page load (Survey-In or Advanced GPS): read saved port/baud/driver, pre-populate dropdowns
- On successful connect: save port/baud/driver to config

### Auto-Disconnect on Relay Start

**Location:** `RelayService.start_relay()` or the Dashboard `_start_relay()` handler.

**Logic:**
```python
# Before starting relay, check if GPS device is connected on the same port
device_svc = get_device_service()
if device_svc.is_connected:
    config = get_config_service().get_input_config()
    if config and config.config.get("port") == device_svc.connected_port:
        await device_svc.disconnect()
```

This only disconnects if the relay input port matches the GPS device port. If they're on different ports (e.g., Bluetooth relay + USB GPS config), no disconnection needed.

---

## Shared UI Patterns

### Connection Component
Both Survey-In and Advanced GPS need the same connection UI. Options:
1. **Duplicate the code** (simplest, each page is self-contained)
2. **Extract a shared function** like `render_connection_section()` that returns references to the widgets

**Recommendation:** Start with option 1 (duplication) since the two pages may evolve differently. Extract later if they stay identical.

### Capability-Gated Visibility
Both pages should respect `DeviceCapability` flags:
- Survey-In tab: only if `SURVEY_IN` capability
- Fixed Base tab: only if `FIXED_BASE` capability
- RTCM config: only if `RTCM_MESSAGE_SELECT` capability
- GNSS config: only if `GNSS_SELECT` capability
- Save to Flash: only if `SAVE_TO_FLASH` capability

---

## Files Changed Summary

| File | Action |
|------|--------|
| `ui/layout.py` | Update `NAVIGATION_ITEMS` with sections |
| `ui/pages/input.py` | **NEW** — input source config |
| `ui/pages/survey.py` | **NEW** — survey-in workflow |
| `ui/pages/gps_config.py` | **NEW** — advanced GPS config |
| `ui/pages/dashboard.py` | Remove GPS device card |
| `ui/pages/settings.py` | Remove input source section |
| `ui/pages/device.py` | **DELETE** — replaced by survey.py + gps_config.py |
| `ui/pages/__init__.py` | Update imports |
| `app.py` | Update page registrations |
| `models/config_models.py` | Add `DeviceConnectionSettings` |
| `services/config_service.py` | Add device connection persistence methods |
| `services/relay_service.py` OR `ui/pages/dashboard.py` | Auto-disconnect logic |

## Tests Impact

- Existing API tests: **no changes** (API layer unchanged)
- Existing unit tests: **no changes** (services unchanged except new methods)
- New unit tests needed for:
  - `DeviceConnectionSettings` model
  - `ConfigService.get/save_device_connection_settings()`
  - Auto-disconnect logic
- `test_app.py`: Update to reference new page modules instead of `device`

---

## Implementation Order

1. Create this plan document ✅
2. `layout.py` — new navigation structure
3. `input.py` — extract from settings, simplest new page
4. `settings.py` — slim down (remove input section)
5. `config_models.py` + `config_service.py` — add device connection persistence
6. `survey.py` — largest new page (connection + survey + positions)
7. `gps_config.py` — second new page (connection + RTCM + GNSS + flash + handoff)
8. `dashboard.py` — remove GPS device card
9. `app.py` — update page registrations, remove device.py reference
10. Delete `device.py`
11. Run full test suite, fix regressions
12. Update memory bank

---

## Risk Notes

- The `device.py` file is 1,423 lines — splitting requires careful extraction of shared state (timers, chart data, position tracking variables)
- Both new pages share `DeviceService` singleton state — connecting on one page means you're connected on the other, which is the desired behavior
- Survey-in timers and position polling timers need proper cleanup when navigating away (already handled by NiceGUI page lifecycle)
- The ECharts convergence chart config is ~120 lines of nested dict — will be moved as-is to survey.py
