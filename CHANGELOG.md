# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Commit messages follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/);
the changelog can be regenerated automatically via `uv run cz bump`.


Baseline release; not yet published to PyPI.

## v0.3.0 (2026-05-27)


- feat(survey): add Cancel button, progress visibility, ETA/% readouts
- Operators reported the Survey-In page froze with no feedback after
- clicking Start. Two root causes:
- 1. UI: the progress card was hidden until the configure RPC returned,
-    so the page looked dead while the receiver was actually surveying.
- 2. Driver: u-blox TMODE state machine is edge-triggered. Writing
-    TMODE_MODE=1 with new SVIN params on a receiver already in TMODE=1
-    silently ACKs without restarting the survey clock — re-runs of
-    Start would appear to do nothing.
- Fix (three layers):
- - Driver: new GpsReceiverDriver.disable_base_mode() abstract method;
-   UbloxDriver writes CFG_TMODE_MODE=0; configure_survey_in() now sends
-   disable-first-then-enable so the survey clock always restarts;
-   FakeGpsDriver.disable_base_mode() resets the state machine for tests.
- - Service + API: DeviceService.cancel_survey_in() (async, relay-guard,
-   state-restore on failure) + POST /api/device/cancel-survey-in
-   returning 200/409.
- - UI (/survey): progress card now reveals synchronously the moment
-   Start is confirmed; in-card red error banner replaces toast-only
-   failure surface; new Cancel Survey button + 'Cancel Survey-In?'
-   confirmation dialog (Keep Surveying / Cancel Survey); two new live
-   readouts — % to target accuracy (clamped geometric ratio) and ETA
-   (linear slope over rolling 30 s window, '—' until slope is
-   meaningfully negative).
- Deliberately NOT save-to-flash on cancel: preserves any prior
- fixed-base position across power cycles.
- Tests: 12 new unit tests in test_cancel_survey_in.py + 2 new Playwright
- tests in test_survey_cancel.py (progress-card-visible-immediately
- regression + full Start → Cancel flow). 582 unit + 41 e2e all green;
- ruff clean.
- fix(deploy): heal config-yaml ownership for service user writes
- The v0.2.x installer created /etc/sp-rtk-base/ as root:sp-rtk-base 0750
- and config.yaml as root:sp-rtk-base 0640 (group-readable only). The
- service runs as sp-rtk-base, so atomic-rename saves of config.yaml from
- the web UI (Bluetooth, input settings, destinations, etc.) failed with
-   PermissionError: [Errno 13] Permission denied:
-     '/etc/sp-rtk-base/config.yaml'
- Fix:
- - Create CONFIG_DIR owned by SERVICE_USER:SERVICE_USER from the start.
- - Always re-apply chown SERVICE_USER:SERVICE_USER + chmod 0640 on the
-   default config (even when contents are left untouched) so a re-run
-   of the installer heals pre-existing root-owned installs without
-   requiring operators to hand-chown the file.
- Operators on a broken Pi can also heal in place with:
-   sudo chown sp-rtk-base:sp-rtk-base /etc/sp-rtk-base /etc/sp-rtk-base/config.yaml
-   sudo chmod 0750 /etc/sp-rtk-base && sudo chmod 0640 /etc/sp-rtk-base/config.yaml
-   sudo systemctl restart sp-rtk-base
- Regression test added in test_install_default_config.py.
- docs(memory-bank): record v0.2.2 PyPI publish + uv.lock-on-cz-bump gotcha

## v0.2.2 (2026-05-27)


