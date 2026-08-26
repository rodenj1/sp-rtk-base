# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Commit messages follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/);
the changelog can be regenerated automatically via `uv run cz bump`.


Baseline release; not yet published to PyPI.

## v0.5.2 (2026-08-26)

- feat(ublox): force stationary dynamics model (DYN_MODEL=2) on base-mode transitions
- Fresh ZED-F9P receivers default CFG_NAVSPG_DYNMODEL to 0 (portable),
which is the wrong nav class for a fixed-mount base station and can
slow survey-in convergence / degrade the averaged position. Add
_apply_stationary_dyn_model_locked(), called from both
configure_survey_in() and configure_fixed_base() after their
mode-specific write succeeds, to write and verify CFG_NAVSPG_DYNMODEL=2
via the existing layer=5 verify-and-retry helper.
- disable_base_mode() deliberately leaves the dynamics model untouched
— rover mode is out of scope for this app, and clearing it would
regress a receiver a prior session already set to stationary.
- Closes #38

## v0.5.0 (2026-08-25)


- feat(ublox): auto-apply RTCM-only UART1/UART2 output profile on base-mode transition
- Fresh ZED-F9P receivers boot with NMEA+UBX output on, flooding the
UART1/UART2 base-station data links with traffic rovers don't need.
Add _apply_base_output_profile_locked(), called from both
configure_survey_in() and configure_fixed_base() after their
mode-specific write succeeds, to write and verify a separate
layer=5 RTCM-only OUTPROT profile — with retry-once-then-raise
semantics matching the existing verify-and-retry pattern in
disable_base_mode().
- Closes #40
- fix(ublox): write ECEF alongside LLH so fixed-base RTCM actually emits
- The ZED-F9P base engine requires a valid 3D position in ECEF before it
will generate RTCM corrections. configure_fixed_base() wrote only the
LLH TMODE keys, leaving CFG_TMODE_ECEF_X/Y/Z at their 0,0,0 default —
TMODE_MODE=2 and the RTCM message selection both ACK cleanly, but the
base engine never engages and no RTCM frames are emitted.
- Derive ECEF from the same WGS84 LLH input and write it alongside the
LLH keys, then verify the read-back matches what was written (not
merely non-zero, which would let a stale ECEF from a prior config
pass silently) before reporting success, retrying the write once.
- Closes #39.

## v0.4.2 (2026-08-14)


- release: bump 0.4.1 -> 0.4.2
- fix(net-provision): captive-portal auto-pop via NM's own dnsmasq, not a dead custom DNS server
- NetworkManager's ipv4.method shared on the setup-AP profile spawns its
own dnsmasq bound to the AP's specific gateway IP. Linux's UDP socket
demux always prefers that specific-address bind over sp-rtk-base's own
WildcardDnsServer (bound to 0.0.0.0), so every real client's DNS query
went to NM's dnsmasq instead — which has no upstream and just times
out, so the OS captive-portal sign-in prompt never fires.
- Confirmed live on larson-base: NM's dnsmasq log showed "no servers
found in /etc/resolv.conf", and the portal received zero HTTP requests
for ~100s after the AP came up (the first hit was a manual GET /, not
an OS probe path).
- Fixes it by feeding NM's own dnsmasq an `address=/#/<ap_gateway_ip>`
line via an install-time /etc/NetworkManager/dnsmasq-shared.d/ drop-in
— the process that actually wins now answers every domain with the
AP's IP. Removes the now-dead WildcardDnsServer/dns_responder.py and
the portal_dns_port config field.
- Closes #34.

## v0.4.1 (2026-08-14)


- release: bump 0.4.0 -> 0.4.1
- fix(net-provision): stop setup-AP flapping on a noisy connectivity read
- Excludes NetworkManager's always-active loopback connection from
NmcliAdapter's active-connection set so a hotspot-only host correctly
short-circuits to "no uplink" instead of falling through to a live
connectivity check every tick. Also requires has_uplink to hold for
uplink_confirm_ticks (default 2) consecutive polls before decide()
tears down an active AP, via a new durable consecutive_uplink_ticks
clock mirroring the issue #25 failure-backoff pattern.
- Fixes a field incident on larson-base: with Ethernet physically
unplugged for 5+ minutes, the setup AP cycled on/off every ~12s for
the entire outage, making it impossible for a phone to join it.
- Closes #33.

## v0.4.0 (2026-08-13)


