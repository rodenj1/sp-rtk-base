# Progress

## Recent Changes

### 2026-05-27 — Button-Click E2E Tests Across Every Page 🆕
Added four new e2e test files driving every actionable button on the Outputs, Survey, Advanced-GPS, and Input pages through the real browser, lifting the e2e suite from 27 → **39 tests** (still green, ~35 s wall-clock).  Every test asserts the Quasar toast in the browser **and** verifies the side-effect via REST so a UI change that wires the click to the wrong handler can never quietly slip through.

- `tests/e2e/test_outputs_buttons.py` (3) — Add-Destination success/validation, Delete dialog.
- `tests/e2e/test_survey_buttons.py` (4) — Start-Survey confirm/cancel, Fixed-Base Edit/Commit/Cancel.
- `tests/e2e/test_gps_config_buttons.py` (4) — Disconnect, Save-to-Flash, Load-GNSS, Apply-GNSS.
- `tests/e2e/test_input_buttons.py` (1) — TCP host/port Save → YAML round-trip.
- `docs/e2e-testing.md` table refreshed (39 tests / ~35 s, per-file column).

Gotchas resolved (the kind that bite you if you don't read the source first):
- Page button "Start Survey-In" vs dialog button "Start Survey" — disambiguate with `role + exact=True`.
- Endpoints don't always match UI nouns: GNSS read is `/api/device/gnss`, base-config read is `/api/device/base-config`, survey-in status is `/api/device/survey-in`.
- `DeviceStatus` serialises `state` ("disconnected"/"connected") — there is no `connected: bool`.
- Quasar `q-toggle` clicks don't always flip the underlying `v-model` from Playwright.  When that bites a side-effect assertion, switch from "did the toggle flip" to "did the Apply write differ from a seeded baseline" — equally meaningful, more robust.

Verification: `pytest tests/e2e --no-cov` → **39 passed in 35 s** · unit suite unchanged at 530 / 92.17 %.

### 2026-05-26 — FakeGpsDriver + Device-Driven E2E Coverage
Added an in-memory `FakeGpsDriver` (vendor key `"fake"`, env-gated on `SP_RTK_BASE_FAKE_GPS=1`) and 16 new e2e tests that exercise the device-dependent UI/REST paths end-to-end without real hardware.

- **Why**: the initial Playwright harness covered navigation, REST CRUD, and settings — but every device-driven path (Survey-In, Advanced GPS, GNSS config, position polling, save-to-flash) was either skipped or REST-mocked, and the Survey-Save dialog regression test was still a placeholder.  We needed a synthetic driver that exposes the full `GpsReceiverDriver` interface so the entire `DeviceService` → API → UI stack runs unchanged.
- **What landed**:
  - `src/sp_rtk_base/services/drivers/fake.py` (new, **100 %** unit coverage): implements all 17 abstract methods on `GpsReceiverDriver` with realistic fixture data — RTK-fixed solution at the bug-report coordinates `32.7329015 °N / -117.2362788 °W / 27.940 m`, 6 GNSS constellations (GPS/GLONASS/Galileo/BeiDou/QZSS/SBAS), full capability set, in-process survey-in state machine.
  - `src/sp_rtk_base/services/drivers/__init__.py`: env-gated auto-registration under vendor key `"fake"`.  Activates only when `SP_RTK_BASE_FAKE_GPS == "1"`; production binaries with the var unset are bit-for-bit identical.  Registry can be reset for tests (idempotent re-import supported).
  - `tests/unit/test_fake_driver.py` (45 tests): fixture-value invariants, capability set, GNSS round-trip, survey-in state-machine transitions, base-config transitions, registry env gating, auto-registration toggle.
  - `tests/e2e/conftest.py`: subprocess fixture now exports `SP_RTK_BASE_FAKE_GPS=1`; added `connected_gps` function-scoped fixture that POSTs `/api/device/connect` with `{vendor: "fake", port: "FAKE", baud_rate: 115200}` and disconnects on teardown.
  - **New e2e files**:
    - `tests/e2e/test_survey_save_position.py` (2 tests) — full browser-driven regression for the Save-Position dialog bug (click → toast → dialog-close → REST-verify persisted lat/lon/alt against FakeGpsDriver fixture).  Replaces the earlier placeholder.
    - `tests/e2e/test_device_connection.py` (6 tests) — REST-only connect/disconnect lifecycle, double-connect 409, idempotent disconnect, unknown-vendor 400, capability set.
    - `tests/e2e/test_gps_data_flow.py` (7 tests) — position fixture, GNSS GET/PUT round-trip (disable Galileo), base-config DISABLED → FIXED transition, save-to-flash, Advanced GPS page render with Save-to-Flash button visible.
- **Gotchas resolved**:
  1. `PUT` not `POST` for GNSS config — the write endpoint is `PUT /api/device/gnss`; the older `/configure/gnss` shape silently no-op'd in the first test draft.
  2. `/api/device/ports` returns the live serial scan, not driver-registered ports — FakeGpsDriver doesn't surface there.  Test relaxed to "endpoint returns a well-formed list"; connect accepts the port string verbatim.
  3. `DeviceService.is_available` means "a driver is loaded", not "a connection is open".  After the first connect, `disconnect()` is idempotent (200, not 409) on every subsequent call — pinned with a regression test.
  4. Pyright + `pytest.approx` — strict mode flags `approx()` as partially-unknown because pytest doesn't ship strict stubs.  Left as Pylance warnings (mypy is happy); switching to manual `abs(a-b) < ε` would have hurt readability.
- **Verification**: `pytest tests/e2e --no-cov` → **27 passed in 14 s** (was 11) · `pytest tests/unit/` → **530 passed, 92.17 % coverage** (gate 90 %; was 485 / 91.76 %) — `fake.py` at 100 % · mypy clean (0 issues across all new files) · ruff/format clean.
- **Patterns to reuse**:
  - **Env-gated test drivers** — register synthetic implementations behind one env var; the rest of the production code can't see them.
  - **Mocked-at-the-boundary, not mocked-at-the-API** — the fake driver implements the *driver* interface, so `DeviceService` + API + UI all run unchanged.  This is what let the Save-Position regression test catch the original `async def` closure bug, which lived above the driver boundary where any API-level mock would have hidden it.
  - **Hybrid REST + UI tests** — REST-only tests are ~10× faster than browser tests; reserve Playwright for actual UI click paths.  `connected_gps` makes the REST-connect a one-liner.

### 2026-05-26 — Playwright E2E Test Harness
Stood up a real-browser end-to-end test suite that boots the live SP-Base server in a subprocess and drives the NiceGUI front-end with headless Chromium.

- **Why**: unit tests cover REST endpoints + services exhaustively but cannot detect NiceGUI page-mount regressions, button-handler wiring failures, WebSocket hydration breakage, or Quasar→backend round-trip bugs. The 2026-05-26 survey-save dialog bug (silent `async def`-in-`ui.row()` failure) was exactly this class of regression and would not have been caught by `tests/unit`.
- **What landed**:
  - New `tests/e2e/` package (4 files, 11 tests): subprocess-server `conftest.py` fixture, navigation smoke tests for all 6 pages + `/api/health`, destinations REST CRUD lifecycle with UI verification, and a real-button-click "Save Settings" toast assertion.
  - Dev deps `pytest-playwright`, `playwright`, `httpx` added; chromium installed into `~/.cache/ms-playwright`.
  - `pyproject.toml`: `e2e` pytest marker; `tests/e2e` excluded from coverage and from default test discovery so `pytest tests/unit -q` stays fast.
  - `.github/workflows/ci.yml`: new `e2e` job after the unit-test matrix, with `~/.cache/ms-playwright` cached on `uv.lock`. `build` job now depends on `[test, e2e]`.
  - `docs/e2e-testing.md`: architecture diagram, gotchas, local-run instructions, selector best-practices, and a prioritised roadmap (data-testids on dialog inputs, visual regression, `--tracing=retain-on-failure`, cross-browser).
  - Cline MCP `~/.vscode-server/.../cline_mcp_settings.json`: installed `@playwright/mcp` server for ad-hoc dev-time browser driving (not used by pytest or CI).
- **Two gotchas resolved** (documented in `tests/e2e/conftest.py`):
  1. **NiceGUI's pytest sniff** — `helpers.is_pytest()` checks `PYTEST_CURRENT_TEST`; inherited from the parent process it flipped `ui.run()` into screen-test mode and crashed on `KeyError: 'NICEGUI_SCREEN_TEST_PORT'`. Fixture now `env.pop`s `PYTEST_CURRENT_TEST`, `PYTEST_VERSION`, `PYTEST_XDIST_WORKER`, `NICEGUI_USER_SIMULATION` before `Popen`.
  2. **Pipe-buffer deadlock** — first iteration used `subprocess.PIPE` for stdout without an active reader; the uvicorn worker eventually blocked on writes. Fixture now redirects combined stdout+stderr to a log file in the session tmp dir; the tail is embedded in the `TimeoutError` raised by the health-check helper so CI failures aren't blind.
- **Verification**: `pytest tests/e2e --no-cov -ra` → **11 passed in 7.4 s** · `pytest tests/unit -q` → **485 passed, 91.76 % coverage** · ruff/pyright clean · mypy clean (1 documented `# type: ignore[redundant-cast]`).
- **Patterns to reuse**:
  - Subprocess the app, don't import it — NiceGUI's module-level state isn't safe to tear down inside one pytest process.
  - Strip `PYTEST_*` env vars from any subprocess that imports NiceGUI.
  - Log subprocess stdout/stderr to a file, never to an unread `PIPE`.
  - Prefer `get_by_role()` > `data-testid` > `text=` selectors.
  - Mark e2e tests with `@pytest.mark.e2e`; honour the `norecursedirs` exclusion so unit-test runs stay sub-15-second.
- **Roadmap items deferred**: `data-testid` props on dialog inputs in `outputs.py`/`survey.py`/`gps_config.py` (would let us drive the full create-destination dialog through Playwright instead of REST+UI hybrid); Playwright tracing in `pyproject.toml`; visual regression with `to_have_screenshot()`; Survey-In save-position workflow e2e (needs mocked GPS service); Firefox + WebKit in CI matrix.

### 2026-05-26 — Survey Save-Position Dialog Bug Fix
Fixed silent failure on the **Save Position Profile** dialog (Survey-In page).

- **Symptom**: clicking **Save** in the dialog did nothing — no notify, no log entry, no persistence, no dialog close. Cancel button worked.
- **Root cause**: in `src/sp_rtk_base/ui/pages/survey.py` the original `_save_position_dialog()` was `async def` with a nested `async def _do_save()` defined inside a `with ui.row():` slot context manager. NiceGUI 3.x silently drops exceptions raised in this exact closure shape. `_load_saved_dialog` had the same outer-async-for-no-reason shape.
- **Fix**: `_save_position_dialog` and `_load_saved_dialog` converted from `async def` → `def`; `_do_save` lifted out of the nested `ui.row()` slot into dialog-function scope as a plain `def`; its body wrapped in `try/except Exception` with `logger.exception(...)` + `ui.notify(..., type="negative")` so any future failure is loud, not silent. (`_load_saved_dialog`'s inner `_pick` legitimately stays `async` for `_commit_fixed_base`.)
- **Regression test**: `test_save_screenshot_values_persists_to_disk` added to `TestConfigServiceBasePositions` in `tests/unit/test_base_positions.py` — uses the exact values from the bug report (`lat=32.7329015, lon=-117.2362788, alt=27.940, accuracy_mm=47308.0`) against a real `ConfigService` + `tmp_path` YAML, asserting in-memory state, on-disk YAML content, and roundtrip via a fresh service instance.
- **Lesson learned**: in NiceGUI 3.x, never define `async def` handler closures inside slot context managers (`with ui.row():`, `with ui.card():`, etc.) nested under another already-running async handler. Lift them to the enclosing function scope and always wrap UI handler bodies in `try/except` with `logger.exception` + `ui.notify(..., type="negative")` to keep silent failures impossible.
- **Verified**: `ruff check` ✅ / `ruff format --check` ✅ / `pyright src/sp_rtk_base` 0 errors (1 unrelated pre-existing `contextmanager` deprecation warning) / `pytest tests/unit -q` **483 passed, 91.73 % coverage** (up from 480).
- **Files**: `src/sp_rtk_base/ui/pages/survey.py`, `tests/unit/test_base_positions.py`.

### 2026-05-20 — v0.2.0 published to PyPI (first real release) 🚀
First end-to-end publish of `sp-rtk-base` succeeded.

- **PyPI**: <https://pypi.org/project/sp-rtk-base/0.2.0/>
  (Trusted Publisher, OIDC — no API tokens stored anywhere).
- **GitHub Release**: <https://github.com/rodenj1/sp-rtk-base/releases/tag/v0.2.0>
  with sdist, wheel, and sigstore `.sigstore.json` attestations attached.
- **Release run**: `26206746030` — all 9 jobs green
  (verify-version → lint → 4×test → build → publish-pypi → sign).

Workflow validated the full pipeline:
`verify-version` ↔ `pyproject.toml` ↔ `__init__.py` ↔ `uv.lock` ↔
git tag ↔ wheel filename ↔ PyPI metadata ↔ sigstore bundle.

Three workflow-discovered fixes landed during the release:
1. **CI → CODECOV_TOKEN** — switched from OIDC tokenless to the
   universal token-based path because Codecov requires explicit repo
   activation before OIDC works (we got "Repository not found"
   responses; switching to the token unblocked uploads and badge
   rendering).  See `docs/ci-setup.md` for the migration recipe in
   either direction.
2. **`.gitleaks.toml`** — allowlisted `CHANGELOG.md`; `cz bump`
   regenerates that file from git history and can legitimately
   surface old scrub strings from historical commit subjects.
3. **`tests/unit/test_main.py::test_version_importable`** — replaced
   hardcoded `"0.1.0"` assertion with a SemVer regex so future `cz
   bump` runs don't break the pre-push test suite.

Operator setup performed once:
- Created GitHub environment `pypi` (id `15617363293`, no protection
  rules) via `gh api -X PUT /repos/rodenj1/sp-rtk-base/environments/pypi`.
- Registered Pending Trusted Publisher on PyPI with
  project=`sp-rtk-base`, owner=`rodenj1`, repo=`sp-rtk-base`,
  workflow=`release.yml`, environment=`pypi`.
- Added repository secret `CODECOV_TOKEN`.

Per-release recipe (next time):

```bash
uv run cz bump          # bumps version + regenerates CHANGELOG + tags
git push origin main
git push origin --tags
gh release create vX.Y.Z --generate-notes
```

### 2026-05-20 — CI / Release Pipeline + Pre-commit + Conventional Commits
Added the full publish-grade tooling stack adapted from `sp-rtk-base-relay`:

**Workflows** (`.github/workflows/`):
- **`ci.yml`** — 4 sequential jobs on every push/PR to `main`:
  1. `pre-commit` — runs the full hook suite on every file (skips heavy
     pyright/pytest hooks; those are dedicated jobs).
  2. `lint` — `ruff check` + `ruff format --check` + `mypy --strict` +
     `pyright` strict + advisory `pylint --exit-zero`.
  3. `test` — Python 3.10 / 3.11 / 3.12 / 3.13 matrix with coverage and
     JUnit XML, uploads to Codecov via OIDC tokenless (public repo).
  4. `build` — `uv build` (sdist + wheel) artifact sanity.
- **`release.yml`** — triggered by `release: published`:
  `verify-version` (tag ↔ pyproject) → `lint` → `test` matrix → `build`
  + `twine check` + artifact-version verify → `publish-pypi` via PyPI
  Trusted Publishing (OIDC, env `pypi`) → `github-release-assets`
  (sigstore signing + attach `.tar.gz` / `.whl` / `.sigstore` to the
  GitHub Release).  All third-party actions pinned to full commit SHAs.

**Local tooling** (`.pre-commit-config.yaml`):
- pre-commit stage: whitespace, EOF, YAML, TOML, large-files, ruff
  lint + format, gitleaks.
- commit-msg stage: commitizen (Conventional Commits 1.0.0).
- pre-push stage: pyright strict + pytest unit suite (no-cov).

**Configuration** (`pyproject.toml`):
- Added PyPI metadata: `keywords`, `classifiers`, `urls`,
  improved `description`.
- `[tool.commitizen]` with `cz_customize` extending Angular type
  list with `release` and `security`; version files include
  `src/sp_rtk_base/__init__.py:__version__`.
- mypy strict mode for all source files with overrides for the
  NiceGUI UI layer (`sp_rtk_base.ui.*`) — pyright remains canonical.
  Global `warn_unused_ignores = false` because most existing
  `# type: ignore[...]` comments target pyright codes.
- Ruff: dropped `black`; selected `B`, `UP`, `N`, `SIM`, `RUF` on
  top of `E/W/F/I`.  Per-file ignores for FastAPI `Depends(...)`
  (`B008`), u-blox `_CONST_LIKE` locals (`N806`), and NiceGUI
  UI pages (`B`, `SIM`, `N806`).
- Coverage gate raised to `--cov-fail-under=90`; UI pages and the
  `config_audit` CLI excluded from coverage (un-testable headless).

**Documentation**:
- `docs/ci-setup.md` — workflow design + Codecov OIDC setup runbook.
- `docs/release-process.md` — per-release checklist + Trusted
  Publishing one-time setup.
- `CHANGELOG.md` — Keep-a-Changelog format with an `Unreleased` entry
  describing this CI/release work.
- README badges (CI, Codecov, PyPI version, Python versions, license,
  ruff, Conventional Commits).

**Code-quality cleanup** (driven by the new ruff/mypy strict gates):
- Moved late `from typing import Protocol` to the top of
  `device_service.py`.
- Fixed an `N806` clash in `ublox.py` (`key_name` re-bound to
  `str | None`) by introducing a separate `mapped_key` local.
- Added pyright suppressions on the dynamic NiceGUI position-pick
  closure in `ui/pages/survey.py`.
- Guarded an unreachable `Literal` fallthrough in
  `config_models.py` with `# pragma: no cover` + `# type: ignore[unreachable]`.

**Verification**:
- `uv run ruff check .` — all checks pass.
- `uv run ruff format --check .` — 76 files OK.
- `uv run mypy src` — 0 errors (39 source files).
- `uv run pyright src` — 0 errors, 1 unrelated `contextmanager`
  deprecation warning.
- `uv run pytest tests/unit` — **480 passed**, **91.73 % coverage**
  (gate is 90 %).
- `uv run pre-commit run --all-files --hook-stage pre-push` — all
  hooks pass including pyright + full pytest suite.

**Operator action required** (one-time, before first PyPI release):
1. Register the PyPI Trusted Publisher: project `sp-rtk-base`, owner
   `rodenj1`, repo `sp-rtk-base`, workflow `release.yml`,
   environment `pypi` (see `docs/release-process.md`).
2. Create the GitHub environment `pypi` in repo Settings.
3. Link the repo at <https://app.codecov.io/> (OIDC starts working on
   the next CI run; no secret needed).
4. Install pre-commit hooks locally:
   `uv run pre-commit install --install-hooks --hook-type pre-commit --hook-type commit-msg --hook-type pre-push`.

### 2026-05-20 — Switched `sp-rtk-base-relay` to Published PyPI Dependency
- Deleted local `packages/sp-rtk-base-relay/` directory; relay package is now consumed from PyPI (`sp-rtk-base-relay==2.1.1`).
- Regenerated `uv.lock`: relay source switched from `editable = "packages/sp-rtk-base-relay"` → `registry = "https://pypi.org/simple"`. Workspace manifest no longer lists `sp-rtk-base-relay`.
- Transitive bumps: pydantic 2.12.5→2.13.4, pyright 1.1.408→1.1.409, pytest 9.0.2→9.0.3, pyubx2 1.2.60→1.3.0, uvicorn 0.42.0→0.47.0, etc.
- Verified: **480 unit tests pass**, **91.74% coverage**, **pyright 0 errors** (strict).
- Dev tip: to test unreleased relay changes locally, `uv add --editable /path/to/sp-rtk-base-relay` then `git checkout -- pyproject.toml uv.lock && uv sync` to restore.

### 2026-05-15 — Top-Level Package Rename `sp-base` → `sp-rtk-base`
The web-UI/API package was renamed end-to-end:
- Distribution: `sp-base` → `sp-rtk-base`; import package: `sp_base` → `sp_rtk_base`; source dir moved via `git mv`
- Console scripts: `sp-base` / `sp-base-gps-audit` → `sp-rtk-base` / `sp-rtk-base-gps-audit`
- Config dir: `~/.config/sp-base/` → `~/.config/sp-rtk-base/`; env var `SP_BASE_CONFIG` → `SP_RTK_BASE_CONFIG`
- Prometheus namespace + all `sp_base_*` gauges renamed to `sp_rtk_base_*` (input, dest, active/total destinations, chunks, frames). Relay-engine gauges (`sp_rtk_base_relay_*`) unchanged.
- Updated: `pyproject.toml`, all 50+ Python files, README, all docs, all memory-bank files, docker compose + caster sourcetable agent
- **Operator action required**: rename Grafana/PromQL queries from `sp_base_*` → `sp_rtk_base_*`; manually move `~/.config/sp-base/` → `~/.config/sp-rtk-base/` if you want to preserve existing config; rename GitHub repo + working dir + git remote post-commit
- Verified: `uv sync` clean (`sp-rtk-base==0.1.0` built), **480 unit tests pass, 91.74% coverage**, **pyright 0 errors/0 warnings** (strict), grep confirms zero remaining `sp-base`/`sp_base`/`SP_BASE` tokens outside `packages/` and `.venv/`.

### 2026-05-14 — Relay Package Rename `sp-base-relay` → `sp-rtk-base-relay`
The embedded relay-engine package was renamed; all sp-base references updated:
- 7 src files, 5 test files, 4 docs, 6 memory-bank files
- Prometheus gauges `sp_base_relay_running` / `sp_base_relay_uptime_seconds` renamed to `sp_rtk_base_relay_*` (literal, no namespace prefix). Other gauges (`sp_rtk_base_input_*`, `sp_rtk_base_dest_*`) keep configurable `namespace`.
- **Operator action required**: rename Grafana/PromQL queries referencing the old metric names.
- Verified: `uv sync` clean, **480 unit tests pass**, **pyright 0 errors**, grep confirms zero remaining `sp_base_relay`/`sp-rtk-base-relay` references outside `packages/`.

## Completed Phases

### Phase 1 — Project Scaffold ✅
- FastAPI + NiceGUI application structure
- UV package management with pyproject.toml
- Basic app entry point, health endpoint, dark-themed UI layout
- Initial unit tests for app and main

### Phase 2 — Service Layer ✅
- `ConfigService`: YAML-based config persistence (~/.config/sp-rtk-base/config.yaml)
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
  - `sp_rtk_base/cli/config_audit.py` — reads all u-blox Gen9 config keys from RAM and factory-default layers via CFG-VALGET, reports differences
  - 40+ configuration groups: UART/USB/I2C/SPI ports & protocols, TMODE, SIGNAL, RTCM, NMEA, NAVSPG, rates, power mgmt, etc.
  - Uses pyubx2 `UBX_CONFIG_DATABASE` for complete key coverage; fresh UBXReader per poll to avoid buffer corruption
  - Human-readable formatting: enum maps, annotations explaining each change, `--json` output, `--show-same` flag
  - Bundled as `sp-rtk-base-gps-audit` console_scripts entry point in pyproject.toml
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
- **Unit tests**: 530 passed (was 485)
- **E2E tests**: 39 passed (was 27) — Playwright Chromium, FakeGpsDriver-backed
- **Integration tests**: 20+ (end-to-end + destination management + NTRIP)
- **Coverage**: 92.17 % (`fake.py` at 100 %; UI/CLI excluded — NiceGUI can't be unit tested)
- **Pyright / mypy**: 0 errors (strict mode)
- **Python**: 3.10+ compatible
- **API endpoints**: 35 (health, relay, destinations, settings, events, metrics, config, device×19)
- **CLI tools**: `sp-rtk-base` (web app), `sp-rtk-base-gps-audit` (config audit)
- **UI pages**: 6 (Dashboard, Input, Outputs, Survey-In, Settings, Advanced GPS)
- **GPS drivers**: 2 (`ublox` always, `fake` only when `SP_RTK_BASE_FAKE_GPS=1`)

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
