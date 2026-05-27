# End-to-End (E2E) Testing with Playwright

This document describes the SP-Base end-to-end test suite — what it
covers, how it's wired up, how to run it locally, and how to extend
it as the UI evolves.

## Why a separate e2e suite?

The existing `tests/unit/` suite exercises every API endpoint, service
class, and config model in isolation.  It's fast (≈14 s for 485
tests), runs on every push, and enforces ≥90 % branch coverage.

What it **can't** catch:

| Failure class                          | Caught by unit tests? |
|----------------------------------------|-----------------------|
| FastAPI endpoint regression            | ✅                    |
| Pydantic model validation              | ✅                    |
| Service-layer business logic           | ✅                    |
| NiceGUI page mounts at all             | ❌                    |
| Drawer/header rendering on each route  | ❌                    |
| Button click → notification round-trip | ❌                    |
| WebSocket hydration                    | ❌                    |
| Quasar component → backend wiring      | ❌                    |

The Playwright suite at `tests/e2e/` plugs that gap.  It boots the
real `sp_rtk_base.main` process in a subprocess on a random free port,
opens a headless Chromium browser, and asserts on visible UI state.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  pytest process                                         │
│  ┌────────────────┐    ┌───────────────────────────┐    │
│  │ tests/e2e/*.py │───▶│ pytest-playwright fixtures │    │
│  └────────────────┘    │   page, browser_context    │    │
│                        └────────────┬───────────────┘    │
│                                     │ HTTP / WS           │
│                                     ▼                     │
│              ┌───────────────────────────────────┐        │
│              │  Headless Chromium                │        │
│              │  (managed by Playwright)          │        │
│              └─────────────┬─────────────────────┘        │
└────────────────────────────┼──────────────────────────────┘
                             │ http://127.0.0.1:<random>
                             ▼
        ┌──────────────────────────────────────┐
        │  subprocess.Popen([                  │
        │    sys.executable,                   │
        │    "-m", "sp_rtk_base.main"          │
        │  ])  ← real FastAPI + NiceGUI server │
        └──────────────────────────────────────┘
```

Key design choices documented in `tests/e2e/conftest.py`:

1. **Subprocess, not in-process import.**  NiceGUI keeps module-level
   singleton state (the `ui.run()` event loop, page registry, etc.)
   that can't safely be torn down inside a single pytest process.
2. **Isolated `$HOME`.**  Each session gets a fresh
   `tmp_path_factory.mktemp("e2e-home")/.config/sp-rtk-base/` so the
   developer's real `config.yaml` is never touched.
3. **Random free port.**  `socket.bind(("", 0))` picks an OS-assigned
   port → safe to run alongside a developer's `uv run sp-rtk-base` on
   8080.
4. **Strip pytest env vars.**  NiceGUI 3.x's `helpers.is_pytest()`
   sniffs `PYTEST_CURRENT_TEST` and tries to enter screen-test mode,
   which requires the `NICEGUI_SCREEN_TEST_PORT` env var we don't
   set.  The fixture pops those variables before `Popen`.
5. **Server log captured to file.**  `stdout=open(log_path, "wb")`
   avoids the OS pipe-buffer deadlock that bit the first iteration,
   and gives us a debuggable artifact on CI failures.

## Running locally

One-time setup (already done if you ran `uv sync --all-extras`):

```bash
uv sync --all-extras
uv run playwright install chromium      # ~140 MB download
# Linux only — system libs for the headless browser:
sudo uv run playwright install-deps chromium
```

Run the full e2e suite:

```bash
uv run pytest tests/e2e --no-cov
```

Run a single test in headed mode (browser window visible) for
debugging:

```bash
uv run pytest tests/e2e/test_settings_interaction.py --no-cov \
  --headed --slowmo 500
```

Open the Playwright trace viewer after a failure:

```bash
uv run playwright show-trace test-results/.../trace.zip
```

## Current coverage

| File                              | What it asserts                                                                  |
|-----------------------------------|----------------------------------------------------------------------------------|
| `test_navigation_smoke.py`        | All six top-level pages render their H4 heading; `/api/health`                   |
| `test_destinations_crud.py`       | REST create → list → update → delete; UI reflects the list                       |
| `test_settings_interaction.py`    | "Save Settings" button click triggers Quasar toast                               |
| `test_device_connection.py`       | Connect / disconnect lifecycle on the fake GPS driver (REST)                     |
| `test_gps_data_flow.py`           | NAV-PVT polling + GNSS / RTCM round-trips on `/gps-config` (REST + browser)      |
| `test_survey_save_position.py`    | Survey-In → save base position dialog persists to config                         |
| `test_survey_buttons.py`          | Start Survey-In confirm/cancel, Fixed-Base Edit/Commit/Cancel button-handlers    |
| `test_outputs_buttons.py`         | Outputs page: Add Destination dialog, validation warning, Delete dialog          |
| `test_gps_config_buttons.py`      | Advanced GPS page: Disconnect, Save-to-Flash, Load GNSS, Apply GNSS buttons      |
| `test_input_buttons.py`           | Input page: Save TCP host/port → success toast → YAML export round-trip          |

Total: **39 tests, ≈35 s wall-clock** on a Pi-class developer box.

All tests that require an active device session use the
`connected_gps` fixture, which transparently swaps the ublox driver
out for the in-memory `FakeGpsDriver`
(`src/sp_rtk_base/services/drivers/fake.py`).  The fake driver is a
deterministic state machine — Survey-In transitions on demand, NAV-PVT
yields canned values, and `save_to_flash` / `configure_*` calls all
mutate in-process state that's visible via REST — so the browser can
drive every button on every page without any real hardware attached.
The fake is also exercised by `tests/unit/test_fake_driver.py` for
100 % branch coverage.

## Adding new tests

### 1. Reuse the existing fixtures

```python
import pytest
from playwright.sync_api import Page, expect

@pytest.mark.e2e
def test_my_new_thing(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/some-route")
    expect(page.locator("text=Hello")).to_be_visible()
```

`page`, `base_url`, and `api_base_url` are session-scoped — the server
starts once for the whole `pytest tests/e2e` run.

### 2. Prefer stable selectors

In rough order of preference:

1. `page.get_by_role("button", name="Save Settings")` — semantic,
   survives styling changes.
2. `page.locator("[data-testid='destination-add-btn']")` — add a
   `data-testid` to the NiceGUI element via
   `.props('data-testid=destination-add-btn')`.
3. `page.locator("text=Save Settings")` — works today but brittle to
   copy changes.

NiceGUI passes arbitrary `props=` strings straight through to the
underlying Quasar element, so `data-testid` is the right escape hatch
when role-based selectors aren't unique enough.

### 3. Mark tests with `@pytest.mark.e2e`

The marker is declared in `pyproject.toml` and lets us run / skip the
suite explicitly:

```bash
uv run pytest -m e2e            # only e2e
uv run pytest -m "not e2e"      # everything except e2e
```

### 4. Clean up state with `clean_config`

If your test depends on "no destinations exist," depend on the
`clean_config` fixture:

```python
def test_empty_state(page: Page, base_url: str, clean_config: None) -> None:
    ...
```

The fixture wipes destinations via REST before **and** after the test
so neighbours don't pollute each other.

## CI integration

The `e2e` job in `.github/workflows/ci.yml` runs after the unit-test
matrix succeeds (it depends on `test`).  On failure, the workflow
uploads:

- `pytest-e2e-junit.xml`  — JUnit XML for the GitHub test-results UI
- `test-results/`         — Playwright traces / videos if any were
  produced (pytest-playwright writes these by default on failure when
  `--tracing=retain-on-failure` is set; see roadmap below).

The Playwright browser binaries are cached at `~/.cache/ms-playwright`
keyed on `uv.lock`, so subsequent runs skip the ~140 MB download.

## Driving device-dependent tests: FakeGpsDriver

The e2e suite includes a synthetic `FakeGpsDriver`
(`src/sp_rtk_base/services/drivers/fake.py`) that's registered under
the vendor key `"fake"` **only when the environment variable
`SP_RTK_BASE_FAKE_GPS=1` is set**.  Production binaries with the var
unset never see it.

### How the wiring works

1. `tests/e2e/conftest.py` exports `SP_RTK_BASE_FAKE_GPS=1` into the
   subprocess `env` before spawning the server.
2. `services/drivers/__init__.py` checks the env var on import and
   conditionally calls `register_driver("fake", FakeGpsDriver)`.
3. A function-scoped fixture `connected_gps` POSTs
   `/api/device/connect` with
   `{"vendor": "fake", "port": "FAKE", "baud_rate": 115200}` and
   tears down with `/api/device/disconnect` after the test.

### Fixture values

The fake driver returns a stable, RTK-fixed solution:

| Field           | Value                |
| --------------- | -------------------- |
| latitude        | `32.7329015 °`        |
| longitude       | `-117.2362788 °`      |
| altitude_m      | `27.940 m`            |
| accuracy_mm     | `47308`              |
| rtk_status      | `fixed`              |
| num_satellites  | ≥ 20                 |
| capabilities    | full set (`SURVEY_IN`, `FIXED_BASE`, `SAVE_TO_FLASH`, `POSITION_STREAM`, …) |
| GNSS systems    | 6 (GPS/GLONASS/Galileo/BeiDou/QZSS/SBAS) |

Survey-In is driven by an in-process state machine, so the full
`IDLE → IN_PROGRESS → COMPLETE` path runs end-to-end inside the
suite without any timing dependence on real receiver convergence.

### Tests that use it

- `tests/e2e/test_survey_save_position.py` — browser regression for
  the Save-Position dialog bug.  Hits the **real button**, asserts
  the success toast appears, the dialog closes, and the persisted
  lat/lon/alt match the fixture values via REST read-back.
- `tests/e2e/test_device_connection.py` — REST-only lifecycle
  (connect → status → double-connect 409 → disconnect → idempotent
  disconnect → unknown-vendor 400 → capability set).
- `tests/e2e/test_gps_data_flow.py` — position fixture readback,
  GNSS GET/PUT round-trip (disable Galileo and prove the change
  persists), base-config DISABLED → FIXED transition, save-to-flash,
  Advanced GPS page render with Save-to-Flash button visible.

### When to extend it

- **Adding a new device capability** to the production code?  Add
  a matching method override to `FakeGpsDriver` and a unit test in
  `tests/unit/test_fake_driver.py` that exercises it.  The fake
  driver's coverage gate is **100 %** — keep it there.
- **Adding a new failure-mode test**?  The fake driver currently
  models the *happy path* only.  If you need to assert on error
  handling, the recommended pattern is to disconnect first
  (`page.request.post(api_base_url + "/api/device/disconnect")`)
  and rely on the existing 409 / 404 paths in `DeviceService`.
  Injection helpers (`fail_next_call`, `simulate_nack`, etc.) are
  a planned extension to `FakeGpsDriver` — open a TODO before
  adding tests that depend on them.

## Roadmap

Things the current suite intentionally does **not** do yet, in
priority order:

1. **`data-testid` props on dialog inputs** in
   `ui/pages/outputs.py`, `ui/pages/survey.py`, and
   `ui/pages/gps_config.py` — would let us drive the *full* create-
   destination dialog through Playwright instead of falling back to
   the REST API.
2. **Visual regression tests** with `playwright.expect(page).to_have_screenshot()`
   — gated on stabilising the dashboard's live-data widgets first
   (they currently render "—" placeholders that flip to real values
   asynchronously and would flake snapshot diffs).
3. **`--tracing=retain-on-failure`** in `pyproject.toml` so failed CI
   runs upload a fully replayable trace.
4. ~~**Survey-In workflow** — click "Save Current Position" with a
   mocked GPS service so we can assert on the round-trip into the
   base-positions list.~~ ✅ Landed 2026-05-26 via FakeGpsDriver +
   `tests/e2e/test_survey_save_position.py`.
5. **Cross-browser** — add Firefox + WebKit to the
   `pytest-playwright --browser` flag in CI once the suite stabilises.

## Playwright MCP server (optional, dev-time only)

For ad-hoc UI exploration during development, the Playwright MCP
server is wired into Cline at `~/.vscode-server/.../cline_mcp_settings.json`.
It lets the AI assistant drive a real Chromium instance interactively
without writing test code.  **The MCP server is not used by `pytest`
or CI** — it's purely a developer convenience.
