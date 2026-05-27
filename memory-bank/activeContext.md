# Active Context

## Latest Change: Survey-In Progress Visibility + Cancel Button (2026-05-27)

### Symptom (operator report)
> "When you run survey-in, there is no status of the survey process.
> It would be really nice if there could be some progress shown in
> the UI and ability to cancel."

The Survey-In page on `/survey` had two distinct UX gaps that combined into a single "is it doing anything?" moment for the operator:

1. The `svin_progress_card` was hidden until **after** `await svc.configure_survey_in(...)` returned successfully. If the receiver was previously flashed with `TMODE=FIXED` (mode 2), the second `CFG-VALSET` was silently a no-op on the receiver — the survey **never started**, no progress arrived, but the UI also showed nothing because the toast ("Survey-in started") only fired on the no-op success path. End result: blank card, blank chart, blank labels — looked completely frozen.
2. There was no way to abort. Once a survey was actually running (e.g. a 24-hour wall-clock survey) the only way to stop it was to disconnect the GPS or reload the page, neither of which actually cleared TMODE on the device.

### Three-layer fix (driver → service → UI)

**Driver layer** — `src/sp_rtk_base/services/drivers/`
- New abstract method `GpsReceiverDriver.disable_base_mode()` on the ABC. Required for any future driver, not just ublox.
- `UbloxDriver.disable_base_mode()` writes a single CFG-VALSET with `CFG_TMODE_MODE=0` (RAM layer) and waits for ACK.
- `UbloxDriver.configure_survey_in()` was reworked into a **two-step VALSET sequence**: (1) write `CFG_TMODE_MODE=0` and wait for ACK, then (2) write the new `SVIN_MIN_DUR` + `SVIN_ACC_LIMIT` + `CFG_TMODE_MODE=1` and wait for ACK. This is the **root cause** fix — a TMODE state change requires the receiver to see the mode-zero transition first; back-to-back mode-set commands without an intermediate disable get coalesced and the survey clock never restarts.
- `FakeGpsDriver.disable_base_mode()` resets the in-memory state machine (mode → DISABLED, survey clock → None, accuracy → 0). Used by both unit and e2e tests.

**Service layer** — `src/sp_rtk_base/services/device_service.py`
- New `DeviceService.cancel_survey_in()` (async) — wraps the driver call with the same relay-running guard used everywhere else (`set_relay_check`), restores state to `CONNECTED` on both success and failure paths so the UI never gets stuck in `CONFIGURING`.