- release: bump 0.4.0-beta.4 -> 0.4.0
- Merge pull request #26 from rodenj1/feat/net-provision-decision-core
- Feat/net provision decision core
- release: bump 0.4.0-beta.3 -> 0.4.0-beta.4
- docs(net-provision): document appliance vs managed-host deployment modes
- Adds a Deployment modes section to README.md and docs/deployment-pi.md
covering the appliance/managed-host behavior matrix, install commands,
the fixed default AP_PASSWORD caveat, mode-switch teardown, and the
planned (out-of-scope) container mode. Removes stale claims that
install always sets up the AP or always requires AP_PASSWORD.
- Closes #32.
- feat(net-provision): multi-mode install — appliance vs managed-host
- Splits the installer into deployment modes so sp-rtk-base can run as a
full-control appliance (NetworkManager takeover, setup-AP, polkit rule,
net-provision unit) or an app-only managed-host install where something
else owns the host's network stack. install.sh requires an explicit
--mode with no default, preserves the recorded mode on bare re-runs,
and tears down the outgoing mode's network artifacts on a switch via a
shared helper also used by uninstall.sh. The app honors deployment.mode
by hiding the console Network page and 404ing /api/network/* outside
appliance mode.
- Closes #28, #29, #30, #31.

## v0.4.0-beta.3 (2026-08-01)


- release: bump 0.4.0-beta.2 -> 0.4.0-beta.3
- fix(net-provision): grant wifi.share.protected polkit action for the setup AP
- nmcli connection up id <ap_ssid> requires this action separately from
network-control to activate a WPA2-PSK shared-mode (hotspot)
connection. Without it, decide() correctly chooses START_AP but the
adapter's nmcli call fails with "Not authorized to share connections
via wifi" every tick, and the setup AP — the only fallback a stranded
device has — never comes up. Found by forgetting the active WiFi
through the Network console page on a real Pi and watching the
supervisor's captured logs.

## v0.4.0-beta.2 (2026-07-31)


- release: bump 0.4.0-beta.1 -> 0.4.0-beta.2
- fix(net-provision): set SP_RTK_BASE_NET_CONFIG on the main app unit
- The Network console page (issues #22-24) reads net-provisioning config
from inside sp-rtk-base.service itself, not just the separate
supervisor unit — without this env var it falls back to a ~/.config
path that doesn't exist for the homeless sp-rtk-base system user, and
every /api/network/* call 502s. Found by deploying 0.4.0-beta.1 to a
real Pi.

## v0.4.0-beta.1 (2026-07-31)


- fix(release): use SemVer-compliant pre-release format (0.4.0-beta.1)
- release: bump 0.3.31 -> 0.4.0b1
- feat(net-provision): add operator console Network page (issues #22-24)
- Adds a Network page to the operator console for viewing and managing
WiFi post-online: read-only status + scan (#22), add/connect a
network including hidden SSID and the on-Ethernet case (#23), and
switch/forget saved networks (#24). Reuses the shared nmcli adapter
extended in #21; connect/switch/forget are fire-and-acknowledge with
an honest session-drop warning, since a WiFi change can drop the very
connection carrying the request.
- feat(net-provision): extend nmcli adapter for console network ops (issue #21)
- Adds the console-facing primitives on top of the shared nmcli adapter
built for provisioning: list saved WiFi profiles, read the current
active link (wired/wifi + IP + signal), activate/switch to a saved
profile, forget/delete a profile (guarded against deleting the setup
AP's own), and hidden-SSID support on the existing connect call. Scan
results gain an explicit in_range flag. One nmcli boundary continues
to serve both the provisioning supervisor and the upcoming console.
- feat(net-provision): add failure-aware retry backoff (issue #25)
- Closes #25. A saved WiFi profile that's visible but unjoinable (wrong
or changed password) was retried on every rescan forever, stealing the
radio from the setup AP each time. decide() now stops returning
STOP_AP_AND_CONNECT once consecutive failures hit a configurable
threshold, holding the AP up instead (still rescanning on schedule, so
saved_wifi_visible stays fresh) until a configurable suppression window
expires.
- NetworkState gains consecutive_connect_failures and
seconds_since_last_connect_failure (decide() inputs) plus
saved_wifi_name (adapter bookkeeping only, not read by decide()) so
read_state()'s single nmcli lookup can double as the key the
supervisor uses to reset the count when the saved network changes,
without a second nmcli round trip. NetProvisionConfig adds
max_connect_failures/failure_suppression_seconds. The supervisor's
tick() catches WifiConnectError from execute() as a handled outcome —
recorded via a new durable field on ProvisioningClockState and the tick
completes normally — rather than letting it propagate and abort clock
persistence the way an unexpected adapter exception still does.
- Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
- feat(net-provision): wire net-provisioning into install/uninstall (issue #11)
- Closes #11. install.sh now ensures NetworkManager is installed and
enabled (warning, not force-fixing, if wlan0 is unmanaged by e.g.
dhcpcd), writes net_provision.yaml from AP_SSID/AP_PASSWORD only if
absent, and creates the setup-AP NetworkManager connection profile
NmcliAdapter activates via `nmcli connection up/down` — all idempotent,
re-running never clobbers a site's provisioned config. AP_PASSWORD has
no default and is required the first time (issue #6 story 8: one fixed
sticker SSID/password for the whole fleet, never baked into source);
the config heredoc escapes it for safe YAML embedding. uninstall.sh
now removes the AP connection profile (parsed via the still-intact
venv's PyYAML before the app tree is removed) unconditionally,
alongside the existing unit/polkit cleanup. Updates deploy/README.md
and docs/deployment-pi.md to match; the curl one-liner is unchanged
apart from the new required env var.
- Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
- feat(net-provision): add WiFi-picker captive portal (issue #10)
- Adds the AP-mode captive portal: a stdlib http.server picker page
backed by the nmcli adapter's cached scan, a wildcard UDP DNS
responder so OS captive-portal detection auto-pops the sign-in
prompt, and Portal start()/stop() wired to the supervisor loop via a
new on_ap_active callback so it tracks AP state without polling
nmcli itself.
- Extends NmcliAdapter with scan_networks()/latest_scan() (scanning
right before the AP comes up, since a single radio can't scan while
serving it) and connect_to_network() for installer-submitted
SSID+password, distinct from the existing saved-profile reconnect
path. Grants CAP_NET_BIND_SERVICE on the systemd unit so the
non-root service user can bind ports 80/53.
- Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
- feat(net-provision): add supervisor loop, entry point, and systemd unit
- Closes #9. Ties decide()/NmcliAdapter together on a poll_interval_seconds
timer, exposed as sp-rtk-base-net-provision. Adds a strict config loader
(fails loudly instead of defaulting, since ap_password has no safe
default) and a durable JSON clock store so seconds_disconnected/
seconds_in_ap survive a service restart instead of resetting the
fallback window or the AP rescan timer. Ships an independent systemd
unit plus the polkit rule the service account needs for nmcli control.
- Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
- feat(net-provision): add nmcli adapter (state in / commands out)
- Implements issue #8: reads NetworkManager state (uplink connectivity,
AP activity, saved WiFi profile presence/visibility) into the shape
decide() consumes, and executes each ProvisionAction via nmcli.
Enforces the two adapter-owned mapping rules from #6/#7's amendments —
excluding the setup AP's own connection from uplink accounting, and
treating NM's `unknown` connectivity as `limited` only when a non-AP
connection is active — plus saved_wifi_visible's non-sticky contract
across AP restarts. Connect failures raise a distinct WifiConnectError
for #25's future retry-backoff to consume.
- Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
- fix(net-provision): read the uplink, not the device's own hotspot
- Code review caught a field-breaking flap. A NetworkManager `shared`
hotspot is itself an active connection, and NM will report `limited`
for a host whose only connection is that hotspot — so the AP branch
saw connectivity, tore down the AP it had just raised, found no
network, raised it again, and flapped faster than an installer's phone
could associate. Stories 2-6 would never complete.
- `NetworkState.connectivity` is now `uplink_connectivity`, defined as
excluding the setup AP's own connection, and the adapter's mapping
rules (#8) are spelled out on `Connectivity`: a hotspot-only host has
no uplink and must report `none`. Regression test asserts a serving AP
is never mistaken for an uplink.
- Also from review:
- * Drop `poll_interval_seconds` — the supervisor loop it belongs to is
  #9, so it was reach beyond this ticket.
* Make `saved_wifi_visible` a hard contract: the adapter must report
  False whenever the AP is (re)started. A stale True would retry a
  failing network every tick and leave no AP window to reconfigure
  through — the wrong-password-after-a-site-change case.
* Give the decision tests a `_state()` factory so each case shows only
  the fields its scenario turns on, and name the thresholds the
  boundary cases read against.
- Refs #7
- feat(net-provision): add pure decide() core and config knobs
- Implements the decision spine of headless field network provisioning
(#7, parent #6): a pure function of observable state that chooses when
to flip a single-radio Pi between WiFi client and setup-AP mode, plus
the tunable knobs a deployment can set without code changes.
- NetworkManager keeps doing all real networking; this layer only decides
*when* to ask it to switch. No I/O, no nmcli, no clock — every rule is
a function of the state the (still to come) adapter hands in, so the
whole AP-versus-client behavior is testable without hardware.
- Behavior notes worth recording:
- * Two clocks, selected by whether a saved WiFi profile exists. An
  unprovisioned unit opens the AP once boot-wait expires, so an
  installer is never left without a way in. A provisioned unit rides
  out the longer fallback window first, so a router reboot doesn't
  take a working site offline.
* seconds_disconnected exists because seconds_since_boot cannot express
  "WiFi has been down five minutes" on a device with three days uptime.
* Any connectivity other than none counts as connected — a LAN-only
  site with an on-premise caster reports 'limited' and is operational.
* Ethernet needs no special case: a working cable simply shows up as
  connectivity. A cable to a dead switch therefore still yields an AP
  after boot-wait rather than an unreachable device.
* ap_password has no default so no unit ships with a hotspot password
  baked into source; the installer writes the sticker value (#11).
- Refs #7
- Merge pull request #2 from rodenj1/renovate/python-dependencies
- chore(deps): update python-dependencies to 0.11.21
- chore(deps): update python-dependencies to 0.11.21
- release: bump 0.3.30 -> 0.3.31
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
- docs: refresh README accuracy and standardize badges
- - Fix stale test counts (480 → 554 unit; 20+ → 3 integration)
- Document 12 Playwright e2e suites and the sp-rtk-base-gps-audit CLI
- Replace stale packages/sp-rtk-base-relay link with PyPI URL
- Drop stale packages/ entry from the project-structure tree
- Standardize badge set, order, labels, and colors with sp-rtk-base-relay
- Clarify pytest default (unit-only) and add explicit e2e invocation
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.30 (2026-06-02)


- feat(relay): enrich start/stop log messages with trigger + context
- Operator question: should there be a log message when the relay starts
and stops?  Yes — they were already there, but terse to the point of
being unhelpful in Loki ("Relay engine started" / "Relay engine
stopped" with no context).
- This release adds a `trigger` parameter to RelayService.start_relay()
and .stop_relay() so each caller declares who/what initiated the
transition, then formats a one-line status snapshot per event.
- New log shape, examples:
-   Relay engine started — trigger=auto-start (attempt 4) input=bluetooth(28:cd:c1:…) destinations=['ntrip-out', 'tcp-5016']
  Relay engine stopped — trigger=shutdown uptime=4h 23m bytes_in=1.2 GB chunks_out=98432 (started by api)
- Trigger values plumbed by callers:
  auto-start (attempt N) — services._auto_start_with_retry
  api                    — POST /api/relay/start, /api/relay/stop
  shutdown               — app.shutdown_services
  handoff                — api/device.py device→relay handoff
  ui                     — Dashboard Start/Stop button
  unknown                — fallback default
- Mechanics:
- Capture monotonic start time + start trigger on each successful start.
- Snapshot final throughput via engine.get_status() BEFORE stopping the
  engine (best-effort; status is gone after stop()).
- Format uptime + bytes with human-readable helpers in the same file.
- For Bluetooth inputs, render only the MAC; for TCP, host:port; for
  serial, the port path.  Avoids dumping the whole config dict into the
  log line.
- Tests added (3):
  test_start_log_includes_trigger_input_and_destinations
  test_stop_log_includes_trigger_and_uptime  (verifies uptime fmt,
    bytes_in formatted to KB, chunks_out, and cross-reference of the
    start trigger)
  test_default_trigger_is_unknown
- Fixed an existing lifecycle test that pinned its side_effect coroutine
to zero args — it now accepts *args, **kwargs to handle the new
`trigger=` kwarg the shutdown path passes.
- 667 → 670 unit tests passing.  ruff / pyright / mypy strict all clean.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.29 (2026-06-02)


- fix(lifecycle): cancel auto-start task on shutdown; bound stop_relay;   fix WebSocket keepalive race after client disconnect
- Two production bugs surfaced from larson-base journal logs:
- 1. systemd SIGKILL after 90s on `systemctl stop`.  shutdown_services
   never cancelled the v0.3.20 `_auto_start_with_retry` background
   task.  If shutdown landed while the task was mid-attempt — inside
   `asyncio.to_thread(engine.start)` doing a blocking Bluetooth /
   serial / NTRIP connect — the orphaned task held the event loop
   open and uvicorn refused to exit.  Combined with
   `relay_service.stop_relay()` waiting on the same in-flight start,
   it blew through systemd's `TimeoutStopSec=90s`.
-    Fix:
   - Cancel `services_mod.auto_start_task` as step 0 of shutdown.
     Cancellation is instant when the task is in `asyncio.sleep`;
     when it's in a blocking thread the asyncio side detaches
     after a 2 s budget (the daemon thread can't be interrupted,
     but it no longer blocks us).
   - Wrap `relay_service.stop_relay()` in `asyncio.wait_for` with a
     15 s budget so a stuck engine teardown can't burn the rest of
     systemd's window.
- 2. WebSocket keepalive raised `RuntimeError: Unexpected ASGI message
   'websocket.send', after sending 'websocket.close'` whenever the
   client vanished between heartbeats.  The handler sent a `{"type":
   "ping"}` JSON without checking connection state.  Pure log noise
   (the connection was already dead), but produced a multi-frame
   traceback per orphaned client per 5 s heartbeat.
-    Fix:
   - Check `websocket.client_state == WebSocketState.CONNECTED`
     before every send.
   - Catch `(WebSocketDisconnect, RuntimeError)` around the
     keepalive send and exit the handler quietly with a DEBUG log.
   - Skip the final `websocket.close()` in the `finally` block if
     the connection is already torn down.
- Tests added:
- TestShutdownCancelsAutoStartTask (3 cases): pending task cancelled,
  completed task left alone, None handled.
- TestRelayStopTimeoutBudget: hanging stop_relay is bounded.
- TestWebSocketEvents.test_websocket_handles_client_abandonment_during_keepalive
- 662 → 667 unit tests passing.  ruff / pyright / mypy clean.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.28 (2026-06-02)


- docs(monitoring): re-fix destination table — joinByField + exclude env
- v0.3.27 switched to the `merge` transformation hoping it would
combine frames by label set; in practice it stacks frames vertically,
producing 6 rows per destination (one per metric) with only one
Value column populated each.
- Switch back to `joinByField` on `destination` (correct join semantics
for one base) and aggressively exclude the duplicate label columns
that outer-join produces: `Time 1`-`Time 5`, `base 1`-`base 5`,
`__name__ 1`-`__name__ 5`, and the `env` external label + its
`env 1`-`env 5` duplicates (revealed by an operator screenshot —
the env label rides along from Alloy's external_labels).
- Caveat noted in the JSON comment: joinByField on a single key will
cartesian if multiple bases share a destination name.  When that
case arises, the upgrade is to switch to `joinByLabels` joining on
both `base` and `destination`.  Defer until multi-base is real.
- No source code, no test changes.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.27 (2026-06-02)


- docs(monitoring): fix Per-Destination Health table column duplication
- Reported: the table panel showed the `base` value six times across
six columns plus six `Time` columns, because the joinByField
transformation suffixes shared columns from each input frame.
- Switched the transformation from `joinByField(byField=destination)`
to `merge`.  Merge combines frames that share their label set
(base + destination) into a single frame with one row per
(base, destination) and one `Value #<refId>` column per metric —
no duplicated label columns to clean up afterwards.
- Also added an `indexByName` block so columns render in a sensible
order: base, destination, connected, bytes_sent, messages_sent,
dropped, errors, queue_depth.
- No metric semantics, no source code, no test changes.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.26 (2026-06-01)


- docs(monitoring): add Logs panel + Loki datasource var to dashboard
- Extends the unified Grafana dashboard with a Logs panel at the bottom
that queries a Loki datasource and stays filtered by the same $base
dropdown as the metrics panels — one dropdown controls both halves
of the operational view.
- Dashboard changes:
- New `$DS_LOKI` template variable (optional; the Logs panel binds
  to it but the rest of the dashboard ignores it).
- New row + Logs panel: `{base=~"$base", service="sp-rtk-base"}`
  LogQL query, descending sort, time stamps on, wrapping on.
- 22 panels total (was 20).
- README additions:
- New "Logs (optional)" section explaining the dashboard's Loki
  expectation and providing two ready-to-paste recipes (Grafana
  Alloy + Promtail) for shipping sp-rtk-base.service logs with
  the required `base` and `service` labels.
- No source code or test changes.  Users without Loki can just delete
the Logs panel after import — the metrics panels render normally
either way.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.25 (2026-06-01)


- docs(monitoring): rename `site` label to `base` (base station)
- Per operator feedback: the Grafana dashboard selector and the
underlying Prometheus label should read as "base station", not "site"
— the project is sp-rtk-**base** and one instance maps to one base
station.  "Site" is a Prom-ecosystem convention but doesn't match the
operator's mental model here.
- Coordinated rename across:
- - Grafana dashboard JSON: `$site` → `$base` template variable,
  display label "Site" → "Base", all 24 PromQL selectors flipped
  from `{site=~"$site"}` to `{base=~"$base"}`, all legendFormat
  references `{{site}}` → `{{base}}`, the joinByField transformation
  rename map, title and prose touched up.
- prometheus-scrape-config.example.yml: `labels: { site: home }` →
  `labels: { base: home }` with updated comments.
- README.md: every `site` reference flipped to `base`; the "Why
  the `site` label" section becomes "Why the `base` label".
- Operators upgrading: rename the label in your Prometheus
`scrape_config` (or your Alloy `external_labels`) from `site` to
`base` and restart the agent.  Old samples in Prom will still exist
under `{site=...}` but new data lands under `{base=...}` — the
dashboard sees only the new label, so the cleanest cutover is to
also drop / retain-with-tombstone any old series via
`/api/v1/admin/tsdb/delete_series` if pre-cutover data isn't needed.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.24 (2026-06-01)


- docs(monitoring): add unified Grafana dashboard + Prom scrape example
- Generic, public-friendly monitoring docs for sp-rtk-base.  Distinct
from the operator-specific deployment recipe that was withdrawn from
v0.3.23 — this release contains only material that's useful to anyone
running sp-rtk-base.
- New files under docs/monitoring/:
- - README.md — overview + quick-start (4 steps from "I have Prom" to
  "I have an sp-rtk-base dashboard").
- grafana-dashboard-sp-rtk-base.json — unified dashboard, schema 39
  (Grafana 11), 14 panels organised across 5 row-divider sections:
    Service Overview     (5 stats: freshness, running, input, dests, uptime)
    Throughput           (input bytes/s, hub frame+chunk rate, watchdog)
    Per-Destination Health (status table with colour-coded connected/
                            errors/queue + human-readable bytes)
    Per-Destination Activity (bytes/s, msgs/s, drops/s, queue depth)
    Reliability          (connection flap history, error rate)
  Templated on a multi-select $site variable populated from
  `label_values(sp_rtk_base_relay_running, site)` so the same
  dashboard works for one or many instances.
- prometheus-scrape-config.example.yml — drop-in scrape_config snippet
  with the required `site:` label and a commented example for adding
  a second instance.
- No source code or test changes.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
- docs: remove monitoring deployment recipe (operator-specific)
- The docs/monitoring/ tree added in f343137 is specific to the
operator's home cluster topology (Traefik IngressRoute, SealedSecrets,
ArgoCD layout) and should not have been published to the public repo.
- This is a forward removal — the content remains reachable at commit
f343137 in history, but no current ref (main, tag, release) points
at it.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
- docs(monitoring): add larson-base deployment recipe with Alloy + Traefik
- Adds docs/monitoring/ — a self-contained deployment recipe for shipping
sp-rtk-base metrics from a remote-site Raspberry Pi (no inbound network
access) to a home Kubernetes cluster's Prometheus, over public HTTPS
with Basic Auth at the Traefik edge.
- Architecture (all templates use YOUR-DOMAIN.example placeholders):
-   Pi:    Grafana Alloy scrapes localhost:8080/metrics, remote_writes
         via HTTPS+BasicAuth with on-disk WAL buffer (24h outage budget).
  Home:  Traefik IngressRoute with exact `Path(/api/v1/write)` match —
         only the remote_write endpoint is exposed; Prom's admin /
         query / config API stays in-cluster. BasicAuth via Middleware
         backed by a SealedSecret. Receiver enabled on the Prom CR
         via kube-prometheus-stack values.
- Files:
- larson-base-deployment.md — combined runbook with verification
  steps and operational expectations.
- alloy/config.alloy.example — Alloy scrape + remote_write config.
- k8s/prom-rw-middleware.yaml — Traefik basicAuth Middleware.
- k8s/prom-rw-ingressroute.yaml — IngressRoute with exact-path match;
  cert-manager OR Traefik resolver TLS options documented inline.
- k8s/prom-rw-basicauth.sealedsecret.yaml.example — shape stub +
  kubeseal regeneration recipe. NOT a real credential (SealedSecrets
  are per-cluster and must be re-generated).
- k8s/values-overlay-snippet.yaml — kube-prometheus-stack values diff
  to enable the remote-write receiver.
- grafana-dashboard-sp-rtk-base.json — 10-panel Grafana dashboard
  templated on $site so the same panels work for multiple remotes.
- No source code or test changes.  Documentation + manifests + dashboard
JSON only.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.22 (2026-06-01)


- feat(dashboard): collapse Start+Stop to one toggle, gate on saved config
- Reported: the Dashboard always showed two buttons (Start and Stop)
with the inactive one greyed out — clutter, and it was not obvious
when the relay engine could actually be started.
- This release collapses the two buttons into one toggle whose
text/icon/color swap based on ``relay.is_running``:
- - Stopped → "Start" (green play_arrow)
- Running → "Stop"  (red stop)
- When stopped AND a hard precondition isn't met (no input source
configured, or zero enabled destinations) the button is disabled
and a "⚠ Configure ..." caption underneath routes the operator to
the page that needs attention:
- - "Configure an input source on the Input page before starting."
- "Add at least one enabled destination on the Outputs page
   before starting."
- The decision logic is extracted into a pure
``_compute_relay_control_state(...)`` function so it's testable
without spinning up NiceGUI.  When the relay is *running* the
button is always enabled regardless of config — operators must be
able to stop a running engine even if destinations were deleted
mid-run.
- Tests:
- - 6 new unit cases over ``_compute_relay_control_state`` covering
  the full (is_running × has_input × dest_count) decision matrix.
- 2 new e2e cases (``test_dashboard_button.py``) that drive the
  real NiceGUI render in a Playwright browser: only one button is
  in the DOM (no leftover "Stop"), and the precondition-failed
  caption + disabled state appear when destinations are empty.
- 662 → 668 unit tests passing, 41 → 43 e2e passing; ruff / pyright /
mypy strict all clean.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
- test(e2e): update Save Position placeholder to match v0.3.17 regex
- The dialog's example placeholder changed from "Office Roof" (space)
to "Office_Roof" (underscore) when v0.3.17 tightened the position-name
regex to ^[A-Za-z0-9_-]+$.  The matching e2e selector was never
updated, so test_save_position_button_persists_profile has been
failing on every CI push since v0.3.17 (release workflow doesn't run
e2e, so the failures went unnoticed across v0.3.18 / v0.3.19 / v0.3.20
/ v0.3.21).
- No production-code change; v0.3.21 is correct on PyPI.  This brings
the CI workflow back to green for the next push.
- Verified locally: full e2e suite now 41/41 passing.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.21 (2026-06-01)


- feat(dashboard): human uptime + rates instead of totals for flow
- Reported pain on a long-uptime instance: the dashboard showed
"120:34:56" hours-unbounded for uptime, and the Input Source +
Throughput cards showed running totals that climb forever.  When
nothing is flowing, a stuck "1.2 GB" total looks identical to one
that's still ticking up — the operator can't tell whether the relay
is actually moving data right now.
- Two changes, both in the dashboard render path:
- 1. Uptime formatter: format with the largest unit first —
   "12s" → "5m 23s" → "4h 23m" → "12d 04h" → "1y 35d".  Drop the
   colon-separated h:mm:ss form which became misleading past 1 day.
- 2. Input Source + Throughput cards: show per-second rate as the
   primary value, with the running total as a small grey subvalue
   underneath.  Rate baseline is per-page-session state held in the
   page closure; it resets when the relay stops or uptime regresses
   (engine restart).  First poll after baseline reset shows "—"
   (no baseline yet to compute a delta from).
- Rate units:
- Bytes:     B/s → KB/s → MB/s → GB/s
- Counts:    "0.50/s" (low) → "10.0/s" (mid) → "100/s" (high)
- Helper extended: status_metric() now accepts an optional `subvalue`
rendered small + muted under the primary value, so other cards can
use the same rate-prominent / total-subordinate pattern later if
useful.
- Tests: 37 new (parameterized over _format_bytes / _format_byte_rate /
_format_count_rate / _format_uptime).  656 total passing, ruff /
pyright / mypy clean.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.20 (2026-06-01)


- fix(auto-start): retry with backoff and surface state on Dashboard
- Reported on larson-base: with auto_start: true and a Bluetooth GPS,
the relay didn't start after a power cycle.  Service log:
-     sp_rtk_base_relay.exceptions.InputSourceError:
        Bluetooth socket connection failed: [Errno 112] Host is down
- The receiver + BlueZ handshake aren't ready that fast after a hard
power loss.  Previous logic tried start_relay() exactly once and
swallowed the exception with logger.exception(), leaving the user
with no UI surface and a stuck non-running state.
- Two fixes:
- 1. services/__init__.py — replace the single try/except with a
   background task that retries on a fixed schedule (0, 5, 10, 20,
   40, 80 s = six attempts over ~2.5 min).  Bail on permanent errors
   (ValidationError / ConfigurationError) — those won't self-heal.
   Bail if the user starts the relay manually during the backoff
   window.
- 2. Module attribute `auto_start_status: AutoStartStatus` exposes the
   lifecycle state (idle / in_progress / succeeded / succeeded_user
   / failed_config / failed_after_retries / skipped_no_input).
   Surfaced on `GET /api/relay/status` via a new `AutoStartStatusModel`
   field.  Dashboard reads it each polling tick and renders a
   dismissible banner (yellow for in-progress, red for failure).
- Tests: 4 new auto-start retry tests (transient-then-success, all-fail,
config-error fast-fail, user-aborts-mid-retry), 1 API field test.
Updated 2 existing tests to await the new background task.
- Live smoke: started server with auto_start: true + TCP input pointing
at a closed port.  Status response cycled through attempts 1-3 with
the connection-refused error captured in last_error.  Started a
listener on the port; attempt 4 succeeded and state flipped to
"succeeded", relay running.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.19 (2026-06-01)


- fix(config): satisfy both pyright strict and mypy strict in load filter
- v0.3.18's release workflow failed at the mypy-strict step: pyright
wanted `cast()` calls to silence reportUnknownArgumentType, but mypy
flagged those same casts as `redundant-cast` since it inferred the
narrowed types directly. No PyPI artifact was published.
- Rework the type narrowing in `_filter_invalid_base_positions` and
its caller without casts:
- - Annotate `raw: Any` for the .get() result; use `# type: ignore[arg-type]`
  on the per-iteration accesses (which mypy alone trips on).
- Extract the dict-name access via an explicit `isinstance(raw_name, str)`
  narrow into a typed `name: str`, removing pyright's
  `reportUnknownArgumentType` on the logger.warning call.
- Use a `# pyright: ignore[...]` comment on the data_dict assignment
  to acknowledge that YAML keys at the root are always str by schema,
  which pyright cannot infer from yaml.safe_load's `Any` return.
- No functional change. Same 615 tests pass. Ships the v0.3.18 fixes
under the v0.3.19 tag.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
- fix(config): tolerate legacy base_position names on load; tighten 422 mapping
- Round-6 e2e on v0.3.17 surfaced 3 issues:
- 1. Critical: v0.3.17's new regex on BaseStationPosition.name fires at
   YAML deserialization time, not just at API-input time. Any persisted
   position with a legacy non-conforming name (saved before v0.3.17)
   makes AppConfig.model_validate raise — every route that calls
   get_config() returns HTTP 500. After upgrade, the app is bricked
   for users with such data on disk.
-    Fix: pre-filter base_positions per-entry in load_config(). Drop
   invalid ones with a warning log identifying which name was skipped.
   The on-disk YAML stays untouched so the user can rename and recover
   via the UI. Model-layer regex still rejects bad input at POST time
   with 422.
- 2. Low (defense-in-depth): sp_rtk_base_relay.exceptions.ConfigurationError
   fell through the /api/relay/start classifier to 500 because its
   class name and message didn't match any of the historic substring
   keywords. Add ConfigurationError to the class-name OR alongside
   ValidationError.
- 3. Quality: POST/PUT /api/destinations mapped pydantic ValidationError
   to 400, not 422 — inconsistent with FastAPI's own body-validation
   behaviour and with the v0.3.17 PR notes. Catch ValidationError
   explicitly and emit 422; keep 400 as the catch-all.
- Tests: 5 new (2 config-service load resilience, 1 relay class-name
mapping, 2 destinations 422). Total 615 passing.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
- Merge pull request #1 from rodenj1/renovate/docker-images
- chore(deps): update python Docker tag to v3.14
- chore(deps): update python Docker tag to v3.14

## v0.3.17 (2026-05-29)


- fix(models): close 2 remaining API validation gaps from round-5 hunt
- Round-5 e2e tour confirmed all v0.3.16 fixes hold.  Two
API-level-only gaps remained — both reachable only via direct API
POST or config import, never through normal UI use:
- Bug 1 (Medium) - Filter config with empty/invalid allowlist 500s
================================================================
POSTing a destination with ``filter.mode='allowlist'`` and either
an empty ``message_ids`` or out-of-range IDs (-1, 999999, etc.)
was accepted by the API (HTTP 201) but caused HTTP 500 at relay
start time.  The relay engine itself raises pydantic
ValidationError ("filter.message_ids is required when mode is
'allowlist'") which the v0.3.15 status-code mapping correctly
maps to 422 — but operators don't expect a save-then-start two-
step failure.
- Fix: pull both checks down into ``FilterProfile`` at the model
layer:
- - ``@field_validator("message_ids")`` rejects any ID outside the
  RTCM 3.x assigned range 1000-1230.
- ``@model_validator(mode='after')`` rejects ``allowlist`` /
  ``blocklist`` modes when ``message_ids`` is empty.
- Now save-time produces HTTP 422 with the actionable message; the
relay-start path never sees this case anymore.
- Bug 2 (Low) - Position name API bypasses UI regex
================================================================
``POST /api/device/base-positions`` accepted any name string
(spaces, slashes, ``!``, etc.).  The UI dialog enforced the
``^[A-Za-z0-9_-]+$`` regex but the API didn't.  Pure consistency
gap with the destination-name policy.
- Fix: ``BaseStationPosition.name`` now uses pydantic's
``Field(pattern=..., min_length=1, max_length=64)``.  Same rule
applies at every entry point: UI dialog, direct API POST, config
import, programmatic construction.
- Test updates:
- New ``TestFilterProfile`` cases: empty-allowlist rejection,
  invalid RTCM ID rejection, pass_all still accepts empty IDs.
- New ``TestBaseStationPositionNameRegex`` class with 7 cases:
  simple/hyphen/digits accepted; space/slash/special-char/empty/
  over-length rejected.
- ``test_base_positions.py``: existing fixture names updated from
  "Office Roof", "Site A", "Delete Me", "Persist Test", "New Site"
  to underscore-compliant equivalents.
- Verification:
- 610 unit tests pass (+11 new validator tests).
- ruff + format clean.
- pyright strict 0 errors.
- Round-5 result: bug surface now zero in normal-UI-reachable paths
AND zero in direct-API paths.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.16 (2026-05-29)


- fix(ui): 4 round-4 bugs - sticky Connecting state, silent Apply, position name rules
- Round-4 e2e tour on v0.3.15 verified all six v0.3.15 fixes hold and
surfaced four remaining issues, all low-to-medium severity.
- Bug 1 (Medium) - "Connecting..." state stuck after relay-conflict error
=====================================================================
Clicking Connect on the Survey or GPS Config page while the relay
was running showed the v0.3.15 "Cannot connect: relay is running"
toast correctly, but the UI then sat on "Connecting..." with the
Cancel button visible indefinitely.  The error toast and the
connection-state indicator disagreed about reality.
- Root cause: ``DeviceService.connect()`` ran its three early-return
guards (no driver / already connected / relay running) BEFORE the
``self._state = CONNECTING`` line, so when one raised, the
exception handler in the try-block below — which DOES set
``self._state = ERROR`` — never ran.  The UI had already called
``svc.set_connecting()`` before invoking ``connect()``, so state
was stuck at CONNECTING forever.
- Fix: each early-return path now explicitly sets ``self._state`` and
``self._last_error`` before raising.  No-driver -> DISCONNECTED,
relay-running -> ERROR, already-connected -> unchanged (the
existing CONNECTED state is correct).
- Bug 2 (Medium) - GPS Config Apply buttons appear silent
=====================================================================
``_apply_rtcm`` and ``_apply_gnss_config`` both already call
``ui.notify`` on success and failure, but NiceGUI toasts fade in
~5 s.  The round-4 e2e tour saw "no `[role=alert]` in DOM after
Apply" because the snapshot was taken after the toast had vanished.
Operators get no durable feedback that their config landed.
- Fix: persistent status labels (``rtcm_apply_status`` /
``gnss_apply_status``) below each Apply button.  Show "✓ ...
applied" on success and "✗ ... failed: <exc>" on error.  Sticks
on screen until the next attempt.  Toast still fires.
- Bug 3 (Low) - Saved Position name accepts spaces/slashes
=====================================================================
Survey page Save Position dialog accepted names like "my pos" and
"my/pos".  Destination names were already rejected at the regex
``^[A-Za-z0-9_-]+$`` in v0.3.14; position names had no such guard
even though they end up in the same YAML config file and may
later be used in URL paths.
- Fix: added the same regex to the Save Position dialog
(``_POS_NAME_RE`` / ``_POS_NAME_MSG``).  Inline error on the
name field via ``name_input.validation`` AND an explicit recheck
in ``_do_save`` (since NiceGUI validators only fire on
user-input events, never on initial empty value).
- Bug 4 (Low) - Duplicate Saved Position silently overwrites
=====================================================================
Saving a position with an existing name silently replaced the
existing entry with no warning toast or dialog.  Operators
testing the workflow lost their previously-surveyed coordinates.
- Fix: ``_do_save`` now checks ``config_svc.get_base_position(name)``
before persisting; if a duplicate is found, opens an
``_confirm_overwrite`` dialog with the destination name and the
new coordinates' approximate values.  Operator clicks Cancel
(no-op) or Overwrite (proceeds with the persist).  Mirrors the
Restore dialog pattern that already exists for saved positions.
- Verification:
- 599 unit tests pass (no test changes).
- ruff + format clean.
- pyright strict 0 errors.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.15 (2026-05-29)


- fix(outputs): close v0.3.14 gap + 5 more bugs from round-3 e2e hunt
- The post-v0.3.14 e2e tour verified all five v0.3.14 fixes hold, AND
surfaced six new issues including a critical chain my v0.3.14 fix
left half-broken.
- Bug 1 (Critical) - SurePath form accepts empty Username/Password
================================================================
v0.3.14 aligned the SurePath form field NAMES to the model
(username/password instead of project_id/token) but the save
handler still let untouched required fields slip through.  Root
cause: NiceGUI's ``validation`` dict callbacks only fire on
``update:model-value`` events, so an input the operator never
touched has ``inp.error = None`` even when its value is empty.
The save code's ``for inp in config_inputs.values(): if inp.error``
pre-check then silently passed, the empty value was filtered out
by ``{k: v.value for k, v in config_inputs.items() if v.value}``,
and pydantic raised a raw ``ValidationError`` at relay start.
- Fix: new ``_run_validators_now()`` helper explicitly walks each
input's ``validation`` dict and runs the callbacks against the
current value, setting ``inp.error`` inline AND returning the
first failure message so callers can show a toast.  Called from
both ``_save_new`` and ``_save_edit`` before any save attempt.
- Bug 2 (High) - /api/relay/start raw HTML 500 on config-shape errors
================================================================
``input_config = config.input.to_relay_config()`` and
``dest_configs = [d.to_relay_config() for d in enabled_dests]``
were OUTSIDE the try/except in api/relay.py.  A pydantic
ValidationError from a stale (pre-v0.3.15) SurePath profile
therefore escaped as an uncaught exception, returning raw
HTML 500 ``Internal Server Error`` to the caller instead of the
v0.3.14 422/502/500 status-code mapping.
- Fix: move the two ``to_relay_config()`` lines inside the try block.
- Bug 3 (Medium) - Device Connect while relay running hangs UI
================================================================
Clicking Connect on the Survey page while the relay was running
left the UI on "Connecting..." indefinitely.  ``DeviceService``
raises ``RuntimeError('Cannot connect to device while relay is
running')`` immediately, but the survey.py ``_connect`` except
clause wrapped it as a generic "Connection failed:" toast that
didn't make the actual issue or next step clear.
- Fix: detect the "relay is running" pattern and show a specific
warning toast pointing the operator at the Dashboard Stop button.
- Bug 4 (Medium) - Cancel Survey button visible after auto-commit
================================================================
Long-standing state-coherence issue: ``_auto_commit_survey`` set
``svin_start_btn.set_visibility(True)`` on success but never
hid ``svin_cancel_btn``.  After every successful survey both
buttons sat side-by-side, leaving the operator to guess which
state they were in.
- Fix: ``svin_cancel_btn.set_visibility(False)`` added to both the
success path and the auto-commit error tail.
- Bug 5 (Low) - Duplicate destination name from second tab silent
================================================================
Opening Add Destination dialogs in two tabs and saving the same
name in each: tab 1 succeeded, tab 2's save did nothing visible
(the duplicate check toast was emitted but easily missed).
- Fix: in addition to the existing ``ui.notify``, set
``name_input.error`` inline so the conflict stays visible on the
field until the user changes the name.
- Bug 6 (Low) - Toggle switches inaccessible
================================================================
Outputs page enable/disable toggles had no ``aria-label`` (screen
readers couldn't identify which destination they controlled) and
no visible focus ring (keyboard users couldn't see where they
were).  Same for Edit and Delete buttons on each destination card.
- Fix: ``aria-label="Enable <name>"`` / "Edit <name>" / "Delete
<name>" props on the three controls per destination row.
- Verification:
- 599 unit tests pass (no test changes).
- ruff + format clean.
- pyright strict 0 errors.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.14 (2026-05-29)


- fix(outputs): 7 bugs from full-app e2e bug hunt on v0.3.13
- The post-v0.3.13 e2e tour (Dashboard / Input / Outputs / Survey /
GPS Config / Settings) verified all five prior fixes hold AND
surfaced seven new issues across the UI and the relay-control API.
All addressed here; no relay-side changes needed.
- Bug 1 (Critical) - SurePath destination type completely broken.
``TYPE_FIELDS["surepath"]`` in outputs.py asked for
``project_id`` / ``token`` keys, but ``SurePathProfile`` in
models/config_models.py requires ``username`` / ``password``.  Any
SurePath destination saved through the UI failed pydantic validation
at relay start with "username Field required, password Field
required".  No SurePath destination has ever worked end-to-end.
Fix: align the UI form fields with the model - host, port=50010,
username, password.  Also updated ``TYPE_DISPLAY_FIELDS`` for the
read-back path.
- Bug 2 (High) - Destination name validation gap.
The UI accepted any string as a destination name, but the relay
engine requires names matching ``^[A-Za-z0-9_-]+$`` and FastAPI's
URL router fails to parse slash-containing names on the DELETE
endpoint.  Added a regex validator (``_name_validator`` +
``_NAME_VALIDATION_MSG``) to both Add and Edit dialogs.  Resolves
the headline complaint AND Bug 7 (slash-name deletion impossible)
since slashes never reach the API.
- Bug 3 (Medium) - Stale "Cancel Survey" button after reconnect.
Disconnect mid-survey -> reconnect -> both Start and Cancel buttons
were visible at the same time.  ``_connect`` now resets
``svin_progress_card``, ``svin_cancel_btn``, ``svin_error_label``,
``svin_warning_label``, and ``svin_timer`` / ``_svin_dur_offset``
state on every reconnect attempt before the actual ``svc.connect``
call.
- Bug 4 (Medium) - Misleading "Destination not found" on mid-run toggle.
Toggling a destination ON while the relay was running surfaced the
raw engine error "Destination X not found", which sounds like the
config save also failed (it didn't).  ``_toggle_enabled`` now
detects the "not found"/"unknown" pattern and replaces the toast
text with "Config saved.  Restart the relay to activate '<name>'..."
- Bug 5 (Low) - Doubled "Connection failed:" prefix in toast.
``UbloxDriver.connect`` already wraps its errors with
"Connection failed: ..."; the UI then prefixed that with
"Connection failed: " again, producing
"Connection failed: Connection failed: No response...".  Now
detects the existing prefix and renders the message once.
- Bug 6 (Low) - /api/relay/start returns 500 for non-server failures.
Previously every relay-engine failure produced HTTP 500.  Now
mapped:
- pydantic validation / ConfigurationError -> 422 (config malformed)
- connection-refused / DNS / engine-bringup network failures -> 502
  (bad gateway - upstream the relay is trying to reach is down)
- anything else -> 500 (genuine server bug, unchanged)
Helps API consumers distinguish "fix your config" from "fix the
network" from "report a bug".
- Bug 7 (Low) - DELETE /api/destinations/<slash-name> 404s.
FastAPI's path parser eats encoded slashes in URL components.
Compound issue with Bug 2 - now that the name regex rejects
slashes at save time, slash-names can never enter the system, so
the DELETE path no longer needs special handling.  Closed by Bug 2's fix.
- Verification:
- 599 unit tests pass (+2: 422 and 502 status-code branches).
- ruff + format clean.
- pyright strict 0 errors.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.13 (2026-05-28)


- chore(deps): bump sp-rtk-base-relay 2.1.2 -> 2.1.3 for BT poll fix
- sp-rtk-base-relay v2.1.3 replaces the v2.1.2 hardcoded 5 s Bluetooth
recovery scan with a poll-until-org.bluez.Device1-is-populated loop.
Without this bump, sp-rtk-base on larson-base.lan keeps resolving the
v2.1.2 relay from PyPI and continues to hit:
-   Failed to prepare Bluetooth device: Device setup failed after
  recovery scan: Pairing failed: interface not found on this object:
  org.bluez.Device1
- after >~30 s of relay idle, requiring a manual "Scan for Devices"
click from the Input page to recover.  The fix lives entirely in
the relay (poll loop in BluetoothManager + scan_timeout default
bump 10 -> 30 in BluetoothConfig); this commit just picks it up
via the constraint bump + uv.lock refresh.
- No code changes in sp-rtk-base itself.  ``BluetoothInputSource.connect``
already passes ``self.config.scan_timeout`` through from
BluetoothConfig, and sp-rtk-base's existing ``to_relay_config()``
already injects ``DEFAULT_BT_SCAN_TIMEOUT_SECONDS = 20`` when the
operator's config omits the field — that value is now also forwarded
to the relay's interface-poll loop, which is exactly what the v2.1.3
plumbing expects.
- Verification: 597 unit tests pass on the new lock; ruff + format
clean; pyright strict 0 errors.
- See https://github.com/rodenj1/sp-rtk-base-relay/releases/tag/v2.1.3
for the full relay-side write-up.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.12 (2026-05-28)


- fix(ui): 5 UI bugs found by full-app e2e tour with fake GPS driver
- Tested all six pages with sp-rtk-base running locally + fake driver.
Five legitimate bugs surfaced across input/dashboard/outputs/survey:
- 1. CRITICAL — TCP input port saved as string, relay can't start
   (input.py): NiceGUI ui.input() returns strings, so the TCP port
   landed in the saved config as "5015" not 5015.  Subsequent
   relay.start() failed with "input.config.port must be an integer
   between 1 and 65535".  Repro: 3/3 times every config save.
   Cast port to int in _save_input.
- 2. MEDIUM — Dashboard Start error leaks raw exception text
   (dashboard.py): toast showed "Failed to start relay:
   input.config.port must be an integer ... | Key: input.config.port"
   then faded in 3s, leaving the operator with no persistent signal.
   Added a persistent start_error_label below Start/Stop and mapped
   ConfigurationError-pattern messages to friendly text ("TCP input
   port is not a number. Re-save the Input config and try again.").
- 3. MEDIUM — Survey complete shows contradictory progress state
   (survey.py): when valid=true fires, the "% to target: 100% —
   waiting on min duration (Xs left)" + "ETA: ~Xs (waiting on min
   duration)" labels remained on screen alongside "✓ Complete".
   Now clear those labels (and svin_progress_bar->1.0,
   svin_warning_label hidden) in the valid-branch of _poll_survey_in.
- 4. MEDIUM — Restore Past Survey overwrites without confirmation
   (survey.py): all other destructive-ish actions (Start, Cancel,
   Reset) show a confirmation dialog; Restore did not.  Now wraps
   _restore in a ui.dialog with explicit "This will overwrite the
   receiver's current fixed-base position..." copy + Cancel button.
- 5. LOW — Outputs Edit dialog has no Name field
   (outputs.py): renaming a destination required delete + re-add,
   losing any filter rules.  Added a Name input at the top of the
   edit dialog with collision detection.  When name changes, the
   save path removes the old entry before saving the new.
- Skipped from the agent's findings:
- Reset GPS missing toasts on fake driver: fake driver doesn't
  expose reset_and_reconnect; service correctly raises and the
  except branch already emits a "Reset failed" toast.  Real driver
  works as designed (verified in v0.3.11 on larson-base).
- Serial port combobox free-text reverts: needs deeper
  investigation of NiceGUI ui.select with_input behavior.
- Verification: 597 unit tests pass; ruff + format clean; pyright
strict 0 errors.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.11 (2026-05-28)


- fix(survey): second-survey-in just works + clear banners on reset + persist fixed-base correctly
- Three bugs surfaced by the e2e-ui-tester after a successful v0.3.10
first-survey:
- (1) PRIMARY — Second survey-in fails: after a successful survey,
NAV-SVIN.dur sits at >= 120s in BBR.  The next Start fails our
floor check (dur < 30s) with "stale survey-in accumulator —
Click Reset GPS".  User has to manually reset between every survey
attempt.
- (2) BUG A — Stale error banner: after Reset GPS clears the receiver,
the UI's "Configuration failed" banner stays visible.  ``last_error``
is null in the API but the UI doesn't reflect that.
- (3) BUG B — Successful-survey config doesn't persist to Flash: after
auto-commit (configure_fixed_base + save_to_flash), a subsequent
hardware reset reverts base-config to the *prior* flashed state
(e.g. an older ECEF Orig Survey) instead of the just-committed LLH
coordinates.  Root cause: ``save_to_flash`` uses CFG-CFG (the
pre-Gen9 saveMask API) which doesn't reliably persist key/value-
based TMODE config on ZED-F9P (Gen9+).
- Driver changes (services/drivers/ublox.py):
- - ``configure_survey_in`` now reads a NAV-SVIN baseline BEFORE
  doing anything else.  If ``dur >= _SVIN_DUR_FLOOR_S`` (30s), it
  calls ``reset_and_reconnect`` transparently before continuing.
  Fresh receiver: 0 overhead.  Stale receiver: ~5-8s added to
  the start latency, no user-visible error.  The end-of-flow
  floor check stays as a defensive safety net.
- - ``configure_fixed_base`` now writes the TMODE config to layer=5
  (RAM+Flash) directly via the modern CFG-VALSET interface.  This
  is the canonical persistence path on Gen9+ receivers.  The
  legacy ``save_to_flash`` (CFG-CFG) call from the auto-commit
  flow becomes redundant for TMODE but stays for any other config
  it might persist.
- UI changes (ui/pages/survey.py):
- - ``_start_survey_in`` peeks at NAV-SVIN before calling
  ``configure_survey_in``.  If ``dur >= 30``, surfaces a toast
  "Stale survey state detected — resetting GPS before starting
  (~5-8s)..." so the operator understands the extra delay.  Status
  label reads "Resetting GPS, then starting..." during the reset
  window.
- - ``_reset_receiver`` now explicitly clears ``error_label``,
  ``svin_error_label``, ``svin_warning_label`` and resets the
  survey progress card visibility / status label after a
  successful reset.  Without this the stale "Configuration failed"
  banner from a previous attempt persisted indefinitely.
- Tests:
- - New ``test_configure_survey_in_auto_resets_when_dur_stale``: the
  baseline read returns dur=61955; the test asserts
  ``reset_and_reconnect`` is called exactly once and the survey
  then proceeds normally (only 2 CFG-VALSETs: layer=7 disable +
  layer=1 enable, no rollback).
- - Existing survey-in success-path tests gain a baseline NAV-SVIN
  read at position 0 in side_effect.
- - ``test_configure_fixed_base`` asserts the fixed-base VALSET now
  targets ``layer=5`` (RAM+Flash) instead of ``layer=1`` (RAM only).
- Verification: 597 unit tests pass; ruff + format clean; pyright
strict 0 errors.  Saga close-out — first survey + first re-survey
should now both work end-to-end on larson-base without manual
Reset GPS intervention.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.10 (2026-05-28)


- fix(survey-ui): resolve "100% target reached" vs ETA contradiction
- After v0.3.9 the first real survey on larson-base completed cleanly
— but the e2e-ui-tester caught a misleading UX contradiction during
the duration-wait phase:
-   "% to target:  100% (target reached)"
  "ETA:          ~86s (duration)"
- The pct_acc label fired "target reached" the moment ``cur_acc <=
acc_target`` without checking whether the min-duration gate was
also satisfied.  The ETA label correctly said the survey was still
waiting on duration.  The two contradicted each other — which is
exactly the "graph shows it's met the threshold but it doesn't seem
to be complete" symptom the user reported.
- Fix (``ui/pages/survey.py``):
- - "% to target" label now reads
  "100% — waiting on min duration (Xs left)"
  while accuracy is met but duration hasn't elapsed.  Flips to
  "100% (target reached)" only when BOTH gates are satisfied.
- ETA label's duration-only branch reads
  "~Xs (waiting on min duration)" (was "~Xs (duration)") so the
  reason is explicit at a glance.
- No functional change — survey completion semantics are unchanged.
This is a pure label clarity improvement.
- Verification: 597 unit tests pass; ruff + format clean; pyright
strict 0 errors.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.9 (2026-05-28)


- fix(survey): correct 0.1mm wire-unit conversion on accuracy fields
- The user reported: survey was configured with accuracy_limit_mm=50000
(meaning "complete when mean accuracy is within 50 m"), and the
display reached 7000 mm long before that target — yet the survey
never completed.
- Root cause: ``CFG_TMODE_SVIN_ACC_LIMIT`` and ``CFG_TMODE_FIXED_POS_ACC``
are in **0.1 mm units** on the UBX wire, not raw mm (u-blox spec:
"1 m = 10000, 3.2598 m = 32598").  Our driver was sending and reading
them as raw mm, producing a consistent 10× undershoot:
- - "50000 mm" limit sent → receiver read 5000 mm = 5 m target
- meanAcc 7000 mm displayed → receiver had 70000 in 0.1 mm = 7 m
- 7 m > 5 m → survey correctly held open by the receiver
- Same bug bit ``CFG_TMODE_FIXED_POS_ACC`` on both the write side
(configure_fixed_base) and the read side (_parse_cfg_tmode).  That's
why the Saved Positions card showed "±47308 mm" — the actual saved
accuracy was 4730.8 mm (~4.7 m).
- Driver fixes (services/drivers/ublox.py):
- - configure_survey_in: send ``accuracy_limit_mm * 10`` for
  CFG_TMODE_SVIN_ACC_LIMIT (the 0.1 mm wire-unit convention).
- configure_fixed_base: send ``accuracy_mm * 10`` for
  CFG_TMODE_FIXED_POS_ACC (same convention).
- _parse_cfg_tmode: divide the raw CFG_TMODE_FIXED_POS_ACC field
  by 10 when reading it back, so CurrentBaseConfig.accuracy_mm is
  consistently in mm across the API boundary.
- UI tweak (ui/pages/survey.py):
- - Added a small "= X.xxx m" caption directly below the
  "Accuracy Limit (mm)" input field.  Updates live as the operator
  types/spins.  Default 50000 mm shows "= 50.000 m", which makes
  the unit-conversion bug we just fixed obvious to anyone glancing
  at the page.
- Test updates:
- - test_parse_llh_pos_type / test_parse_ecef_pos_type now assert the
  post-conversion mm values (47308 raw → 4730 mm, 5000 raw → 500 mm).
- test_configure_survey_in asserts CFG_TMODE_SVIN_ACC_LIMIT == 400000
  on the wire when accuracy_limit_mm=40000 is passed in.
- test_configure_fixed_base asserts CFG_TMODE_FIXED_POS_ACC == 5000
  on the wire when accuracy_mm=500 is passed in.
- The cancel-survey-in regression test's wire-value assertion bumped
  to 500000 to match the new conversion.
- Verification: 597 unit tests pass; ruff + format clean; pyright
strict 0 errors.  Empirical verification (start a real survey on
larson-base after upgrade) is the next step.
- Note on existing saved positions: prior surveys saved to
positions.json have inflated accuracy fields (10× what the actual
value was).  No migration shipped — those fields are advisory
only; the lat/lon/height that matter are unaffected.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.8 (2026-05-28)


- feat(survey): hardware-reset receiver to clear stuck survey state
- This is the production fix following the v0.3.6 / v0.3.7 diagnostic
campaign on larson-base (ZED-F9P, HPG 1.12).  We ran 7 controlled
CFG-RST variants against the live receiver; only resetMode=0x00
(immediate hardware reset) actually clears the BBR-backed
NAV-SVIN.dur accumulator.  Software resets (resetMode 1/2/8/9 with
any BBR-bit combination including a full coldstart-equivalent
clear) leave the counter untouched.
- The hardware reset drops USB momentarily, so the driver now closes
its serial handle, sleeps ~5s for the chip to re-enumerate, and
reopens on the same port/baud.  Saved fixed-base coordinates in
Flash are preserved across the reset — only the survey state and
MODE key are wiped.
- Driver (services/drivers/ublox.py):
- - New ``reset_and_reconnect()`` method.  Writes CFG_TMODE_MODE=0
  to layer=7 (RAM+BBR+Flash) FIRST so the post-reset boot lands
  in rover mode regardless of what state was last persisted, then
  sends UBX-CFG-RST resetMode=0 + pos=1, closes the stale serial
  handle, sleeps 5s, reopens on the saved port/baud, redoes
  MON-VER.  Returns the refreshed DeviceInfo.
- - ``configure_survey_in`` no longer calls the broken CFG-RST=0x09
  pre-reset (proven not to work on HPG 1.12).  Its dur-floor check
  now raises a clear "click Reset GPS" message instead of the
  power-cycle instruction.
- - ``configure_fixed_base`` no longer calls the broken CFG-RST=0x09
  pre-reset.  The layer=7 TMODE=0 pre-disable handles the
  edge-trigger requirement.
- - Dead methods ``reset_survey_state`` / ``_reset_survey_state_locked``
  and the unused ``_CFG_RST_SETTLE_S`` constant removed.
- - Driver now remembers ``self._port`` and ``self._baud_rate`` on
  connect so ``reset_and_reconnect`` can reopen the same port.
- Service (services/device_service.py):
- - New ``reset_receiver()`` method wrapping the driver call.
- ``cancel_survey_in`` now auto-resets after writing TMODE=0, so
  the next Start sees a clean dur=0 instead of inheriting the
  cancelled session's accumulator.
- ``configure_survey_in`` auto-resets in its except branch on
  failure, so the operator can immediately retry without manual
  intervention.  Reset errors are logged but the original error
  is surfaced to the caller.
- API (api/device.py):
- - New ``POST /api/device/reset`` endpoint.  Returns 200 with the
  refreshed model + firmware on success, 409 when not connected
  or driver doesn't support reset, 502 on reconnect failure.
- UI (ui/pages/survey.py):
- - New "Reset GPS" button in the Fixed Base Position card next to
  Edit / Save / Load.  Confirmation dialog with a clear summary of
  what gets cleared (survey state) vs preserved (flash coords).
- New ``fb_mode_hint`` label under the mode badge.  Shown only
  when TMODE=survey_in with text explaining when Reset GPS is
  the right next action.
- Reset button props turn ``color=warning`` when mode=survey_in
  so it visually escalates without changing position on the page.
- Tests:
- - New ``TestResetReceiverEndpoint`` (4 tests) covers: 409 when no
  driver, 409 when driver doesn't expose reset, 200 success path,
  502 on ConnectionError from the driver.
- Existing tests adjusted: removed assertion that error text
  mentions "power cycle" (now says "Reset GPS"); reset_mode=3
  used for the disallowed-mode test (since 0 is now allowed under
  fire-and-forget).
- Verification:
- 597 unit tests pass; ruff + format clean; pyright strict 0 errors.
- Empirically verified on larson-base: CFG-RST resetMode=0 + pos=1
  drops NAV-SVIN.dur from 65968 -> 0 (the moment of truth that
  triggered this release).
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.7 (2026-05-28)


- feat(diag): allow hardware-reset modes 0/4 with fire-and-forget mode
- Continues the v0.3.6 diagnostic experiment.  Six software reset
variants tested against larson-base (HPG 1.12) all left
NAV-SVIN.dur at 65968 across the call — including a coldstart-
equivalent with all 10 BBR bits set + resetMode=9.  The accumulator
appears to live outside any documented BBR section.
- Only untested variants are hardware resets (resetMode=0 immediate,
resetMode=4 after-delay) which drop the USB serial connection and
make our existing endpoint hang on the post-write NAV-SVIN poll.
- API (api/device.py):
- - Add 0 and 4 to _ALLOWED_RESET_MODES.
- New _HARDWARE_RESET_MODES = {0, 4} guard.
- New `read_after_state: bool = True` field on CfgRstRequest.  When
  False, the endpoint skips both the post-write sleep and the
  after-poll.  Required for hardware resets so the response can
  return before the USB re-enumerates.
- Endpoint refuses reset_mode ∈ {0, 4} with read_after_state=True
  up front with a 400 explaining the pairing.
- `after: SurveyInProgress | None` on CfgRstResponse (null in the
  fire-and-forget case).
- `wait_seconds` is reported as 0.0 in the fire-and-forget response
  so the operator sees the sleep was skipped.
- Driver/Service: ``read_after_state`` plumbed through.  When False,
the driver writes the UBX frame, returns immediately, and the
operator is expected to reconnect via /api/device/connect after
the chip finishes re-enumerating.
- Tests (tests/unit/test_cancel_survey_in.py):
- - Updated existing assert_called_once_with to expect the new 4th
  positional (read_after_state).
- New test_endpoint_rejects_hardware_reset_with_after_read pins the
  refusal-on-incompatible-pairing behaviour.
- New test_endpoint_fire_and_forget_hardware_reset confirms a
  resetMode=0 with read_after_state=False returns after=null,
  wait_seconds=0.0, and forwards read_after_state=False to the
  driver.
- Tweaked test_endpoint_rejects_disallowed_reset_mode to use
  reset_mode=3 (still not in the allowlist) since 0 is now allowed
  with the appropriate pairing.
- No production code path uses the new flag.  This is strictly to
enable the hardware-reset experiment.
- Verification: 593 unit tests pass; ruff + format clean; pyright
strict clean.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.6 (2026-05-28)


- feat(diag): add POST /api/device/debug/cfg-rst for survey-reset experimentation
- v0.3.5's CFG-RST (resetMode=0x09 + pos bit) does not reset the
NAV-SVIN dur accumulator on ZED-F9P HPG 1.12 — verified by e2e
test on larson-base where dur went 65793 -> 65968 across the
start attempt instead of dropping to 0.  The accumulator is not
in the "position" BBR section the way I assumed; it lives somewhere
else and needs a different reset variant.
- Rather than ship another speculative fix, this release adds a
diagnostic endpoint so we can find the right variant empirically
on the actual deployed firmware before shipping a code change.
- Driver (services/drivers/ublox.py):
- - `send_cfg_rst_diagnostic(reset_mode, wait_seconds, bbr_bits)`
  Generic CFG-RST sender that holds the driver lock across a
  before-write-wait-after cycle and returns
  (before: SurveyInProgress, after: SurveyInProgress, ubx_bytes_sent)
  so the diagnostic UI can show the exact effect on NAV-SVIN.dur
  for any reset_mode + BBR-bit combination.
- Service (services/device_service.py):
- - `DeviceService.send_cfg_rst_diagnostic(...)` — thin asyncio
  wrapper.  Raises RuntimeError when the active driver doesn't
  expose the method (i.e. fake driver), so the API layer can
  surface a clear 409.
- API (api/device.py):
- - New endpoint POST /api/device/debug/cfg-rst with Pydantic
  request/response models.  Validates:
  - reset_mode ∈ {1, 2, 8, 9} — hardware resets (0x00, 0x04)
    rejected because they cause USB re-enumeration that drops the
    serial connection.
  - bbr_bits — only the documented pyubx2 named bits
    (eph, alm, health, klob, pos, clkd, osc, utc, rtc, aop).
  Returns 400 on validation failure, 409 when the device isn't
  connected or the driver doesn't support diagnostics.
- Tests (tests/unit/test_cancel_survey_in.py::TestCfgRstDiagnosticEndpoint):
- - rejects disallowed reset_mode → 400
- rejects unknown bbr_bits → 400
- rejects non-ublox driver → 409
- success path: calls driver, returns before/after JSON + ubx hex
- No production code path uses the new diagnostic — survey-in and
fixed-base flows are unchanged from v0.3.5.  This release is
strictly for finding the right reset variant before v0.3.7.
- Verification: 591 unit tests pass; ruff clean; pyright src clean
(0 errors).
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.5 (2026-05-28)


- fix(survey): reset BBR survey accumulator via UBX-CFG-RST + fix Restore-from-survey
- v0.3.4's e2e verification revealed two deeper bugs the layer=7 VALSET
disable did not solve, plus uncovered a long-standing Restore bug:
- 1. NAV-SVIN.dur is BBR-backed and CFG-VALSET cannot reset it.
   Observed: dur=61955s (~17h) carried over the layer=7 disable +
   TMODE=1 enable cycle.  The receiver kept reporting the
   accumulator from prior sessions even after CFG_TMODE_MODE=0 to
   RAM+BBR+Flash.  The dur-progression check from v0.3.4 trivially
   passed against this still-ticking accumulator (false success).
- 2. configure_fixed_base wrote TMODE=2 + coords in a single VALSET
   with no pre-disable.  On a receiver currently in TMODE=1
   (survey-in) the F9P silently coalesces the write — ACKs cleanly
   but never changes mode.  The user-visible symptom was the
   Restore button appearing to succeed (200 OK) while the receiver
   stayed in survey-in mode and the dur counter kept ticking.
- 3. UI poll handler gated dur-offset capture on progress.active,
   which is False on HPG 1.12 even during a real survey — so the
   offset never captured and the UI displayed the raw stale
   accumulator (e.g. "Duration: 63065s") instead of counting from 0.
- Driver changes (services/drivers/ublox.py):
- - New `reset_survey_state()` + `_reset_survey_state_locked()` send
  UBX-CFG-RST with pos=1, resetMode=0x09 (controlled GNSS start,
  position BBR bit).  Canonical software-only way to reset the
  survey accumulator; ephemeris and almanac are preserved so GPS
  reacquires in ~5-30s (warmstart equivalent).
- - configure_survey_in calls reset_survey_state first, then the
  layer=7 TMODE disable, then the layer=1 enable.  Verify uses
  two signals: (a) before.dur < 30s proves the reset actually
  worked (no false-pass against a stale accumulator), and (b)
  after.dur > before.dur proves the survey engaged.  Either
  failure rolls back TMODE=0 to layer=7 and raises with an
  operator-actionable message that mentions the physical
  power-cycle workaround when the floor check fails.
- - configure_fixed_base mirrors configure_survey_in's pre-disable
  pattern: CFG-RST + layer=7 TMODE=0 + settle + write TMODE=2 +
  coords to layer=1.  Restore was silently broken without this
  whenever a survey was in progress.
- UI changes (ui/pages/survey.py):
- - _poll_survey_in no longer gates dur-offset capture on
  progress.active.  The driver guarantees dur < 30s on
  configure_survey_in return, so capturing the first observed dur
  as the offset is correct regardless of the active flag.
- - Status branch reads `elapsed > 0` as the authoritative
  "survey is progressing" signal (works on HPG 1.12).
- - New svin_warning_label surfaces a yellow "Survey may not be
  converging — check antenna placement" banner when the survey
  has been running 2x its min-duration AND accuracy is still 2x
  worse than the target.  Addresses the operator UX gap when poor
  antenna placement causes a survey to grind forever.
- Tests:
- - test_configure_fixed_base exercises the pre-disable pattern;
  asserts layer=7 disable, layer=1 fixed write, and TMODE=2 mode.
- - test_configure_survey_in_raises_when_dur_floor_exceeded pins the
  floor-check and asserts the actionable power-cycle message.
- - Existing survey-in tests still pass — CFG-RST doesn't read ACKs
  (we sleep instead) so CFG-VALSET call_count is unchanged.
- Verification: 587 unit tests pass; ruff + format clean.
- Source references:
- ZED-F9P Interface Description UBX-22008968 (CFG-RST payload)
- u-blox C099 reference scripts (F9P Base Survey in disable.txt /
  start.txt) — confirms TMODE-disable to all three layers and
  TMODE=1 to RAM only is the canonical sequence.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.4 (2026-05-28)


- fix(survey): verify survey-in via NAV-SVIN.dur progression, not active flag
- v0.3.3's e2e test on larson-base revealed the v0.3.3 fix targeted the
wrong NAV-SVIN field.  NAV-SVIN.active stayed False forever on the
deployed ZED-F9P (firmware HPG 1.12) even while the receiver was
genuinely surveying — dur was ticking at 1 Hz and obs was growing.
Multiple u-blox forum threads document the active flag being
unreliable in this firmware era; the protocol spec doesn't promise
the behavior we were assuming.
- Research-backed rewrite of configure_survey_in following u-blox's own
"F9P Base Survey in disable.txt" / "start.txt" reference scripts from
the C099 board package:
- 1. Single CFG_TMODE_MODE=0 write to layer=7 (RAM | BBR | Flash)
   instead of two separate RAM and Flash writes.  This is the only
   way to reset the BBR-backed NAV-SVIN.dur accumulator — without it,
   a survey interrupted in a prior session keeps ticking dur
   silently across host restarts (we observed dur=25986 on a
   "fresh" attempt against larson-base).
- 2. Enable still writes to RAM only (layer=1) — survey-in is
   intentionally not persisted to flash; only the completed
   fixed-base coordinates from save_to_flash are.
- 3. Verify success by polling NAV-SVIN twice ~2 s apart and requiring
   ALL of: both polls returned a NAV-SVIN message, dur strictly
   increased between them, and dur/obs are both non-zero on the
   second poll.  dur progression is the only reliable evidence the
   survey-in state machine engaged — every robust open-source
   driver effectively reduces to this check.
- 4. On verify failure, roll back by re-writing CFG_TMODE_MODE=0 to
   layer=7 before raising.  Without this rollback, v0.3.3 left the
   receiver pinned in TMODE=1 with phantom-survey state on every
   failed start (the new bug the e2e agent logged after v0.3.3
   shipped).
- Tests:
- test_configure_survey_in / test_configure_survey_in_writes_disable_first
  rewritten to assert 2 CFG-VALSETs (layer=7 disable, layer=1 enable)
  and 2 NAV-SVIN polls with incrementing dur.
- test_configure_survey_in_raises_and_rolls_back_when_dur_doesnt_advance
  is the renamed-and-tightened version of the old
  raises_when_enable_doesnt_take_effect test; now also asserts the
  layer=7 rollback fires on failure.
- test_configure_survey_in_retries_disable_when_stuck deleted — the
  disable-retry path doesn't exist in the new flow.
- Verification: 586 unit tests pass; ruff + format clean.
- Source references for the new behaviour (linked in the release notes):
- u-blox C099 reference scripts (official)
- ZED-F9P Interface Description UBX-22008968 (NAV-SVIN payload)
- u-blox forum threads documenting NAV-SVIN.active unreliability
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.3 (2026-05-27)


- fix(survey): unblock survey-in + correct lat/lon display
- Three related driver fixes surfaced by the e2e-ui-tester running
against larson-base.lan with v0.3.2 deployed.
- 1. configure_survey_in's TMODE-disable verify required both
   active=False AND NAV-SVIN dur=0.  The ZED-F9P only zeroes ``dur``
   when a NEW survey starts — disabling TMODE leaves the prior
   session's accumulator intact.  Any receiver that has ever run a
   survey was permanently blocked from starting a new one: the 5
   verify polls would all fail and the subsequent enable verify
   would then raise "did not start a new survey".  Now ``not
   active`` is the sole signal, which is what the receiver actually
   reports.
- 2. _parse_nav_pvt was double-scaling lat/lon/pDOP/headMot.  pyubx2
   1.3.0 (pinned in uv.lock) pre-scales fields whose payload spec
   declares a scale factor, but the code still applied 1e-7 / 0.01 /
   1e-5 manually.  Result: /api/device/position returned lat ~ 3e-6
   instead of 32.7.  The integer-only mm fields (height, hMSL, hAcc,
   vAcc, gSpeed) have no spec scale so their /1000.0 stay.  See
   pyubx2.UBX_PAYLOADS_GET['NAV-PVT'].
- 3. configure_survey_in now also writes CFG_TMODE_MODE=0 to the FLASH
   layer (layer=4) in the cleanup step, so a survey interrupted
   before its auto-commit ran (network drop, disconnect, crash)
   can't pin TMODE=1 in flash and re-assert itself into RAM at the
   next power cycle.  Only the MODE key is touched — flashed ECEF /
   LLH coordinates from a completed prior survey persist so the
   operator can still switch back manually.
- Test updates:
- test_gps_position._make_nav_pvt fixtures now pass pre-scaled
  floats for lat/lon/headMot/pDOP, matching pyubx2 1.3.0's actual
  output shape.
- test_cancel_survey_in / test_ublox_driver survey-in tests now
  expect 3 CFG-VALSETs (RAM disable + FLASH disable + enable); the
  retry path expects 4 (incl. retried RAM disable).
- Verification: 587 unit tests pass; ruff + format clean; no new
pyright errors.  Live MCP playwright snapshot of
larson-base.lan:8080/survey confirms the v0.3.2 error banner is
still on screen until this ships.
- Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.3.2 (2026-05-27)

- fix(survey): Start Survey-In now actually starts a fresh survey
- Symptom on the field unit (larson-base): pressing "Start Survey-In"
  after a previous survey produced a UI that immediately showed
  "Active — collecting…" with a non-zero Duration (e.g. 1247s) and
  a non-zero Observations count, because the receiver was still
  reporting NAV-SVIN data from the *previous* session.  The new
  survey never actually started — TMODE was left at fixed (2) or
  the old survey-in config, so the receiver kept streaming stale
  stats while the operator watched what looked like progress.
- Driver fix (ublox.py::configure_survey_in):
  - A1: Always send TMODE=0 (disable) first, even if the receiver
    is already in survey-in mode.  This guarantees the NAV-SVIN
    counter resets before the new TMODE=1 takes effect.
  - A2: Verify the disable took (poll CFG-TMODE-MODE, expect 0)
    with a short retry window before re-enabling.  Surfaces a
    clear error if the receiver ignores the disable instead of
    silently writing a survey-in config that won't run.
- UI fix (survey.py::_poll_survey_in):
  - B: Snapshot the receiver's NAV-SVIN ``dur`` counter on the
    first poll after Start, then display ``dur - offset`` as the
    elapsed time.  Belt-and-suspenders defense against any
    residual stale counter the driver's reset window didn't catch.
  - Status now reads "Waiting for receiver..." until the device
    confirms ``active=true`` for the first time, instead of jumping
    straight to "Active" off stale data.
- Tests: 587 unit tests pass.  New coverage:
  - tests/unit/test_ublox_driver.py: configure_survey_in always
    sends TMODE=0 first and verifies the disable.
  - tests/unit/test_cancel_survey_in.py: existing cancel-path
    coverage still passes after the refactor.

## v0.3.1 (2026-05-27)


- fix(survey): cancel-survey actually cancels (lock + RX-drain + verify-retry)
- The new Cancel Survey-In button silently lied to the operator —
the receiver kept surveying while the UI showed a success toast.
Two independent bugs:
- 1. Concurrent-poll race: disable_base_mode() and the 2s NAV-SVIN
   poll shared one serial.Serial without holding self._lock, so a
   poll firing during Cancel could interleave bytes with the
   CFG-VALSET write. Receiver dropped the corrupted frame; ACK
   either matched a stale write or never arrived.
- 2. Buried ACK: a healthy ZED-F9P in base mode streams RTCM3 +
   NAV-PVT continuously. _wait_for_ack's 50-iteration cap couldn't
   reach the real ACK once it was buried behind backlogged RTCM
   frames in the FTDI RX buffer, raising spurious
   'No ACK/NAK response for CFG-VALSET' on writes that actually
   succeeded.
- Fix:
- Every CFG-VALSET writer (configure_survey_in, disable_base_mode,
  configure_fixed_base, configure_rtcm_messages,
  configure_rtcm_ports, configure_gnss, save_to_flash) now grabs
  self._lock directly and calls a new _send_cfg_valset_locked().
- _send_cfg_valset_locked() calls ser.reset_input_buffer() before
  each write to evict backlogged streaming traffic.
- disable_base_mode() verifies via NAV-SVIN under the same lock;
  on active=True it retries once, then raises a clear
  'Cancel did not take effect' RuntimeError.
- UI keeps Cancel visible + red banner on failure (no more
  optimistic flip to Start) and restarts the 2s poll so live
  state stays visible to the operator.
- Tests: +3 unit (RX-drain, retry-success, retry-fail) in
test_cancel_survey_in.py; patched test_rtcm_port_config.py to
target _send_cfg_valset_locked. 585 unit tests pass (was 582).
- docs(memory-bank): record v0.3.0 PyPI publish + CDN-cache gotcha

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