- Merge pull request #4 from rodenj1/fix/bluetooth-lifecycle-and-scan-duration
- fix(bluetooth): tighten lifecycle (shutdown, SIGHUP, startup recovery) + scan tuning
- fix(bluetooth): tighten lifecycle (shutdown disconnect, SIGHUP, startup recovery) + scan tuning
- Closes the four-bug audit triggered by the May 27 'system still holding the Bluetooth GPS' report.
- Bug B - DeviceService.disconnect() in shutdown: app.py promotes _shutdown to module-level shutdown_services(); order is device -> event bridge -> relay; device wrapped in asyncio.wait_for(timeout=10s).
- Bug C - SIGHUP handler in main.py: _install_sighup_handler() forwards SIGHUP -> SIGTERM via os.kill so systemctl reload uses the same teardown as SIGINT/SIGTERM. No-op on Windows.
- Bug D - Startup pre-disconnect: services/__init__.py _release_stale_bluetooth_handle(mac) calls BluetoothManager.disconnect_device(mac) off-loop (5s budget), invoked before start_relay() when source==bluetooth. Lazy import keeps CI/macOS happy.
- Bug A - Already shipped upstream as sp-rtk-base-relay 2.1.2 (PR rodenj1/sp-rtk-base-relay#7). Pin bumped: sp_rtk_base_relay>=2.1.1 -> >=2.1.2; uv.lock regenerated.
- BT scan duration UX: input.py exposes 20/30/45/60s dropdown (default 20s); config_models.py injects scan_timeout=20 into relay config when not pinned by the profile.
- Tests: 25 new (4 new files + 4 InputProfile cases). Full suite: 566/566 passing, 92.91% coverage. ruff + pyright strict clean.

## v0.2.1 (2026-05-27)


- feat(e2e): add Playwright button-click tests for every page
- Add FakeGpsDriver (env-gated on SP_RTK_BASE_FAKE_GPS=1) plus
four new e2e test files driving every actionable button on the
Outputs, Survey, Advanced-GPS, and Input pages.  Tests assert
the Quasar toast in the real browser AND verify the side-effect
via REST so UI handler-wiring regressions cannot silently slip
through.
- - src/sp_rtk_base/services/drivers/fake.py: in-memory driver
  implementing all 17 GpsReceiverDriver methods with realistic
  RTK-fixed fixture data (100% unit coverage, 45 new tests).
- tests/e2e/: 12 new tests across 4 files (outputs/survey/gps-
  config/input button click flows), plus connect/disconnect
  lifecycle + GPS data-flow REST suites.  E2E suite: 27 -> 39.
- docs/e2e-testing.md: architecture + gotchas + per-file
  coverage table.
- Unit: 530 passed, 92.17% coverage.  E2E: 39 passed in ~35s.
- fix(deploy): add plugdev to service supplementary groups for USB-serial
- Raspberry Pi OS Bookworm + recent udev rules assign FTDI / CP210x / CH340 USB-serial adapters to root:plugdev rather than root:dialout.  A service that was only in dialout therefore got EACCES on /dev/ttyUSB0 even though the historic 'add user to dialout' fix was correctly applied:
-   crw-rw----+ 1 root plugdev 188, 0 /dev/ttyUSB0
- Reported in the field as 'Connection failed: Failed to open /dev/ttyUSB0: [Errno 13] Permission denied' when selecting a u-blox receiver on the Input page.
- * deploy/sp-rtk-base.service - add plugdev to SupplementaryGroups (plus inline comments explaining what each group is for)
- * deploy/install.sh - add plugdev to the usermod -aG loop and update the success log line
- * docs/deployment-pi.md - update the symptom table entry, the systemd-unit snippet, the 'what gets configured' bullet, and the top-of-doc paragraph to all mention plugdev
- Live-Pi recovery: sudo usermod -aG plugdev sp-rtk-base && sudo systemctl restart sp-rtk-base
- feat(deploy): enable Bluetooth at install time + troubleshooting docs
- Raspberry Pi OS Bookworm ships with Bluetooth rfkill-soft-blocked.  Combined with systemd-rfkill restoring state across reboots and (on NM 1.42+) NetworkManager re-asserting an rfkill block from its own state file, a fresh Pi will silently refuse Bluetooth scans even though bluez and the hci0 adapter look healthy.  The Input -> Bluetooth scan in sp-rtk-base then fails with org.bluez.Error.NotReady.
- * deploy/install.sh - new Step 7.6 runs rfkill unblock bluetooth || true and sets BluetoothEnabled=true in /var/lib/NetworkManager/NetworkManager.state (creating the key if missing), then reloads NetworkManager.  Both are best-effort and a no-op when rfkill / NetworkManager aren't present, so the installer still works on non-Pi Debian hosts.
- * docs/deployment-pi.md - new four-step 'Bluetooth scan finds nothing' troubleshooting entry covering: rfkill + saved-state diagnosis, the unblock + NetworkManager.state fix the installer applies, an rfkill-bluetooth=ignore NetworkManager drop-in (fleet-bulletproof fallback documented at networkmanager.dev/docs/rfkill/), and the rfkill.default_state=1 kernel-cmdline last resort.  Also added a row to the existing symptom table that points at the new section.
- Verified live on larson-base (Pi 4): scan returns SetDiscoveryFilter success, rfkill list bluetooth shows Soft blocked: no, and /var/lib/systemd/rfkill/*bluetooth* contains no :1 entries after a full reboot cycle.
- fix(deploy): correct installer default config schema + add drift guard
- Production crash on 2026-05-26: deploy/install.sh wrote a default config with the wrong field names (input.source_type / tcp_host / tcp_port) which failed AppConfig validation and prevented the service from starting.
- * deploy/install.sh - heredoc now emits a schema-correct, minimal default (settings.metrics_enabled, destinations: [], base_positions: []).  No input: block — the operator picks one from the Input page on first launch and the YAML is rewritten then.
- * deploy/install.sh - new Step 7.5 runs ConfigService().load_config() as the service user with SP_RTK_BASE_CONFIG pointed at the default file and dies with a clear error if validation fails, so future schema drift is caught at install time.
- * tests/unit/test_install_default_config.py - new regression test extracts the heredoc body from deploy/install.sh with a regex, parses it as YAML, and round-trips it through AppConfig.model_validate(...).  Asserts metrics_enabled is true, destinations + base_positions are empty, and input is None.  Catches drift in CI on any future PR that touches either the installer or the model.
- * docs/deployment-pi.md - Default-config snippet updated to match the new installer output; added a note explaining why there is intentionally no input: block.
- feat(deploy): add Raspberry Pi systemd installer + runbook
- Production deployment kit for Pi / Debian targets:
- * deploy/install.sh - idempotent installer.  Creates sp-rtk-base system user (dialout + bluetooth groups, no shell), /opt/sp-rtk-base/venv/, /etc/sp-rtk-base/config.yaml (0640, root:sp-rtk-base), /var/lib/sp-rtk-base/ (0750), pip-installs from PyPI, symlinks console scripts into /usr/local/bin/, drops the systemd unit, enables + starts.  Re-running upgrades in place; existing config is never overwritten.
- * deploy/sp-rtk-base.service - hardened systemd unit with NoNewPrivileges, ProtectSystem=strict, ProtectHome, PrivateTmp, ReadWritePaths scoped to /etc + /var/lib.  Reads SP_RTK_BASE_CONFIG from /etc/sp-rtk-base/config.yaml.
- * deploy/upgrade.sh - one-line pip install -U + systemctl restart, prints old/new version.
- * deploy/uninstall.sh - interactive (or --purge / --keep-data) removal of service + venv + symlinks, optionally config + state + user.
- * docs/deployment-pi.md - full runbook: filesystem layout, day-2 ops, backup/restore tar recipe, nginx reverse-proxy snippet, ufw rule, sigstore wheel verification, troubleshooting table, fleet management notes.
- * README.md - Quick Start rewritten.  Production install (Pi systemd) is now the lead path; pipx / uv tool covers single-user workstations; from-source moved to a developer note.
- docs(memory-bank): record v0.2.0 PyPI release milestone
- First successful end-to-end release: tag -> verify -> lint -> test matrix -> build -> publish (Trusted Publisher OIDC) -> sigstore -> GitHub Release assets.
- Also documents three workflow-discovered fixes that landed during the release (CODECOV_TOKEN switch, CHANGELOG.md gitleaks allowlist, SemVer-regex version test) and captures the per-release recipe for future bumps.

## v0.2.0 (2026-05-20)


- build(gitleaks): allowlist CHANGELOG.md (auto-generated by commitizen)
- `cz bump` regenerates CHANGELOG.md from git history, which can legitimately surface historical scrub strings (e.g. the real BT MAC referenced in commit 94a38dd's subject 'chore: switch sp-rtk-base-relay to published PyPI 2.1.1; scrub example MAC').
- CHANGELOG.md is human-reviewed on every PR, so allowlisting it follows the same logic already in place for memory-bank/*.md and docs/*.md.  This unblocks future `cz bump` runs from being rejected by the pre-commit gitleaks hook.
- ci(codecov): switch from OIDC tokenless to CODECOV_TOKEN upload
- Codecov's OIDC tokenless upload requires the repo to be activated on app.codecov.io first; the activation step was missing, causing 'Repository not found' on every upload and an empty coverage badge.
- Switch to the universal token-based path (works for public and private repos alike):
- - ci.yml: replace 'use_oidc: true' with 'token: ${{ secrets.CODECOV_TOKEN }}' on both Codecov steps
- - ci.yml: drop the 'id-token: write' permission on the test job (no longer needed)
- - docs/ci-setup.md: rewrite Codecov setup section for token path; keep OIDC migration recipe as an optional appendix
- Operator action required: add CODECOV_TOKEN as a repository secret (Settings -> Secrets and variables -> Actions). Token is shown on the Codecov repo setup page.
- ci: add CI/release workflows, pre-commit, and PyPI publishing setup
- Mirror the sp-rtk-base-relay publishing pipeline:
- - Add .github/workflows/ci.yml (pre-commit, lint, 3.10-3.13 test matrix with OIDC Codecov, build)
- - Add .github/workflows/release.yml (verify-version, lint, test, build+twine, PyPI Trusted Publishing, sigstore, GH Release assets)
- - Add .pre-commit-config.yaml (ruff lint/format, gitleaks, commitizen, pre-push pyright+pytest)
- - Add .gitleaks.toml for secret scanning
- - Add docs/ci-setup.md and docs/release-process.md
- - Add CHANGELOG.md (Keep-a-Changelog format)
- - Update pyproject.toml: PyPI metadata, ruff B/UP/N/SIM/RUF, mypy strict with NiceGUI overrides, 90% cov gate, commitizen config
- - Add README badges (CI, Codecov, PyPI, Python versions, license, ruff, Conventional Commits)
- - Fix strict-mode lint findings in ublox.py, device_service.py, survey.py, config_models.py
- chore: switch sp-rtk-base-relay to published PyPI 2.1.1; scrub example MAC
- - Remove embedded packages/sp-rtk-base-relay (now consumed from PyPI)
- Regenerate uv.lock: relay source = registry pypi.org/simple, version 2.1.1
- Transitive bumps: pydantic 2.13.4, pyubx2 1.3.0, uvicorn 0.47.0, etc.
- Replace example MAC '98:D3:51:FE:FE:E4' (real device) with '00:11:22:33:44:55'
  in ui/pages/input.py placeholder
- Add .vscode/settings.json for consistent pytest/pyright workspace config
- Update memory-bank/{activeContext,progress}.md
- Verified: 480 unit tests pass, 91.74% coverage, pyright 0 errors (strict).
- chore: rename package sp-base -> sp-rtk-base
- - Distribution: sp-base -> sp-rtk-base (pyproject.toml)
- Import package / source dir: src/sp_base/ -> src/sp_rtk_base/ (git mv, history preserved)
- Console scripts: sp-base -> sp-rtk-base; sp-base-gps-audit -> sp-rtk-base-gps-audit
- Config dir: ~/.config/sp-base/ -> ~/.config/sp-rtk-base/
- Env var: SP_BASE_CONFIG -> SP_RTK_BASE_CONFIG
- NiceGUI storage_secret, event bridge thread name, config export filename, NTRIP caster Source-Agent
- Prometheus namespace default sp_base -> sp_rtk_base; all sp_base_* gauges renamed to sp_rtk_base_*
  (input, dest, active/total destinations, chunks, frames). Relay engine gauges sp_rtk_base_relay_* unchanged.
- README, docs/, memory-bank/, docker/ntrip-caster/ updated
- All 480 unit tests pass, 91.74% coverage, pyright strict 0 errors/0 warnings
- BREAKING: Grafana/PromQL queries against sp_base_* must be renamed to sp_rtk_base_*.
BREAKING: Existing ~/.config/sp-base/config.yaml is no longer read; users must recreate config
or manually 'cp -r ~/.config/sp-base ~/.config/sp-rtk-base'.
BREAKING: SP_BASE_CONFIG env var no longer honored; use SP_RTK_BASE_CONFIG.
BREAKING: CLI entry points sp-base / sp-base-gps-audit removed; use sp-rtk-base / sp-rtk-base-gps-audit.
- refactor: rename embedded relay package sp-base-relay → sp-rtk-base-relay
- The embedded relay-engine package directory and distribution name were
renamed (sp-base-relay → sp-rtk-base-relay; import package
sp_base_relay → sp_rtk_base_relay). All sp-base references updated to
match.
- Source/test/doc changes (sed pass):
- src: services/{relay_service,event_bridge,metrics_service,__init__}.py,
       models/config_models.py, ui/pages/{input,settings}.py
- tests: unit/test_{relay_service,event_bridge,metrics_service,
         api_metrics,config_models}.py
- docs: README.md, docs/relay-engine-api-spec.md,
        docs/ublox_gps_webui_planning.md, tools/test_ntrip_caster.py
- memory-bank: all six files updated; activeContext.md + progress.md
  prepended with rename-completion entries
- Prometheus gauge rename (breaking for external dashboards):
- sp_base_relay_running → sp_rtk_base_relay_running
- sp_base_relay_uptime_seconds → sp_rtk_base_relay_uptime_seconds
These two gauges now use literal names in MetricsService rather than
the f'{namespace}_...' template, since they represent the relay engine
rather than the sp-base app. The remaining sp_base_input_* /
sp_base_dest_* gauges still honor the configurable 'namespace' arg.
- .gitignore: nested-package exclusion path updated to
packages/sp-rtk-base-relay/.
- Verified: uv sync clean; pytest tests/unit → 480 passed;
pyright src/sp_base → 0 errors, 0 warnings.
- Updated README
- feat: initial SP-Base web UI + REST API implementation
- - FastAPI app (api/) with endpoints for relay, destinations, settings,
  device, config, metrics, health, and events (WebSocket)
- NiceGUI UI (ui/) with dashboard, destinations, settings, device/GPS
  config, survey, inputs/outputs pages + shared components + validators
- Services layer: ConfigService, RelayService, DeviceService,
  MetricsService, EventBridge
- Pydantic models: api_models, config_models, device_models
- Device drivers: base + u-blox (pyubx2) driver with registry
- CLI: sp-base-gps-audit config dump tool
- Tests: ~70 unit + integration test modules, pytest + coverage config
- Docker: NTRIP caster sandbox for local testing
- Tools: GPS config reader, hardware GPS test, NTRIP test, demo simulator
- Docs: relay-engine API spec, NTRIP caster guide, UI restructuring plan,
  ZED-F9P config reference, GPS webUI planning
- memory-bank/: full project brief, product/tech context, system patterns,
  active context, progress
- .clinerules/ with Development Rules + Memory Bank workflow
- pyproject.toml with uv workspace, sp-base-relay dependency, pyright
  strict mode, pytest+coverage config
- README rewrite describing the UI + API
- packages/sp-base-relay/ is tracked in its own repo and excluded from
  this repo via .gitignore
- Initial commit