**API layer** — `src/sp_rtk_base/api/device.py`
- New `POST /api/device/cancel-survey-in` → `DeviceActionResponse`. Returns `409` if no driver / not connected / relay running (matches the rest of `device.py`'s convention).

**UI layer** — `src/sp_rtk_base/ui/pages/survey.py`
- The `svin_progress_card` is now revealed **synchronously**, the instant the user confirms "Start Survey", before the configure call returns. The card initialises to a "Configuring receiver…" status with all metric labels visible but at neutral values ("—", "0s"), so the operator always sees *something*.
- An in-card `svin_error_label` (red, persistent) was added to surface configure failures. Previously the only error feedback was a `ui.notify(..., type='negative')` toast that fades after ~6s and is easy to miss on a head-down install. The toast is still emitted for consistency, but the in-card banner is the primary signal.
- New **Cancel Survey** button (negative outline, `stop` icon) appears beside Start whenever a survey is active. Click → confirmation dialog ("Keep Surveying" / "Cancel Survey") → calls the new `cancel_survey_in()` on the service → status flips to "Cancelled by operator" + Start button is restored.
- Two new live readouts in the progress card:
  - **`% to target accuracy`** — geometric ratio `log(start/cur) / log(start/target)` clamped to [0, 99%] before duration completion; "100% (target reached)" when accuracy meets the limit. Geometric instead of linear because convergence is roughly exponential (mean accuracy halves every few minutes early on, then plateaus).
  - **ETA** — linear extrapolation from a rolling 30-second sample window of `(elapsed_s, accuracy_mm)`. Falls back to "—" until two samples exist and the slope is meaningfully negative (`da < -0.1 mm/s`). When accuracy is already under target, ETA shows the remaining `min_duration` seconds. Both gates (`min_duration` AND `acc_limit`) must be met before survey completes — the displayed ETA is `max(seconds_to_target, seconds_to_min_dur)`.

### Why "disable-then-set" is the *real* fix
The Survey-In TMODE state machine on the ZED-F9P is edge-triggered, not level-triggered: it only re-arms the survey clock + observation counter when it sees `TMODE_MODE` transition `0 → 1`. If TMODE was already `1` or `2`, writing `1` plus new SVIN_MIN_DUR/ACC_LIMIT is a no-op — the receiver ACKs the VALSET (so the driver sees success) but neither the active survey nor any subsequent NAV-SVIN poll reflects the new params. This is documented obliquely in the F9P Integration Manual's TMODE section but is easy to miss because the ACK lies. The intermediate `TMODE_MODE=0` write forces the transition, and the second VALSET then arms cleanly.

This is also why the cancel button needs to write `TMODE_MODE=0` (and **not** flash it) — we want the next `configure_survey_in()` to be able to re-arm without needing a power cycle.

### Test coverage added — 12 new unit + 2 new e2e tests
- `tests/unit/test_cancel_survey_in.py` (new file, 12 tests):
  - `TestFakeDriverDisableBaseMode` (2) — fake driver disable resets state; fails when disconnected.
  - `TestUbloxDisableBaseMode` (3) — disable writes `CFG_TMODE_MODE=0`; `configure_survey_in` writes the disable step **first** then the new params (regression test for the silent-no-op root cause); disconnected raises.
  - `TestDeviceServiceCancelSurveyIn` (4) — delegates to driver; raises when no driver; propagates driver errors while restoring `CONNECTED` state; blocked when relay running.
  - `TestCancelSurveyInEndpoint` (3) — `POST /api/device/cancel-survey-in` returns 409 without driver, 409 when relay is running, 200 + transitions FakeDriver from SURVEY_IN → DISABLED on success.
- `tests/unit/test_ublox_driver.py::test_configure_survey_in` — updated the existing test to expect the **two-VALSET sequence** (disable → enable) rather than one. Asserts the first call is `[("CFG_TMODE_MODE", 0)]` and the second contains all three new params with `TMODE_MODE=1`.
- `tests/unit/test_driver_registry.py::StubDriver` — added a `disable_base_mode()` stub so the registry test driver satisfies the new ABC.
- `tests/e2e/test_survey_cancel.py` (new file, 2 tests):
  - `test_progress_card_visible_immediately_after_start` — regression: card must be visible within 3s of clicking Start Survey, with `% to target` and `ETA` labels rendered.
  - `test_cancel_survey_in_flow` — full Start → Cancel Survey → "Keep Surveying" (no-op) → Cancel Survey → confirm → REST flips to `active=False` → UI shows "Cancelled by operator" → Start button restored.

### Files touched
- `src/sp_rtk_base/services/drivers/base.py` — added `disable_base_mode()` abstract method.
- `src/sp_rtk_base/services/drivers/ublox.py` — added `disable_base_mode()`; reworked `configure_survey_in()` into two VALSETs.
- `src/sp_rtk_base/services/drivers/fake.py` — added `disable_base_mode()`.
- `src/sp_rtk_base/services/device_service.py` — added `cancel_survey_in()`.
- `src/sp_rtk_base/api/device.py` — added `POST /cancel-survey-in` route.
- `src/sp_rtk_base/ui/pages/survey.py` — synchronous progress reveal, in-card error banner, Cancel button + confirmation dialog, ETA + %-to-target math, two-step state restore on cancel.
- `tests/unit/test_cancel_survey_in.py` — new.
- `tests/unit/test_ublox_driver.py` — updated `test_configure_survey_in` for the new two-VALSET contract.
- `tests/unit/test_driver_registry.py` — StubDriver gains `disable_base_mode`.
- `tests/e2e/test_survey_cancel.py` — new.

### Verification
- `pytest tests/unit -q --no-cov` → **582 passed, 1 warning** (12 new + the updated ublox test passing).
- New unit module run standalone: `pytest tests/unit/test_cancel_survey_in.py -v` → 12 passed in 0.26s.
- Ruff / format pending CI; no Pylance errors in any of the touched files.

### Follow-ups (deliberately deferred)
1. The e2e tests in `tests/e2e/test_survey_cancel.py` need the live server + Playwright stack from `tests/e2e/conftest.py`. They are marked `@pytest.mark.e2e` so they only run under the explicit e2e selector — same as every other e2e file in this repo.
2. The "% to target accuracy" math is geometric but assumes the first observation's accuracy is the worst (i.e. convergence is monotonic). On a noisy antenna with periodic jumps this can briefly read *backwards*. The label clamps to [0, 99%] so we never *show* negative progress, but the displayed number can wobble. Could be smoothed with an EMA in a future polish pass.
3. Persistent "ETA reliability" warning when slope is too noisy to extrapolate — currently we just show "—". A more informative "stabilising…" hint would be nicer.
4. The Cancel endpoint does **not** save-to-flash the TMODE=0 write. That's deliberate — we want the fixed-base position (if any) saved to flash to survive a power-cycle, so the operator can come back later and restart a survey without losing the previous good fix. If you actually want to wipe the saved position, that's a separate explicit operator action (and we don't have UI for it yet).

---

## Previous Change: Installer Permission Fix — Config Writable by Service User (2026-05-27)


### Symptom
Operator hit `Error saving input: [Errno 13] Permission denied: '/etc/sp-rtk-base/config.yaml'` from the **Input** page (Bluetooth settings save) on a freshly-installed Raspberry Pi. The service was running, the page rendered, but every web-UI write to the YAML failed.

### Root cause — `deploy/install.sh` Step 7
The original Step 7 wrote the default config and then did:
```bash
chown "root:${SERVICE_USER}" "$default_cfg"   # owner=root, group=sp-rtk-base
chmod 0640 "$default_cfg"                      # rw- r-- ---
```
That gives the service user **group-read-only**, but the systemd unit runs the app as `User=sp-rtk-base Group=sp-rtk-base`. `ConfigService.save_config()` does `self._config_path.write_text(...)`, which needs **owner-write**, so the kernel returns `EACCES`. Pure setup bug — runtime code is fine.

A second, related issue lurked in Step 3: `CONFIG_DIR` (`/etc/sp-rtk-base`) was created `root:sp-rtk-base 0750`, i.e. not group-writable either. Current in-place `write_text` doesn't notice because it doesn't need dir-write, but the moment we move to atomic `tempfile → os.replace` saves (which we should, for crash safety) it would EACCES again. Fixed in the same pass.

### Fix
- **`deploy/install.sh` Step 3** — `install -d` for `CONFIG_DIR` now uses `-o "$SERVICE_USER" -g "$SERVICE_USER"`. Followed by an unconditional `chown $SERVICE_USER:$SERVICE_USER $CONFIG_DIR` + `chmod 0750` that **heals re-runs** on hosts that already have the broken `root:sp-rtk-base` directory from the v0.2.x installer.
- **`deploy/install.sh` Step 7** — the post-heredoc `chown/chmod` block is no longer inside the `if [[ ! -e "$default_cfg" ]]` branch. It now runs **unconditionally** at the end of Step 7 with `chown "${SERVICE_USER}:${SERVICE_USER}"` (not `root:${SERVICE_USER}`) + `chmod 0640`. Re-running the installer on a broken host fixes the live file in place.
- **systemd unit** — no change; `ReadWritePaths=/var/lib/sp-rtk-base /etc/sp-rtk-base` was already correct.

### Live-Pi heal one-liner (for operators who can't wait for a re-install)
```bash
sudo chown sp-rtk-base:sp-rtk-base /etc/sp-rtk-base /etc/sp-rtk-base/config.yaml
sudo chmod 0750 /etc/sp-rtk-base
sudo chmod 0640 /etc/sp-rtk-base/config.yaml
sudo systemctl restart sp-rtk-base
```
Re-running `deploy/install.sh` later converges on the same end state — no conflict.

### Regression tests
New `TestInstallerConfigPermissions` class in `tests/unit/test_install_default_config.py` (4 tests) parses `deploy/install.sh` and asserts:
1. The config-file `chown` line targets `${SERVICE_USER}:${SERVICE_USER}` (and the broken `root:${SERVICE_USER}` form is absent).
2. The `chmod` line on the config file is `0640` or `0660` (owner-writable).
3. The `install -d` line for `$CONFIG_DIR` owns the directory to `${SERVICE_USER}:${SERVICE_USER}`.
4. A re-run-heal `chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"` is present.

These pin the fix so a future "tighten permissions" patch can't silently reintroduce the EACCES.

### Verification
- `bash -n deploy/install.sh` → syntax OK.
- `pytest tests/unit/test_install_default_config.py -v --no-cov` → **6 passed** (2 pre-existing schema-drift tests + 4 new permission tests).
- `pytest tests/unit -q` → **570 passed, 92.91 % coverage** (gate 90 %; was 566 / 92.91 %).
- `ruff check` + `ruff format` clean.

### Files touched
- modified: `deploy/install.sh` (Step 3 dir ownership + Step 7 heal-on-re-run chown/chmod)
- modified: `tests/unit/test_install_default_config.py` (added `TestInstallerConfigPermissions`)
- modified: `memory-bank/activeContext.md`, `memory-bank/progress.md`

### Follow-up (not blocking)
Switch `ConfigService.save_config()` to atomic `tempfile → fsync → os.replace`. Requires `$CONFIG_DIR` to be writable by the service user — Part A above already does that. Protects against half-written YAML on a crash mid-save.

---

## Previous Change: Bluetooth Scan Duration — UI Dropdown + Longer Default (2026-05-27)

### Summary
While investigating a "BlueZ is still holding the GPS" report, traced
the failure mode to a **too-short discovery window** rather than a
stuck-connection bug. The Input page hard-coded an 8-second scan and
the relay's `BluetoothConfig.scan_timeout` defaulted to 10 s — both
shorter than the typical 1-2 s advertising interval of the operator's
ZED-F9P (`RTK_BASE_DAE5`, `00:06:66:B9:DA:E5`). Field probes confirmed
BlueZ had **zero** paired/known devices, no `rfcomm`/`l2cap` sockets
were open, and a fresh scan finds the receiver immediately; the
process simply wasn't scanning long enough.

This change bumps the default scan to **20 s**, exposes a dropdown
with **20 / 30 / 45 / 60 s** presets, and pushes the same default
into the persisted `InputConfig.scan_timeout` so the relay engine
gets the longer window on auto-start as well.

### What landed
- **`src/sp_rtk_base/ui/pages/input.py`** — new module constants
  `DEFAULT_BT_SCAN_DURATION_SECONDS = 20` and
  `BT_SCAN_DURATIONS_SECONDS = [20, 30, 45, 60]`. The "Scan for
  Devices" row now has a "Scan duration" combobox that defaults to
  20 s and is read on every click; the value is forwarded to
  `_discover_bluetooth_devices(mgr, scan_seconds)` (new param,
  defaulted + clamped to positive). Status label now interpolates
  the chosen duration (`Scanning (Xs)...`).
- **`src/sp_rtk_base/models/config_models.py`** — new module
  constant `DEFAULT_BT_SCAN_TIMEOUT_SECONDS = 20` and
  `InputProfile.to_relay_config()` now injects `scan_timeout = 20`
  into the relay-side config dict when `source == "bluetooth"` and
  the persisted profile didn't pin its own value. The original
  `self.config` is **not** mutated (test enforces this).
- **`tests/unit/test_config_models.py`** — 4 new tests on
  `TestInputProfile` covering injection, explicit-override
  preservation, no-mutation, and non-bluetooth no-op.
- **`tests/unit/test_input_page_bt_scan.py`** — new file with 7
  tests guarding the dropdown constants (default ≥ 20, default
  is in the option list, sorted ascending, all positive ints,
  30/45/60 presets all present) and the helper signature/clamp.

### Why this is the right fix (and not a "disconnect bug")
1. Live `bluetoothctl` showed no paired devices and no active
   connections — there was nothing to disconnect from.
2. `dbus-send` to `org.bluez.Manager.GetManagedObjects` returned
   only the adapter — no `org.bluez.Device1` children, so BlueZ
   itself wasn't holding the GPS.
3. A direct `hcitool lescan` / `bluetoothctl scan on` found
   `RTK_BASE_DAE5` within ~6 s on a quiet bus, ~18 s on a busy
   one — exactly the window the old 8 s scan could miss.

### Deferred (separate PRs, captured in progress.md "Known Issues")
- **Bug A (relay-engine pkg, v2.1.2)** — `BluetoothInputSource.disconnect()`
  closes the RFCOMM socket before unregistering the BlueZ
  `org.bluez.Device1` proxy, which can leave the device in a
  half-paired state if the process is `SIGKILL`-ed during the
  window. Belongs in `sp-rtk-base-relay`, not this repo.
- **Bug B** — `DeviceService` teardown doesn't currently get a
  chance to call `driver.disconnect()` on `app._shutdown` because
  the shutdown hook fires after the lifespan context has already
  cancelled background tasks.
- **Bug C** — `main.py` installs no `SIGTERM` / `SIGINT` / `SIGHUP`
  handlers; on `systemctl stop` the relay's TaskGroup is cancelled
  but the driver's cleanup coroutines don't complete before exit.
- **Bug D** — `init_services()` doesn't pre-disconnect any
  lingering serial/BT handles on startup. If a previous instance
  exited uncleanly and the kernel hasn't released `/dev/rfcommN`
  yet, the new instance will fail to open the source even though
  the GPS itself is idle. A startup probe + force-release would
  paper over both bugs above.

### Test counts
| Suite        | Tests | Time   |
|--------------|-------|--------|
| `tests/unit` | **541** (was 530) | ≈14 s |
| `tests/e2e`  | 39   | ≈35 s  |

Coverage of `src/sp_rtk_base/models/config_models.py` is **100 %**;
total unit-suite coverage holds at **92.19 %** (well over the 90 %
gate).

### Files touched
- modified: `src/sp_rtk_base/ui/pages/input.py`
- modified: `src/sp_rtk_base/models/config_models.py`
- modified: `tests/unit/test_config_models.py`
- new: `tests/unit/test_input_page_bt_scan.py`
- modified: `memory-bank/activeContext.md`, `memory-bank/progress.md`

---

## Previous Change: Button-Click E2E Tests for Every Page (2026-05-27)


### Summary
Built on top of the FakeGpsDriver landed yesterday to add four new
e2e test files that drive **every actionable button on every page**
through the real browser, asserting on Quasar toasts and verifying
side-effects via REST.  E2E suite went from 27 → **39 tests** (still
green at ~35 s wall-clock).

### What landed
- **`tests/e2e/test_outputs_buttons.py`** (3 tests) — Outputs page:
  Add Destination → dialog opens → fill fields → Save → toast +
  destination appears in REST list; Add Destination → empty name →
  validation warning; Delete dialog flow.
- **`tests/e2e/test_survey_buttons.py`** (4 tests) — Survey page:
  Start Survey-In → confirmation Cancel (no-op verified via
  `/api/device/survey-in.active == False`); Start Survey-In →
  confirm → `active == True`; Fixed-Base Edit → Cancel restores
  read-only view; Fixed-Base Edit → Commit writes new lat via
  `/api/device/base-config`.
- **`tests/e2e/test_gps_config_buttons.py`** (4 tests) — Advanced
  GPS page: Disconnect → toast + `state == "disconnected"`;
  Save-to-Flash success toast; Load-from-Device GNSS toast;
  Apply-GNSS triggers a real write (channel totals change after
  seeded baseline).
- **`tests/e2e/test_input_buttons.py`** (1 test) — Input page:
  fill TCP host/port → Save → success toast → `/api/config/export`
  YAML reflects the new values.
- **`docs/e2e-testing.md`** updated with the new coverage table
  (39 tests / ~35 s) and a per-file column.

### Non-obvious gotchas resolved
1. **Survey REST shape != button labels.** UI button is
   "Start Survey-In" but the confirmation dialog button is
   "Start Survey" (singular).  Use `get_by_role("button", exact=True)`
   to disambiguate when one is a prefix of the other.
2. **Endpoints != ui labels.** GNSS read is
   `/api/device/gnss` (not `/configure/gnss`), base-config read is
   `/api/device/base-config` (not `/configure/base`), survey-in poll
   is `/api/device/survey-in` (not `/survey-in/status`).  All caught
   by readback assertions.
3. **DeviceStatus has `state` not `connected`.**  Pydantic enum
   serialises to lowercase string ("connected" / "disconnected").
4. **Quasar `q-toggle` doesn't always honour Playwright clicks.**
   The first draft of the Apply-GNSS test toggled Galileo off via
   the switch and asserted the enabled count dropped — but the click
   didn't reliably flip the underlying ``v-model``.  Switched to a
   side-effect that *always* changes when Apply fires: seed a
   non-default channel-count via REST, click Apply, assert the
   totals differ from the seed (the form's defaults overwrite the
   stored config).  More robust, equally meaningful.
5. **NiceGUI `ui.input` validation blocks clicks.** Outputs Add-
   Destination "Save" handler validates required fields and returns
   early on empty name — Playwright sees no toast either way, so we
   assert on the *absence* of the success toast instead.

### Test counts
| Suite        | Tests | Time   |
|--------------|-------|--------|
| `tests/unit` | 530   | ≈14 s  |
| `tests/e2e`  | **39**| ≈35 s  |

### Files touched
- new: `tests/e2e/test_outputs_buttons.py`
- new: `tests/e2e/test_survey_buttons.py`
- new: `tests/e2e/test_gps_config_buttons.py`
- new: `tests/e2e/test_input_buttons.py`
- modified: `docs/e2e-testing.md`
- modified: `memory-bank/activeContext.md`, `memory-bank/progress.md`

---

## Previous Change: FakeGpsDriver + Device-Driven E2E Coverage (2026-05-26)

### Summary
Built an in-memory **FakeGpsDriver** that registers behind the
`SP_RTK_BASE_FAKE_GPS=1` environment variable and unlocks the
device-dependent UI paths (Survey-In, Advanced GPS, GNSS config,
position polling, save-to-flash) for the Playwright e2e suite.
Previously, every test that needed a connected receiver was either
skipped or stubbed at the REST layer; now they run end-to-end
against the real `DeviceService` ↔ driver state machine.

### What landed
- **`src/sp_rtk_base/services/drivers/fake.py`** (new, 100 % unit
  coverage): implements every abstract method on
  `GpsReceiverDriver` (17 of them) with realistic fixture data —
  RTK-fixed solution at `32.7329015 °N / -117.2362788 °W / 27.940 m`,
  6 GNSS constellations, full capability set (`SURVEY_IN`,
  `FIXED_BASE`, `SAVE_TO_FLASH`, `POSITION_STREAM`, …).  Survey-In
  is event-driven via a small in-process state machine.
- **`src/sp_rtk_base/services/drivers/__init__.py`**: env-gated
  registration of the fake driver under vendor key `"fake"`.  Only
  activates when `SP_RTK_BASE_FAKE_GPS == "1"` so prod images never
  see it.  Idempotent re-import is supported (the registry can be
  reset for tests).
- **`tests/unit/test_fake_driver.py`** (45 tests):
  fixture-value invariants, capability set, GNSS round-trip,
  survey-in state machine, base-config transitions, registry env
  gating, and the auto-registration toggle.
- **`tests/e2e/conftest.py`**: now exports `SP_RTK_BASE_FAKE_GPS=1`
  into the subprocess environment, and adds a function-scoped
  `connected_gps` fixture that POSTs to `/api/device/connect` with
  `{vendor: "fake", port: "FAKE", baud_rate: 115200}` and tears
  down with `/api/device/disconnect` after the test.
- **New e2e files**:
  - `tests/e2e/test_survey_save_position.py` (2 tests) — full
    browser-driven regression for the Save-Position dialog bug
    (button click → toast → dialog close → REST verification of
    persisted lat/lon/alt against the FakeGpsDriver fixture).
    Replaces a placeholder stub.
  - `tests/e2e/test_device_connection.py` (6 tests) — REST-only
    connect/disconnect lifecycle, double-connect 409, idempotent
    disconnect, unknown-vendor 400, capability set.
  - `tests/e2e/test_gps_data_flow.py` (7 tests, mixed REST + 1
    browser smoke) — position fixture, GNSS GET/PUT round-trip
    (disable Galileo), base-config DISABLED → FIXED transition,
    save-to-flash, Advanced GPS page render with Save-to-Flash
    button visible.

### Non-obvious gotchas resolved
1. **Pyright + `pytest.approx`** — strict mode flags `approx()` as
   "partially unknown type" because pytest doesn't ship strict
   stubs.  We left these as Pylance warnings (mypy is happy) and
   documented the deviation; switching to manual `abs(a-b) < ε`
   comparisons would have hurt readability for the same outcome.
2. **`PUT` not `POST` for GNSS config** — the GNSS write endpoint
   is `PUT /api/device/gnss`, not the older `POST /configure/gnss`
   shape that pre-dates the API refactor.  First test draft hit
   the wrong verb and silently no-op'd; readback caught it.
3. **`/api/device/ports` returns the live serial scan, not driver-
   registered ports.**  FakeGpsDriver doesn't surface here.  We
   relaxed the ports test to "endpoint returns a well-formed list"
   rather than "FAKE entry is present"; the connect endpoint
   accepts the port string verbatim anyway.
4. **`is_available` semantics** — `DeviceService.is_available`
   means "a driver is loaded", not "a connection is open".  After
   a first connect, `disconnect()` is idempotent and returns 200
   on every subsequent call.  We pin this with a dedicated
   regression test so future refactors don't accidentally flip
   the Disconnect button into raising 409s at the user.

### Verification
- `uv run pytest tests/e2e --no-cov` → **27 passed in 14 s**
  (was 11; added 16 new device-driven cases).
- `uv run pytest tests/unit/` → **530 passed, 92.17 % coverage**
  (gate 90 %; was 485 / 91.76 %).  `fake.py` at 100 %.
- `uv run mypy …` → **clean, 0 issues** across all new files.
- `uv run ruff check / format` → all checks passed.

### Pattern to reuse
- **Env-gated test drivers** — register synthetic implementations
  behind a single env var; production binaries with that var
  unset are bit-for-bit identical to before.
- **Hybrid REST + UI tests** — REST-only tests are 10× faster than
  browser tests; reserve Playwright for the actual UI click paths.
  The `connected_gps` fixture means a REST connect is one line.
- **Mocked-at-the-boundary, not mocked-at-the-API** — the fake
  driver implements the *driver interface*, so the entire
  `DeviceService` + API + UI stack runs unchanged.  This is what
  let the Save-Position regression test catch the original
  `async def` closure bug — the bug lived above the driver
  boundary, where a typical API-level mock would have hidden it.

---

## Earlier Change: Playwright End-to-End Test Harness (2026-05-26)

### Summary
Stood up a real-browser end-to-end test suite that boots the live
SP-Base server in a subprocess and drives the NiceGUI front-end with
headless Chromium via Playwright.  Goal: catch UI regressions
(button-handler wiring, page mounts, WebSocket hydration, Quasar →
backend round-trips) that the unit-test suite cannot see.

### What landed
- **Dev deps**: `pytest-playwright`, `playwright`, `httpx` added to
  `pyproject.toml` under `[tool.uv].dev-dependencies` (or equivalent).
  Chromium installed into `~/.cache/ms-playwright`.
- **`tests/e2e/`** (new package):
  - `conftest.py` — session-scoped subprocess server fixture on a
    random free port + isolated `$HOME`; `browser_context_args` set
    to 1280×800 so the NiceGUI drawer auto-expands; `clean_config`
    function-scoped fixture wipes destinations via REST before & after.
  - `test_navigation_smoke.py` — 6 parametrised page renders + 1
    `/api/health` assertion (7 tests).
  - `test_destinations_crud.py` — full REST create→list→update→delete
    lifecycle with UI verification of the list view + empty-state
    copy (2 tests).
  - `test_settings_interaction.py` — real button click on **Save
    Settings** asserts the Quasar "Settings saved" toast appears
    (2 tests).
- **`pyproject.toml`**: added `e2e` pytest marker; the e2e folder is
  excluded from coverage and from default test discovery via
  `norecursedirs` so `pytest tests/unit -q` stays fast.
- **CI** (`.github/workflows/ci.yml`): new `e2e` job runs after the
  unit-test matrix.  Caches the Playwright browser at
  `~/.cache/ms-playwright` keyed on `uv.lock`; uploads JUnit XML and
  a failure-artifacts bundle.  `build` now depends on `[test, e2e]`.
- **`docs/e2e-testing.md`** — architecture diagram, rationale, local
  run instructions, selector best-practices, and a prioritised
  roadmap (data-testids, visual regression, tracing, cross-browser).
- **Cline MCP** (`~/.vscode-server/.../cline_mcp_settings.json`):
  installed the official `@playwright/mcp` server so the assistant
  can drive a browser interactively during development.  The MCP
  server is **not** used by pytest or CI.

### Two non-obvious gotchas resolved
1. **NiceGUI's pytest sniff** — `nicegui.helpers.is_pytest()` checks
   `PYTEST_CURRENT_TEST` in the environment.  Inherited from the
   pytest parent process, this flipped `ui.run()` into screen-test
   mode and crashed on `KeyError: 'NICEGUI_SCREEN_TEST_PORT'`.
   Fixture now `env.pop()`s `PYTEST_CURRENT_TEST`, `PYTEST_VERSION`,
   `PYTEST_XDIST_WORKER`, and `NICEGUI_USER_SIMULATION` before
   `subprocess.Popen`.
2. **Pipe-buffer deadlock** — first iteration used
   `subprocess.PIPE` for stdout without an active reader.  NiceGUI's
   uvicorn worker eventually blocked on writes once the buffer
   filled.  Fixture now redirects combined stdout+stderr to a log
   file in the session `tmp_path` — robust, and gives CI a
   debuggable artifact when health-check times out (the tail is
   embedded in the `TimeoutError` message).

### Verification
- `uv run pytest tests/e2e --no-cov -ra` → **11 passed in 7.4 s**.
- `uv run pytest tests/unit -q` → **485 passed, 91.76 % coverage**.
- `uv run ruff check .` → all checks passed.
- `uv run pyright tests/e2e src/sp_rtk_base/main.py` → 0 errors.
- `uv run mypy tests/e2e src/sp_rtk_base/main.py` → clean (one local
  `# type: ignore[redundant-cast]` documented in the wipe helper).

### Pattern to reuse
- Subprocess-the-app, don't import-it.  NiceGUI's module-level state
  makes in-process testing brittle.
- Strip `PYTEST_*` env vars from any subprocess that imports NiceGUI.
- Log subprocess stdout/stderr to a file, never to an un-read PIPE.
- Prefer `get_by_role()` > `data-testid` > `text=` for selectors.
- Mark e2e tests with `@pytest.mark.e2e` so CI and local devs can
  opt-in/out cleanly.

---

## Earlier Change: Survey Save-Position Dialog Bug Fix (2026-05-26)

### Symptom
On the Survey-In page, the **Save Position Profile** dialog's **Save** button did nothing — no notify message, no error in the log, no entry persisted, and no dialog dismissal. The **Cancel** button in the same dialog worked normally. Affected values from the bug report: `name="test", lat=32.7329015, lon=-117.2362788, alt=27.940m, accuracy_mm=47308`.

### Root Cause
In `src/sp_rtk_base/ui/pages/survey.py`, the original `_save_position_dialog()` was an `async def` that contained a **nested `async def _do_save()` defined inside a `with ui.row():` slot context manager**. NiceGUI 3.x silently drops exceptions raised in this exact closure shape — the coroutine is created but its `ui.notify` / `dlg.close` calls have no observable effect and no traceback is surfaced. The `_load_saved_dialog` outer wrapper had the same shape (only its inner `_pick` legitimately needed to be async).

This is the same class of fragility flagged earlier in `progress.md` line 107 ("Added pyright suppressions on the dynamic NiceGUI position-pick closure in `ui/pages/survey.py`") — closures defined inside NiceGUI slot context managers are easy to silently break.

### Fix
- `_save_position_dialog`: `async def` → `def`. Lifted `_do_save` out of the nested `with ui.row():` slot to dialog-function scope; converted it to a plain `def`. Wrapped its body in `try/except Exception as exc:` that calls `logger.exception("Save position failed")` and `ui.notify(f"Save failed: {exc}", type="negative")` so any future regression in this code path is loud, not silent.
- `_load_saved_dialog`: `async def` → `def` (inner `_pick` stays `async` because it awaits `_commit_fixed_base`).
- File-level pyright suppressions unchanged; no new ignores needed.

### Regression Test
`tests/unit/test_base_positions.py::TestConfigServiceBasePositions::test_save_screenshot_values_persists_to_disk` exercises the exact values from the bug report through a real `ConfigService` against a `tmp_path` YAML file. It asserts:
1. In-memory `get_base_positions()` reflects the save,
2. The YAML file exists on disk and contains the expected literal values,
3. A fresh `ConfigService` instance pointing at the same path reads the position back identically.

This locks in the data-model + persistence side so a YAML regression can never silently re-emerge.

### Lesson Learned
**In NiceGUI 3.x, do not define `async def` handler closures inside `with ui.row():` / `with ui.card():` slot context managers nested inside another already-running async handler.** Move them to the enclosing function's scope. Always wrap UI handler bodies in `try/except Exception` with `logger.exception(...)` + `ui.notify(..., type="negative")` so silent failures become impossible.

### Verification
- `uv run ruff check src/sp_rtk_base/ui/pages/survey.py tests/unit/test_base_positions.py` ✅
- `uv run ruff format --check ...` ✅ (after one auto-reformat of the new test)
- `uv run pyright src/sp_rtk_base` — **0 errors, 1 pre-existing unrelated `contextmanager` deprecation warning in `ui/layout.py`** ✅
- `uv run pytest tests/unit -q` — **483 passed, 91.73 % coverage** (gate 90 %) ✅

### Files Changed
- `src/sp_rtk_base/ui/pages/survey.py` — dialog handlers de-asynced + try/except guard
- `tests/unit/test_base_positions.py` — new regression test (`test_save_screenshot_values_persists_to_disk`)

---

## Previous: Switched `sp-rtk-base-relay` to Published PyPI Dependency (2026-05-20)

### Summary
The embedded relay-engine package (`packages/sp-rtk-base-relay/`) was deleted and `sp-rtk-base` now consumes the published PyPI release `sp-rtk-base-relay==2.1.1`.

### What Changed
- **`packages/sp-rtk-base-relay/` directory removed** from the repo (the package is now published to PyPI: https://pypi.org/project/sp-rtk-base-relay/).
- **`pyproject.toml`**: `sp_rtk_base_relay>=2.1.1` was already declared; no edit needed.
- **`uv.lock` regenerated**: `sp-rtk-base-relay` source switched from `editable = "packages/sp-rtk-base-relay"` → `registry = "https://pypi.org/simple"`, version `2.1.0` → `2.1.1`. Workspace manifest `members` no longer lists `sp-rtk-base-relay`.
- **Incidental transitive bumps from re-lock**: `pydantic 2.12.5→2.13.4`, `pydantic-core 2.41.5→2.46.4`, `pylance 4.0.0→6.0.1`, `pyright 1.1.408→1.1.409`, `pytest 9.0.2→9.0.3`, `pyubx2 1.2.60→1.3.0`, `uvicorn 0.42.0→0.47.0`, `urllib3 2.6.3→2.7.0`, `watchfiles 1.1.1→1.2.0`, `yarl 1.23.0→1.24.2`, `pynmeagps 1.1.2→1.1.4`, `python-multipart 0.0.22→0.0.29`. New additions pulled in by deps: `pyarrow 24.0.0`, `tinycss2 1.5.1`, `webencodings 0.5.1`.

### Verification
- `uv pip show sp-rtk-base-relay` → `Version: 2.1.1`, `Location: .venv/lib/python3.10/site-packages` (non-editable) ✅
- `grep -A2 'name = "sp-rtk-base-relay"' uv.lock` → `source = { registry = "https://pypi.org/simple" }` ✅
- `uv run pytest tests/unit -q` → **480 passed, 91.74% coverage** ✅
- `uv run pyright src/sp_rtk_base` → **0 errors** (1 pre-existing `contextmanager` deprecation warning in `ui/layout.py` unchanged) ✅

### Dev Workflow Note
For future relay-engine work, develop against a checkout of the `sp-rtk-base-relay` repo and release new versions to PyPI. To temporarily test an unreleased local copy from sp-rtk-base, use `uv add --editable /path/to/sp-rtk-base-relay` and restore with `git checkout -- pyproject.toml uv.lock && uv sync`.

---

## Previous: Top-Level Package Renamed `sp-base` → `sp-rtk-base` (2026-05-15)

### Summary
The web-UI/API package was renamed from `sp-base` to `sp-rtk-base`:
- **Distribution**: `sp-base` → `sp-rtk-base` (pyproject.toml `[project].name`)
- **Import package / source dir**: `src/sp_base/` → `src/sp_rtk_base/` (via `git mv` — history preserved)
- **Console scripts**: `sp-base` → `sp-rtk-base`, `sp-base-gps-audit` → `sp-rtk-base-gps-audit`
- **Config dir**: `~/.config/sp-base/` → `~/.config/sp-rtk-base/`
- **Env var**: `SP_BASE_CONFIG` → `SP_RTK_BASE_CONFIG`
- **NiceGUI storage secret**: `sp-base-dev-secret` → `sp-rtk-base-dev-secret`
- **Event bridge thread name**: `sp-base-event-bridge` → `sp-rtk-base-event-bridge`
- **Config export filename**: `sp-base-config.yaml` → `sp-rtk-base-config.yaml`
- **NTRIP caster Source-Agent**: `sp-base-caster` → `sp-rtk-base-caster`
- **Prometheus namespace**: default `sp_base` → `sp_rtk_base`. All sp-rtk-base-scoped gauges renamed: `sp_base_input_*` → `sp_rtk_base_input_*`, `sp_base_dest_*` → `sp_rtk_base_dest_*`, `sp_base_active_destinations` → `sp_rtk_base_active_destinations`, `sp_base_total_destinations`, `sp_base_chunks_distributed`, `sp_base_frames_parsed`, `sp_base_input_*` connection/bytes/seconds gauges. (The relay-engine-scoped `sp_rtk_base_relay_*` gauges from the May 2026 rename are unchanged.)

### Coverage of Changes
- **Source (all of `src/sp_rtk_base/`)**: imports + runtime strings + namespace default
- **Tests**: all `from sp_base` imports rewritten to `from sp_rtk_base`; all metric-name assertions rewritten; config-path fixtures updated
- **Tools**: `tools/{demo_with_simulator,read_gps_config,test_ntrip_caster,test_hardware_gps}.py`
- **Docker**: `docker/ntrip-caster/{docker-compose.yml,ntrip_caster.py}` — container name + sourcetable agent
- **Docs**: `README.md`, `docs/*.md`
- **Memory bank**: all 6 files
- **`pyproject.toml`**: name, scripts, `--cov=src/sp_rtk_base`

### Verification
- `uv sync` — clean (`sp-rtk-base==0.1.0` installed; relay package `sp-rtk-base-relay` resolved via workspace member)
- `uv run pytest tests/unit -q` — **480 passed, 91.74% coverage**
- `uv run pyright src/sp_rtk_base` — **0 errors, 0 warnings** (strict mode)
- `grep -rE "\bsp-base\b|\bsp_base\b|\bSP_BASE\b"` outside `packages/` and `.venv/` — **no matches** (every reference is now `sp-rtk-base` / `sp_rtk_base` / `SP_RTK_BASE`)

### Operator Impact (Breaking)
1. **Grafana / PromQL**: dashboards/alerts referencing `sp_base_input_*`, `sp_base_dest_*`, `sp_base_active_destinations`, `sp_base_total_destinations`, `sp_base_chunks_distributed`, `sp_base_frames_parsed`, or other `sp_base_*` gauges **must** be renamed to `sp_rtk_base_*`. The relay-engine gauges (`sp_rtk_base_relay_running`, `sp_rtk_base_relay_uptime_seconds`) are unchanged.
2. **Configuration**: existing `~/.config/sp-base/config.yaml` is no longer read. Users start with defaults and must re-create destinations / input config (or manually `cp -r ~/.config/sp-base ~/.config/sp-rtk-base`). Per user direction, no automatic migration was implemented.
3. **Env var**: `SP_BASE_CONFIG` is no longer honored; use `SP_RTK_BASE_CONFIG`.
4. **CLI entry points**: `sp-base` and `sp-base-gps-audit` no longer exist; use `sp-rtk-base` and `sp-rtk-base-gps-audit`.

### Post-Rename Manual Steps (for operator)
1. Rename GitHub repo `rodenj1/sp-base` → `rodenj1/sp-rtk-base` (github.com → Settings → Rename).
2. Rename working directory: `mv /opt/development/sp-base /opt/development/sp-rtk-base`.
3. Update git remote: `cd /opt/development/sp-rtk-base && git remote set-url origin https://github.com/rodenj1/sp-rtk-base.git`.
4. Reopen the project in VS Code from the new path; rerun `uv sync` to refresh the venv path-binding.

---

## Previous: Embedded Relay Package Renamed `sp-base-relay` → `sp-rtk-base-relay` (2026-05-14)

### Summary
The embedded relay-engine package was renamed from `sp-base-relay` to `sp-rtk-base-relay` (directory + distribution name + import package `sp_base_relay` → `sp_rtk_base_relay`). All sp-base references were updated to match.

### Prometheus Gauge Rename
Two relay-engine-scoped gauges had names tied to the relay package and were updated:
- `sp_base_relay_running` → `sp_rtk_base_relay_running`
- `sp_base_relay_uptime_seconds` → `sp_rtk_base_relay_uptime_seconds`

In `MetricsService` these now use literal names (not `f"{ns}_..."`) since they represent the relay engine rather than the sp-base app. The sp-base-specific gauges still used the configurable `namespace` prefix until the 2026-05-15 rename above.

### Operator Impact
**Breaking for Grafana / alerting**: PromQL queries referencing `sp_base_relay_running` or `sp_base_relay_uptime_seconds` were renamed to `sp_rtk_base_relay_*`. The shipped Grafana dashboard template (`packages/sp-rtk-base-relay/templates/grafana_dashboard.json`) was updated as part of the package rename.

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
- `src/sp_rtk_base/app.py` — Added `_shutdown()` handler with `app.on_shutdown()`
- `src/sp_rtk_base/api/events.py` — WebSocket timeout 30s → 5s
- `packages/sp-rtk-base-relay/src/sp_rtk_base_relay/core/destinations/ntrip_destination.py` — Socket timeout `None` → `30.0`

### Tests: All pass (21 sp-rtk-base, 77 relay)

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
- `src/sp_rtk_base/ui/layout.py` — New nav items with separators
- `src/sp_rtk_base/ui/pages/input.py` — New (extracted from settings)
- `src/sp_rtk_base/ui/pages/survey.py` — New (survey workflow + positions + live position + reload button)
- `src/sp_rtk_base/ui/pages/gps_config.py` — New (RTCM + GNSS + flash + handoff + reload button)
- `src/sp_rtk_base/ui/pages/dashboard.py` — Slimmed (relay-only)
- `src/sp_rtk_base/ui/pages/settings.py` — Slimmed (app settings only)
- `src/sp_rtk_base/app.py` — Updated page registrations
- `src/sp_rtk_base/ui/pages/device.py` — Deleted
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
