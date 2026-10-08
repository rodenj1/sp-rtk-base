# Deploying SP-RTK-Base on a Raspberry Pi

This runbook installs `sp-rtk-base` from PyPI onto a Raspberry Pi (or any
Debian/Ubuntu host) as a long-running systemd service.  It follows the
Filesystem Hierarchy Standard so the appliance is independent of any
human user account:

| Path | Purpose | Owner |
|---|---|---|
| `/opt/sp-rtk-base/venv/` | Isolated Python venv with the app + dependencies | `sp-rtk-base:sp-rtk-base` |
| `/etc/sp-rtk-base/config.yaml` | Operator configuration | `root:sp-rtk-base` (0640) |
| `/var/lib/sp-rtk-base/` | Runtime state, persistent files | `sp-rtk-base:sp-rtk-base` (0750) |
| `/etc/systemd/system/sp-rtk-base.service` | systemd unit | `root:root` (0644) |
| `/usr/local/bin/sp-rtk-base` | Operator CLI (symlink into the venv) | `root:root` |
| `/usr/local/bin/sp-rtk-base-gps-audit` | u-blox config audit CLI (symlink) | `root:root` |
| `/etc/sp-rtk-base/net_provision.yaml` | Network-provisioning config, written from `$AP_SSID`/`$AP_PASSWORD` only if absent (issue #11) | `sp-rtk-base:sp-rtk-base` (0640) |
| `/etc/systemd/system/sp-rtk-base-net-provision.service` | Independent systemd unit for headless network provisioning | `root:root` (0644) |
| `/etc/polkit-1/rules.d/10-sp-rtk-base-net-provision.rules` | Grants the service account NetworkManager control | `root:root` (0644) |
| `/usr/local/bin/sp-rtk-base-net-provision` | Network-provisioning CLI (symlink) | `root:root` |
| NetworkManager connection profile named after `ap_ssid` | Setup-AP profile `NmcliAdapter` activates via `nmcli connection up/down` (issue #11) | root (NetworkManager's own keyfile store) |

The service runs as the dedicated **`sp-rtk-base`** system user (no
shell, no home directory) added to the `dialout`, `bluetooth`, and
`plugdev` groups so it can talk to the GPS receiver (USB-serial
adapters land in `plugdev` on Raspberry Pi OS Bookworm).

`sp-rtk-base-net-provision.service` is a **separate, independent**
systemd unit (issue #6, story 17) with no dependency on
`sp-rtk-base.service` in either direction — it runs the headless
Ethernet-first / WiFi-AP-fallback provisioning loop and must keep
self-healing network state even while the web app is down.

Everything in the table above from `net_provision.yaml` down is
**appliance-mode only** — see the next section.

---

## Deployment modes

`install.sh` requires an explicit `--mode` (or `MODE=` env var) the
first time it runs on a host. There's no default: guessing wrong is
destructive — auto-seizing `wlan0` on a box something else manages, or
shipping a device with no way to ever join a network. A bare re-run
with no `--mode` preserves whatever mode is already recorded in
`/etc/sp-rtk-base/config.yaml` (`deployment.mode`), so `curl |
install.sh` version-bump re-runs don't need the flag repeated.

| Concern | `appliance` | `managed-host` |
|---|---|---|
| App + relay + `sp-rtk-base.service` | ✅ | ✅ |
| Bluetooth rfkill nudge | ✅ | ✅ |
| NetworkManager install/enable | ✅ | ❌ (host stack left alone) |
| Setup-AP nmcli profile on `wlan0` | ✅ | ❌ |
| Polkit NM-control rule | ✅ | ❌ |
| `sp-rtk-base-net-provision.service` | ✅ | ❌ |
| Console **Network** page + `/api/network/*` | ✅ | ❌ hidden / 404 |
| `AP_PASSWORD` | defaults to `sp-rtk-base1234!` if unset | n/a |
| `deployment.mode` written to `config.yaml` | `appliance` | `managed-host` |

**`appliance`** — full-control install for a dedicated device shipped
to a customer: everything in [What gets configured](#what-gets-configured)
below, including the setup-AP and headless network provisioning.

**`managed-host`** — app-only install for a co-tenant Pi, NUC, or VM
where **something else owns the network stack** (systemd-networkd,
netplan, dhcpcd, an existing NetworkManager config you don't want
touched, or a VM host's virtual NIC). Installs only the app, its
systemd unit, and config — no NetworkManager, no polkit, no AP profile,
no `sp-rtk-base-net-provision.service`, and no edits to rfkill/NM state
beyond the Bluetooth nudge (Bluetooth is still needed for the BT input
source, so that one rfkill unblock happens in both modes). The
operator-console **Network** page isn't registered and `/api/network/*`
returns 404, since there's nothing here for it to control.

```bash
sudo ./deploy/install.sh --mode appliance          # full network takeover
sudo ./deploy/install.sh --mode managed-host       # app only, network untouched
```

⚠️ **Fixed default AP password.** In `appliance` mode, if `AP_PASSWORD`
is unset the first time `net_provision.yaml` is written, the installer
defaults it to `sp-rtk-base1234!` and prints a warning rather than
failing. A password shared across the whole fleet is a known risk —
acceptable only because it protects a *transient* field-setup AP behind
a physical sticker, not a normal WiFi network. Override it per fleet:

```bash
sudo AP_PASSWORD='your-sticker-password' ./deploy/install.sh --mode appliance
```

### Switching modes on an existing install

Re-running `install.sh` with a `--mode` that differs from the one
already recorded in `config.yaml` switches modes in place:

- **`appliance` → `managed-host`**: tears down every appliance network
  artifact first — stops and disables
  `sp-rtk-base-net-provision.service` and removes its unit file,
  deletes the setup-AP nmcli connection profile, removes the polkit
  rule, and reloads systemd/polkit. This is a full teardown, not just
  "stop writing new artifacts" — leaving any of it behind would keep
  fighting for `wlan0` even after `config.yaml` says `managed-host`
  (a "half-appliance zombie").
- **`managed-host` → `appliance`**: runs the full appliance
  provisioning path, applying the `AP_PASSWORD` default if unset.

```bash
# Already provisioned as an appliance; hand this Pi off to a host that
# manages its own network:
sudo ./deploy/install.sh --mode managed-host

# Reverse: turn a managed-host install into a self-contained appliance
sudo AP_PASSWORD='your-sticker-password' ./deploy/install.sh --mode appliance
```

Switching to the mode already in effect is a no-op (no teardown, no
re-provisioning). The teardown logic is shared between a mode switch
and `deploy/uninstall.sh` (`deploy/shared/net-provision-teardown.sh`)
so the two can't drift apart.

### `container` mode (planned, not yet built)

A third mode — app-only, *never* touches host networking, published as
a Docker image — is named in the design but out of scope for the
current work: it needs a `Dockerfile` and an image-publishing pipeline
(registry, CI, tag strategy) that don't exist yet. `docker/` today only
holds the `ntrip-caster` dev tool. Until it lands, `managed-host` is the
closest fit for a containerized deployment where you own the container
runtime's networking.

---

## Prerequisites

- Raspberry Pi 3 / 4 / 5 (or any 64-bit ARM / x86-64 Debian box)
- Raspberry Pi OS **Bookworm** (Debian 12) or newer / Ubuntu 22.04+
- Network connectivity to PyPI and GitHub
- `sudo` access

`apt`-installable dependencies are handled by the installer script;
nothing needs to be installed by hand first.

---

## Quick install (recommended)

From a fresh Pi. `--mode` is required the first time — see
[Deployment modes](#deployment-modes) above for the full appliance vs.
managed-host tradeoff. This walkthrough uses `appliance`, the
full-control mode; swap `--mode managed-host` (and drop `AP_PASSWORD`
entirely — it's unused in that mode) if something else on this host
already owns networking.

`AP_PASSWORD` fixes one setup-AP SSID/password across the whole fleet
(issue #6, story 8), printed once on a sticker template. If you omit it,
the installer falls back to a fixed default (`sp-rtk-base1234!`) and
warns — see the caveat above. `AP_SSID` optionally overrides the
setup-AP name (default: `sp-rtk-base-setup`). Both are only consulted
the first time `net_provision.yaml` is written — a re-run with
`net_provision.yaml` already in place ignores them.

```bash
curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/deploy/install.sh \
    | sudo AP_PASSWORD='your-sticker-password' bash -s -- --mode appliance
```

That single command will:

1. `apt install` the few OS packages we need (`python3-venv`,
   `libdbus-1-dev`, `bluez`, …).
2. Create the system user `sp-rtk-base` and add it to `dialout`,
   `bluetooth`, and `plugdev` (so it can read FTDI / CP210x USB-serial
   adapters under Raspberry Pi OS Bookworm's udev rules).
3. Lay out `/opt/sp-rtk-base/`, `/etc/sp-rtk-base/`, `/var/lib/sp-rtk-base/`
   with the correct ownership and modes.
4. Build a Python venv at `/opt/sp-rtk-base/venv/`.
5. `pip install` the latest `sp-rtk-base` release from PyPI.
6. Symlink the `sp-rtk-base`, `sp-rtk-base-gps-audit`, and
   `sp-rtk-base-net-provision` CLIs into `/usr/local/bin/`.
7. Resolve the deployment mode from `--mode`/`$MODE`, or preserve
   whatever's already in `config.yaml` on a bare re-run, and write a
   minimal default config to `/etc/sp-rtk-base/config.yaml` including
   `deployment.mode` (only if one isn't already there — your existing
   config is never touched, except to sync `deployment.mode` on an
   explicit mode switch).
8. Install the `sp-rtk-base.service` systemd unit, enable + start it.
9. **`appliance` mode only:** ensure NetworkManager is installed and
   enabled, write `net_provision.yaml` from `$AP_SSID`/`$AP_PASSWORD`
   (only if absent; defaults `AP_PASSWORD` to `sp-rtk-base1234!` with a
   warning if unset), and install the setup-AP NetworkManager connection
   profile (issue #11) — all idempotent. **`managed-host` mode skips
   this step entirely.**
10. **`appliance` mode only:** install a polkit rule granting
    `sp-rtk-base` NetworkManager control, and install + enable
    `sp-rtk-base-net-provision.service`. **Skipped in `managed-host`.**
11. Print the LAN URL (`http://<pi-ip>:8080`), the deployment mode, and
    (appliance only) the setup-AP SSID.

The installer is **idempotent** — re-running it upgrades the venv,
reloads systemd, and restarts the service. Re-running with no `--mode`
preserves the mode already recorded in `config.yaml`; re-running with a
different `--mode` switches modes (see
[Switching modes on an existing install](#switching-modes-on-an-existing-install)).

### Pin a specific version

```bash
curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/deploy/install.sh \
    | sudo AP_PASSWORD='your-sticker-password' bash -s -- --mode appliance 0.2.0
```

### Run the script from a cloned repo

```bash
git clone https://github.com/rodenj1/sp-rtk-base.git
cd sp-rtk-base
sudo AP_PASSWORD='your-sticker-password' ./deploy/install.sh --mode appliance          # latest
sudo AP_PASSWORD='your-sticker-password' ./deploy/install.sh --mode appliance 0.2.0     # pinned
sudo ./deploy/install.sh --mode managed-host                                           # app only
```

---

## What gets configured

### systemd unit (`/etc/systemd/system/sp-rtk-base.service`)

Key settings — see [`deploy/sp-rtk-base.service`](../deploy/sp-rtk-base.service)
for the canonical version:

```ini
[Service]
User=sp-rtk-base
Group=sp-rtk-base
SupplementaryGroups=dialout bluetooth plugdev
WorkingDirectory=/var/lib/sp-rtk-base
Environment=SP_RTK_BASE_CONFIG=/etc/sp-rtk-base/config.yaml
ExecStart=/opt/sp-rtk-base/venv/bin/sp-rtk-base
Restart=on-failure
```

Hardening directives (`NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome`, `PrivateTmp`, `ReadWritePaths=…`) are enabled by default
and tested on Raspberry Pi OS Bookworm.  If you hit permission errors
during bring-up, comment them out one at a time.

### Update units (`sp-rtk-base-update.service`, `sp-rtk-base-update.path`)

Installed in both modes. They let the operator install an Update from the
web UI without giving the app any privilege (see
[ADR 0005](adr/0005-update-runs-in-its-own-unit-triggered-by-a-request-file.md)):

- The app writes a request file, `/var/lib/sp-rtk-base/update/request.json`,
  naming the versions the operator read Release notes for.
- `sp-rtk-base-update.path` sees it and starts `sp-rtk-base-update.service`,
  a oneshot unit with its own sandbox that may write only `/opt/sp-rtk-base`,
  `/var/lib/sp-rtk-base` and `/etc/sp-rtk-base`.
- The unit runs `sp-rtk-base-apply-update` as `sp-rtk-base`. It deletes the
  request, resolves the newest release itself, refuses unless that is what
  the request named ("A newer release appeared; check again."), checks there
  is room for a snapshot ("not enough disk space" otherwise), copies the
  venv to `/opt/sp-rtk-base/venv.prev` and `/etc/sp-rtk-base` to
  `/opt/sp-rtk-base/config.prev`, and installs exactly
  `sp-rtk-base==X sp-rtk-base-relay==Y`. If pip fails, the snapshot is put
  back and nothing restarts.
- Its only root steps are fixed lines that restart `sp-rtk-base` and
  (if present) `sp-rtk-base-net-provision`: once after pip, and once more
  only if the rollback marker `/var/lib/sp-rtk-base/update/rollback` exists.
- **Health check and Rollback.** After the restart, the old version's code
  (run from `venv.prev`) checks the new one: within 90 s `/api/health`
  (on the host and port `sp-rtk-base.service` sets, 8080 by default, so a
  `SP_RTK_BASE_PORT` drop-in is followed) must report the new SP-Base and
  Relay, still answer 30 s later, and `NRestarts` must not have gone up. If
  not, it restores the snapshot, writes the marker, and the unit restarts
  the old version, which gets the same check. A unit stopped part-way once
  pip has started (its 15 min timeout, a crash) is rolled back the same
  way. The snapshot is deleted once the running version is healthy. If the
  old version fails too, there is no second attempt: the snapshot is kept,
  Settings says so, and recovery is `sudo deploy/upgrade.sh <previous
  version>`.
- The updater decides from its own record,
  `/opt/sp-rtk-base/update-progress.json`, which the app can't write.
  Progress and outcome are reported to the app in
  `/var/lib/sp-rtk-base/update/status.json`; the unit's log is
  `sudo journalctl -u sp-rtk-base-update`.
- **A power cut mid-Update.** Nothing finishes it; at the next start the
  app marks it failed ("… was interrupted"), with the recovery command if
  the snapshot was kept.

**Checking it on a real Pi.** The tests fake systemd. Before each release
that touches the Update mechanism, run
[Acceptance checklist: Update on a real Pi](#acceptance-checklist-update-on-a-real-pi).

**Turning Update off.** Install with `--no-update`, or disable the path
unit; the web app cannot turn it back on, and a later `install.sh` re-run
keeps it off:

```bash
sudo ./deploy/install.sh --mode managed-host --no-update
# or, on an installed base:
sudo systemctl disable --now sp-rtk-base-update.path
# and to turn it back on:
sudo systemctl enable --now sp-rtk-base-update.path
```

### Default config (`/etc/sp-rtk-base/config.yaml`)

```yaml
# sp-rtk-base config file — edit through the web UI at http://<host>:8080
# or by hand here; the service must be restarted after manual edits:
#   sudo systemctl restart sp-rtk-base

settings:
    metrics_enabled: true

deployment:
    mode: appliance     # or managed-host — set from install.sh's --mode

destinations: []
base_positions: []
```

This is just a starting point — the **vast majority of configuration
is done through the web UI** at `http://<pi-ip>:8080`.  Anything you
save in the UI is written back to this same YAML file. `deployment.mode`
is the one field the installer manages on your behalf (see
[Deployment modes](#deployment-modes)); a config predating issue #27/#28
with no `deployment` section at all is treated as `managed-host`
(fail-safe default — never auto-seize a network the app can't prove it
owns).

There is intentionally no `input:` block in the default config; the
operator chooses Serial / Bluetooth / TCP from the **Input** page on
first launch, and the YAML is populated then.  (`input:` is an
optional field on `AppConfig`.)

### Network-provisioning unit — `appliance` mode only (`/etc/systemd/system/sp-rtk-base-net-provision.service`)

Everything in this subsection — the unit itself, `net_provision.yaml`,
the setup-AP nmcli profile, and the polkit rule — is installed **only in
`appliance` mode**. `managed-host` skips all of it; see
[Deployment modes](#deployment-modes).

A second, independent systemd unit runs the headless Ethernet-first /
WiFi-AP-fallback loop — see
[`deploy/sp-rtk-base-net-provision.service`](../deploy/sp-rtk-base-net-provision.service):

```ini
[Service]
User=sp-rtk-base
Group=sp-rtk-base
WorkingDirectory=/var/lib/sp-rtk-base
Environment=SP_RTK_BASE_NET_CONFIG=/etc/sp-rtk-base/net_provision.yaml
ExecStart=/opt/sp-rtk-base/venv/bin/sp-rtk-base-net-provision
Restart=on-failure
```

It deliberately does **not** depend on `sp-rtk-base.service`, and does
**not** wait on `network-online.target` — the entire point of the loop
is to open a setup AP when there is no network, so it must be able to
start and run before connectivity exists.

`install.sh` writes `net_provision.yaml` from `$AP_SSID`/`$AP_PASSWORD`
the first time it runs in `appliance` mode, **only if the file is
absent** — a re-run never overwrites a site's provisioned config, same
contract as `config.yaml` (issue #11). `ap_password` has no default in
the `NetProvisionConfig` pydantic model itself, but as of issue #27
`install.sh` synthesizes one at the point it's actually needed: if
`AP_PASSWORD` is unset when writing a fresh `net_provision.yaml`, it
defaults to `sp-rtk-base1234!` and prints a warning, rather than failing
the install outright. Override it per fleet:

```yaml
# /etc/sp-rtk-base/net_provision.yaml
ap_ssid: "sp-rtk-base-setup"
ap_password: "your-sticker-password"
```

To reconfigure the fixed AP credentials on an already-provisioned
device: edit this file by hand, delete the matching NetworkManager
connection profile (`sudo nmcli connection delete id <old ap_ssid>`),
re-run `install.sh` to recreate the profile from the new values, then
`sudo systemctl restart sp-rtk-base-net-provision`.

`install.sh` also ensures NetworkManager itself is installed and
enabled, and installs the setup-AP NetworkManager connection profile
that `NmcliAdapter` (issue #8) activates via `nmcli connection up/down
id <ap_ssid>` — the adapter only ever brings that profile up or down,
it never creates one, so this is the one place the profile comes from.
Also idempotent: skipped if a connection profile by that name already
exists.

Because the service calls `nmcli connection up/down` with no
interactive session to authenticate against, `install.sh` also drops a
polkit rule at
[`/etc/polkit-1/rules.d/10-sp-rtk-base-net-provision.rules`](../deploy/polkit/10-sp-rtk-base-net-provision.rules)
granting the `sp-rtk-base` user unconditional NetworkManager control.
Durable clocks (`seconds_disconnected` / `seconds_in_ap`) persist to
`/var/lib/sp-rtk-base/net_provision_state.json` so a service restart
doesn't reset the fallback-window or AP-rescan timers.

### WiFi-picker captive portal

While the setup AP is up, the same process also runs a minimal HTTP
server on port 80 — this is why the systemd unit grants
`AmbientCapabilities=CAP_NET_BIND_SERVICE`. The wildcard DNS answer
(every hostname resolves to the AP's own gateway IP, which triggers
the OS's captive-portal sign-in prompt) is *not* served by
`sp-rtk-base` itself: `install.sh` drops an
`address=/#/<ap_gateway_ip>` config snippet into
`/etc/NetworkManager/dnsmasq-shared.d/`, which feeds NetworkManager's
own shared-mode `dnsmasq` instance. An earlier version ran a custom
UDP/53 responder here, but NM's own `dnsmasq` always wins real client
DNS traffic (it binds the AP's specific gateway IP; Linux's UDP demux
prefers that over a wildcard `0.0.0.0` bind), so the custom responder
never actually worked in production.

For an installer: join the AP (`ap_ssid` / `ap_password` from
`net_provision.yaml`) with a phone, and the "Sign in to network"
prompt should pop up automatically. **If it doesn't**, open a browser
and visit `http://<ap_gateway_ip>/` (default `10.42.0.1`, NetworkManager's
`shared`-mode hotspot address) — this is the manual fallback and reaches
the exact same picker page. Choose a network from the scan, enter its
password, and submit; a wrong password re-shows the form with an error
so you can retry.

---

## Day-2 operations

### Start / stop / restart

```bash
sudo systemctl start sp-rtk-base
sudo systemctl stop sp-rtk-base
sudo systemctl restart sp-rtk-base
sudo systemctl status sp-rtk-base
```

### Logs

```bash
sudo journalctl -u sp-rtk-base -f          # live tail
sudo journalctl -u sp-rtk-base --since '1 hour ago'
sudo journalctl -u sp-rtk-base --since today --no-pager
```

systemd also persists logs across reboots once you have
`Storage=persistent` in `/etc/systemd/journald.conf` (default on
Pi OS Bookworm).

### Upgrade

```bash
# Latest
sudo /opt/sp-rtk-base/venv/bin/pip install --upgrade sp-rtk-base
sudo systemctl restart sp-rtk-base

# Pinned (CI guarantees the same wheel that's on the GitHub Release)
sudo /opt/sp-rtk-base/venv/bin/pip install --upgrade sp-rtk-base==0.3.0
sudo systemctl restart sp-rtk-base
```

Or use the bundled wrapper:

```bash
sudo /opt/sp-rtk-base/venv/bin/python -m pip install -U sp-rtk-base
sudo systemctl restart sp-rtk-base
```

If you cloned the repo:

```bash
sudo ./deploy/upgrade.sh                    # latest
sudo ./deploy/upgrade.sh 0.3.0              # pinned
```

### Backup

Everything stateful lives in **two directories** — back them up
together. This also covers `net_provision.yaml` and the durable
provisioning clocks (`net_provision_state.json`), since both live
under these same paths:

```bash
sudo tar czf sp-rtk-base-backup-$(date +%F).tar.gz \
    /etc/sp-rtk-base/ \
    /var/lib/sp-rtk-base/
```

To restore on a fresh Pi (run `install.sh` with the **same `--mode`**
the backup was taken from first, then):

```bash
# appliance
sudo systemctl stop sp-rtk-base sp-rtk-base-net-provision
sudo tar xzf sp-rtk-base-backup-2026-05-20.tar.gz -C /
sudo systemctl start sp-rtk-base sp-rtk-base-net-provision

# managed-host — no net-provision unit to stop/start
sudo systemctl stop sp-rtk-base
sudo tar xzf sp-rtk-base-backup-2026-05-20.tar.gz -C /
sudo systemctl start sp-rtk-base
```

The venv at `/opt/sp-rtk-base/` is *not* in the backup — `pip install`
recreates it on demand and bit-for-bit reproducibility is guaranteed
by the PyPI artifact + sigstore attestation.

### Uninstall

Interactive:

```bash
sudo ./deploy/uninstall.sh
```

Wipe everything including config + state:

```bash
sudo ./deploy/uninstall.sh --purge
```

On an `appliance` install, either form always removes the setup-AP
NetworkManager connection profile (read out of `net_provision.yaml`
before anything else is touched) alongside the systemd units and
polkit rule — it's installer-created infrastructure, not site data, so
it isn't gated behind the config/state `[y/N]` prompts. `uninstall.sh`
reads `deployment.mode` from `config.yaml` to decide whether there's
any of this to do at all; on a `managed-host` install (which never had
these artifacts) it prints a one-line note and skips straight to
removing the app itself, with no spurious "removing AP profile" output.
The teardown logic is the same shared helper used by an `appliance` →
`managed-host` mode switch (see
[Switching modes on an existing install](#switching-modes-on-an-existing-install)).

---

## Networking

The service binds to `0.0.0.0:8080` by default — accessible from any
host on the LAN.  Common follow-ups:

### Reverse proxy with nginx (optional)

If you want HTTPS or a friendlier hostname:

```nginx
server {
    listen 443 ssl http2;
    server_name rtk.example.lan;
    ssl_certificate     /etc/ssl/rtk.example.lan.crt;
    ssl_certificate_key /etc/ssl/rtk.example.lan.key;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        # WebSocket support for /api/events/ws
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        "upgrade";
        proxy_read_timeout 86400s;
    }
}
```

### Bind to a different port

Edit the systemd unit with a drop-in:

```bash
sudo systemctl edit sp-rtk-base
```

Add:

```ini
[Service]
Environment=SP_RTK_BASE_PORT=9090
```

(Or change `ExecStart` to call `sp-rtk-base --port 9090` once the
CLI flag is added in a future release.)

### Firewall

If you use `ufw`:

```bash
sudo ufw allow 8080/tcp comment 'sp-rtk-base web UI'
```

---

## Troubleshooting

### Service won't start

```bash
sudo journalctl -u sp-rtk-base --no-pager -n 100
```

Common causes:

| Symptom | Fix |
|---|---|
| `permission denied: /dev/ttyUSB0` (or `[Errno 13]` from pyserial) | The udev rule on your distro probably owns the device as `root:plugdev` (Pi OS Bookworm + FTDI / CP210x / CH340 adapters) rather than `root:dialout`.  Run `ls -l /dev/ttyUSB0` to confirm the owning group, then: `sudo usermod -aG dialout,plugdev sp-rtk-base && sudo systemctl restart sp-rtk-base`.  Recent installer versions (≥ post-v0.2.0) add `plugdev` automatically. |
| `org.bluez.NotFound` on Bluetooth pair | `sudo systemctl restart bluetooth && sudo systemctl restart sp-rtk-base` |
| `org.bluez.Error.NotReady` on Bluetooth scan, or no devices found | See **"Bluetooth scan finds nothing"** below. |
| `OSError: [Errno 98] Address already in use` | Another service is on port 8080.  Change either port. |
| `ImportError: dbus-fast` | Run `sudo /opt/sp-rtk-base/venv/bin/pip install --force-reinstall sp-rtk-base` — the build wheel from PyPI should be picked up automatically. |

### Setup AP never appears (wlan0 unmanaged)

*(`appliance` mode only — `managed-host` never provisions a setup AP.)*

`install.sh` warns `wlan0 is unmanaged by NetworkManager` if
`nmcli device status` reports `wlan0` as `unmanaged` — NetworkManager
itself is running, but something else (commonly `dhcpcd`, or a
distro-shipped `/etc/NetworkManager/conf.d/*.conf` override) has claimed
the interface, so the setup-AP connection profile can never come up no
matter how many times `sp-rtk-base-net-provision.service` retries it.

```bash
nmcli device status                          # confirm wlan0 shows "unmanaged"
sudo systemctl status dhcpcd                 # a common culprit on older Pi OS images
sudo systemctl disable --now dhcpcd          # if dhcpcd is managing wlan0
sudo systemctl restart NetworkManager
nmcli device status                          # re-check: wlan0 should now show
                                              # "disconnected" or "connected"
```

`install.sh` deliberately doesn't do this for you automatically —
disabling a network service you didn't ask it to touch, mid
`curl | sudo bash`, is exactly the kind of surprise a headless installer
shouldn't spring on a box you might be SSH'd into over that same
interface.

### Bluetooth scan finds nothing

Symptom: the **Input → Bluetooth** scan returns zero devices, or
`journalctl -u sp-rtk-base` shows `org.bluez.Error.NotReady`.

**99% of the time it's an rfkill soft-block.**  Raspberry Pi OS Bookworm
ships with Bluetooth `rfkill`-soft-blocked by default, and
`systemd-rfkill.service` faithfully restores that "blocked" state on
every boot.  The fix has three layers — try them in order.

#### Step 1 — Diagnose

```bash
rfkill list bluetooth
# Look for:  Soft blocked: yes   ← that's the problem

sudo grep -H . /var/lib/systemd/rfkill/*bluetooth*
# Look for any line ending in :1 (1 means "blocked, restore as blocked")
```

Also verify the rest of the stack is healthy:

```bash
systemctl is-active bluetooth                  # expect: active
groups sp-rtk-base | grep -q bluetooth && echo ✓ group OK
sudo -u sp-rtk-base bluetoothctl -- show | head -3   # expect adapter info
```

#### Step 2 — Unblock + persist (most common fix)

```bash
sudo rfkill unblock bluetooth

# Set BluetoothEnabled=true in NetworkManager.state — newer NetworkManager
# (1.42+) will otherwise re-assert an rfkill block on every boot.
nm_state=/var/lib/NetworkManager/NetworkManager.state
if [[ -f "$nm_state" ]]; then
    if sudo grep -q '^BluetoothEnabled=' "$nm_state"; then
        sudo sed -i 's/^BluetoothEnabled=.*/BluetoothEnabled=true/' "$nm_state"
    else
        echo 'BluetoothEnabled=true' | sudo tee -a "$nm_state"
    fi
    sudo systemctl restart NetworkManager
fi

sudo reboot
```

After the reboot:

```bash
rfkill list bluetooth                          # expect: Soft blocked: no
sudo -u sp-rtk-base timeout 8 bluetoothctl -- scan on 2>&1 | head -20
```

A clean shutdown lets `systemd-rfkill.service` save the unblocked
state to `/var/lib/systemd/rfkill/*bluetooth*` (`:0`), so subsequent
boots come up unblocked.  (The installer's Step 7.6 runs these two
commands for you on first install — this section is for fixing an
existing install or recovering after someone disabled BT via the GUI.)

#### Step 3 — Fleet-bulletproof fallback: tell NetworkManager to never touch Bluetooth

If Bluetooth still re-blocks after Step 2 (rare, usually NetworkManager
versions 1.42+ with unusual settings), drop in this config snippet to
take the killswitch out of NM's hands entirely:

```bash
sudo tee /etc/NetworkManager/conf.d/sp-rtk-base-no-bt.conf >/dev/null <<'EOF'
[main]
# sp-rtk-base manages Bluetooth via bluez directly; do not let
# NetworkManager rfkill-block the adapter.
rfkill-bluetooth=ignore
EOF
sudo systemctl restart NetworkManager
sudo rfkill unblock bluetooth
sudo reboot
```

(`rfkill-bluetooth=ignore` is documented in the upstream NetworkManager
rfkill reference: <https://networkmanager.dev/docs/rfkill/>.)

#### Step 4 — Kernel-cmdline last resort

If even Step 3 doesn't stick (which would point at a non-NM rfkill
source — uncommon on Pi OS), add the kernel parameter so the rfkill
subsystem defaults to "unblocked" *before* userspace runs:

```bash
# Bookworm path (older Pi OS uses /boot/cmdline.txt instead)
sudo sed -i 's/$/ rfkill.default_state=1/' /boot/firmware/cmdline.txt
sudo reboot
```

`rfkill.default_state=1` means "default to unblocked at boot"
([systemd-rfkill docs](https://www.man7.org/linux/man-pages/man8/systemd-rfkill.8.html)).

### Verify the wheel signature (paranoid mode)

```bash
sudo /opt/sp-rtk-base/venv/bin/pip install sigstore
sudo /opt/sp-rtk-base/venv/bin/sigstore verify identity \
    --cert-identity 'https://github.com/rodenj1/sp-rtk-base/.github/workflows/release.yml@refs/tags/v0.2.0' \
    --cert-oidc-issuer 'https://token.actions.githubusercontent.com' \
    <(curl -L https://github.com/rodenj1/sp-rtk-base/releases/download/v0.2.0/sp_rtk_base-0.2.0-py3-none-any.whl)
```

The `--cert-identity` value is the GitHub Actions workflow path
that PyPI's Trusted Publisher attests built the wheel.

### Run the audit CLI

```bash
sudo -u sp-rtk-base sp-rtk-base-gps-audit --help
sudo -u sp-rtk-base sp-rtk-base-gps-audit --port /dev/ttyUSB0
```

(Running as the same user avoids permission edge cases on the serial
device.)

---

## Acceptance checklist: Update on a real Pi

Update from the web UI depends on real systemd: the path unit, the update
unit's sandbox, its root restart lines and `NRestarts`. The tests fake all
of these, so a real Raspberry Pi is the only place to prove them. Run this
checklist once per release that touches the Update mechanism (the updater,
the Update units, the health check, `status.json`, Host setup), then paste
the [results template](#results) into the release's acceptance ticket.

It takes about an hour per mode. Do the steps in order; each one starts
from the state the previous one left.

### What you need

- A Raspberry Pi on Raspberry Pi OS Bookworm with network access. You need
  two clean installs, one per deployment mode: re-flash the SD card between
  [Part A](#part-a-appliance) and [Part B](#part-b-managed-host), or use
  two cards.
- A u-blox receiver on the Console link, plus an Input and a Destination
  you can configure, so the Relay can run and a Survey-in can start.
- A browser on the same LAN, open at `http://<pi-ip>:8080`, and two SSH
  sessions to the Pi: one to run commands, one to follow the update log.

Three versions are used:

| Name | What it is |
|------|------------|
| `R` | The release under test: the `version` in `pyproject.toml` at its commit. It may already be on PyPI, or not yet: run this before tagging, from the version-bump commit. |
| `G` | `R.post1`: a good test release, the same code as `R`. |
| `B` | `R.post2`: a broken test release that raises on import, so it fails its health check. |

`G` and `B` are built on the Pi and offered only to this Pi, by a **fake
release source**: the PyPI JSON documents and GitHub files that release
resolution reads, served from a directory that `SP_RTK_BASE_FAKE_PYPI_DIR`
names. The updater's pip finds the wheels through `PIP_FIND_LINKS`. Both
reach the units through `systemctl` drop-ins. Nothing is uploaded
anywhere. The kit lives in `/srv/sp-rtk-base-test`, because the units'
sandboxes (`ProtectHome`, `PrivateTmp`) can't see `/home` or `/tmp`.

### Part A: appliance

#### A1. A pre-Update base shows nothing new

- [ ] Install 0.9.0, the last release without Update, from its own tag:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone --branch v0.9.0 https://github.com/rodenj1/sp-rtk-base.git ~/sp-rtk-base-0.9.0
cd ~/sp-rtk-base-0.9.0
sudo AP_PASSWORD='your-sticker-password' ./deploy/install.sh --mode appliance 0.9.0
```

- [ ] Check that no Update units are installed and that 0.9.0 is running:

```bash
systemctl list-unit-files 'sp-rtk-base-update*'    # expect: 0 unit files listed.
curl -s http://127.0.0.1:8080/api/health; echo     # expect: "version":"0.9.0"
```

- [ ] **Web UI:** Settings has the old **Version Information** card and no
  **Version & Update** card. The header shows no badge next to "SP-Base".
- [ ] Set the base up as it would be in the field. Configure an Input and a
  Destination. On Settings, turn on **Auto-start relay on application
  launch** and press **Save Settings**. On the Dashboard, press **Start**,
  then check that the Relay runs and corrections reach the Destination.

#### A2. Build the test kit

- [ ] Clone the release under test and check its version:

```bash
git clone https://github.com/rodenj1/sp-rtk-base.git ~/sp-rtk-base
git -C ~/sp-rtk-base checkout <R's tag or commit>
grep '^version' ~/sp-rtk-base/pyproject.toml       # this is R
sudo install -d -o "$USER" -g "$USER" /srv/sp-rtk-base-test
python3 -m venv /srv/sp-rtk-base-test/buildenv
```

- [ ] Write the helpers. Set `R` on the first line to the version above:

```bash
cat > /srv/sp-rtk-base-test/env.sh <<'EOF'
export R=0.10.0                  # <- the release under test
export G=$R.post1 B=$R.post2
export T=/srv/sp-rtk-base-test SRC=$HOME/sp-rtk-base

# make_release VERSION [broken]: a wheel of $SRC's HEAD as VERSION, into $T/wheels.
make_release() {
    rm -rf "$T/build" && mkdir -p "$T/build" "$T/wheels"
    git -C "$SRC" archive HEAD | tar -x -C "$T/build"
    sed -i "s/^version = \".*\"/version = \"$1\"/" "$T/build/pyproject.toml"
    sed -i "s/^__version__ = \".*\"/__version__ = \"$1\"/" "$T/build/src/sp_rtk_base/__init__.py"
    if [ "${2:-}" = broken ]; then
        echo 'raise RuntimeError("deliberately broken test release")' \
            >> "$T/build/src/sp_rtk_base/__init__.py"
    fi
    "$T/buildenv/bin/pip" wheel --quiet --no-deps -w "$T/wheels" "$T/build"
}

# make_index: the fake release source in $T/index, offering every wheel in $T/wheels.
make_index() { sudo T="$T" /opt/sp-rtk-base/venv/bin/python "$T/make-index.py"; }

# offer_test_releases / stop_offering: point the app and the update unit at it, or back at PyPI.
offer_test_releases() {
    sudo mkdir -p /etc/systemd/system/sp-rtk-base.service.d /etc/systemd/system/sp-rtk-base-update.service.d
    printf '[Service]\nEnvironment=SP_RTK_BASE_FAKE_PYPI_DIR=%s/index\n' "$T" \
        | sudo tee /etc/systemd/system/sp-rtk-base.service.d/acceptance.conf >/dev/null
    printf '[Service]\nEnvironment=SP_RTK_BASE_FAKE_PYPI_DIR=%s/index\nEnvironment=PIP_FIND_LINKS=%s/wheels\n' "$T" "$T" \
        | sudo tee /etc/systemd/system/sp-rtk-base-update.service.d/acceptance.conf >/dev/null
    sudo systemctl daemon-reload && sudo systemctl restart sp-rtk-base
}
stop_offering() {
    sudo rm -f /etc/systemd/system/sp-rtk-base.service.d/acceptance.conf \
        /etc/systemd/system/sp-rtk-base-update.service.d/acceptance.conf
    sudo systemctl daemon-reload && sudo systemctl restart sp-rtk-base
}

health() { curl -s http://127.0.0.1:8080/api/health; echo; }
update_status() { sudo cat /var/lib/sp-rtk-base/update/status.json | python3 -m json.tool; }
snapshot_gone() { ls /opt/sp-rtk-base; sudo ls -A /var/lib/sp-rtk-base/update; }
EOF
```

- [ ] Write the fake release source generator:

```bash
cat > /srv/sp-rtk-base-test/make-index.py <<'EOF'
"""The fake release source for SP_RTK_BASE_FAKE_PYPI_DIR, from the wheels
in $T/wheels: each one is an SP-Base release with its Relay pin, its Host
setup and a changelog section. The Relay is offered at the version
installed now, so pip needs nothing new for it."""
import datetime, email, json, os, re, shutil, zipfile
from importlib.metadata import version
from pathlib import Path

T = Path(os.environ["T"])
index = T / "index"
shutil.rmtree(index, ignore_errors=True)
raw = index / "raw.githubusercontent.com/rodenj1/sp-rtk-base"
relay = version("sp-rtk-base-relay")
apps, notes = {}, []
for wheel in sorted((T / "wheels").glob("sp_rtk_base-*.whl")):
    with zipfile.ZipFile(wheel) as z:
        name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        meta = email.message_from_bytes(z.read(name))
        host = z.read("sp_rtk_base/update/host_setup.py").decode()
    v = meta["Version"]
    plumbing = re.search(r"^PLUMBING_VERSION = (\d+)", host, re.M)[1]
    apps[v] = {"requires_python": meta["Requires-Python"],
               "requires_dist": meta.get_all("Requires-Dist")}
    (raw / f"v{v}/deploy").mkdir(parents=True)
    (raw / f"v{v}/deploy/plumbing-version").write_text(plumbing + "\n")
    notes.append(f"## v{v} ({datetime.date.today()})\n\n"
                 f"- Acceptance test build {wheel.name}.\n")
    print(f"offering sp-rtk-base {v} (Host setup {plumbing}) with Relay {relay}")
for v, info in apps.items():
    (index / f"sp-rtk-base-{v}.json").write_text(
        json.dumps({"info": {"version": v, **info}}))
    (raw / f"v{v}/CHANGELOG.md").write_text(
        "# Changelog\n\n" + "\n".join(reversed(notes)))

def releases(found):
    return json.dumps({"releases": {
        v: [{"filename": "test.whl", "requires_python": rp, "yanked": False}]
        for v, rp in found.items()}})

(index / "sp-rtk-base.json").write_text(
    releases({v: i["requires_python"] for v, i in apps.items()}))
(index / "sp-rtk-base-relay.json").write_text(releases({relay: None}))
EOF
```

- [ ] Load the helpers, then build the wheels. Run `source` again in every
  new SSH session:

```bash
source /srv/sp-rtk-base-test/env.sh
make_release "$R"     # only if R is not on PyPI yet
make_release "$G"
ls "$T/wheels"        # expect: sp_rtk_base-<R>-py3-none-any.whl (if built) and sp_rtk_base-<G>-py3-none-any.whl
```

  The first build takes a minute or two: pip downloads the `uv_build`
  backend. Don't build `B` yet. The fake release source offers the newest
  wheel in `$T/wheels`, so `B` would be offered in place of `G`.

#### A3. Bootstrap: one `install.sh` re-run

- [ ] Re-run the installer once, from the release under test. A bare
  re-run keeps the mode. `PIP_FIND_LINKS` lets pip find `R` when it isn't
  on PyPI yet, and is harmless when it is:

```bash
cd "$SRC" && sudo PIP_FIND_LINKS="$T/wheels" ./deploy/install.sh "$R"
```

  Expect `Installed sp-rtk-base <R>` and `Update units installed; Update
  from the web UI is on`.
- [ ] Check the Host setup. Then follow the update log in your second SSH
  session, and leave it running:

```bash
systemctl show sp-rtk-base-update.service -p LoadState -p Environment
#   expect: LoadState=loaded; Environment includes SP_RTK_BASE_PLUMBING=<deploy/plumbing-version>
systemctl show sp-rtk-base-update.path -p UnitFileState -p ActiveState
#   expect: UnitFileState=enabled, ActiveState=active
systemctl is-active sp-rtk-base sp-rtk-base-net-provision    # expect: active, active
health                                                       # expect: "version":"<R>"
```

```bash
sudo journalctl -u sp-rtk-base-update -f        # second SSH session
```

- [ ] **Web UI:** Settings now has the **Version & Update** card. It shows
  "Last checked <d Mon HH:MM>", the SP-Base row on R, the SP-Base Relay row,
  "Up to date.", and the Python and Platform rows. It shows no Host setup
  warning. The footer reads "SP-Base v<R>". Your Input, Destination and
  auto-start setting are unchanged.

#### A4. Happy path

- [ ] Offer `G` to this Pi:

```bash
make_index              # expect: offering sp-rtk-base <G> (Host setup 1) with Relay <relay>
offer_test_releases
```

- [ ] Check that the Relay is running. On the Dashboard, the Relay shows as
  running (press **Start** if not), and its counters move:

```bash
curl -s http://127.0.0.1:8080/api/relay/status | python3 -m json.tool | grep -E '"running"|chunks_distributed'
```

- [ ] **Badge:** the header shows a teal **Update <G>** badge, which links
  to Settings. If it doesn't, press **Check now** on the card.
- [ ] **Notes:** the card shows SP-Base "R → G" and the Relay row without
  an arrow. Under **Release notes**, the SP-Base tab shows "SP-Base <G> ·
  <today>" with "Acceptance test build sp_rtk_base-<G>-py3-none-any.whl.",
  and the Relay tab says "The Relay stays on <relay>."
- [ ] Press **Update to <G>**. The dialog is titled "Update to <G>?" and
  says "SP-Base <R> → <G>, Relay <relay> (unchanged). The base restarts;
  this page reconnects by itself." Because the Relay is running, it also
  shows the amber line "The Relay is running. Corrections stop for about a
  minute and resume on their own." Press **Update**.
- [ ] **Phases:** the badge turns orange and reads "Updating…". The bar
  under the versions moves through "Waiting for the host… (step 1 of 5)",
  "Checking the release… (step 2 of 5)", "Installing… (step 3 of 5)",
  "Restarting… (step 4 of 5)" and "Checking it started… (step 5 of 5)".
  The banner on every page reads "Updating to <G>…", then "Restarting into
  <G>. This page reconnects by itself.", then "Checking <G> started…".
  The early phases can pass in under a second. pip takes a few minutes on
  a Pi, and the health check holds for 30 s after the new version answers.
- [ ] **Restart:** the page reconnects by itself, without a manual reload.
- [ ] **"Now on X":** the banner reads "Now on <G>." with a close button.
  The card's outcome stripe reads "Updated <R> → <G> on <d Mon HH:MM>.",
  the card says "Up to date.", the badge is gone, and the footer reads
  "SP-Base v<G>".
- [ ] Check the host side:

```bash
health            # expect: "version":"<G>", "relay_version":"<relay>"
update_status     # expect: "phase": "done", "from" R, "to" G, "error": null, "rolled_back": false
snapshot_gone     # expect: no venv.prev, no config.prev; no request.json, no rollback
systemctl show sp-rtk-base-update.service -p Result     # expect: Result=success
systemctl show sp-rtk-base-net-provision -p ActiveState -p ActiveEnterTimestamp
#   expect: active, entered at the time of the Update (the unit's try-restart line)
```

  In the second session, the update log has no `ERROR` lines and the run
  ends with systemd's `Finished sp-rtk-base-update.service`.
- [ ] **Corrections resume through auto-start:** without pressing
  **Start**, the Dashboard shows the Relay running within about a minute of
  the restart, and corrections reach the Destination again:

```bash
sudo journalctl -u sp-rtk-base --since '10 min ago' | grep 'Auto-started relay engine'
curl -s http://127.0.0.1:8080/api/relay/status | python3 -m json.tool | grep -E '"running"|chunks_distributed'
```

- [ ] Dismiss the banner, then reload the page. The banner stays dismissed.

#### A5. Rollback: a broken release

- [ ] Build `B` and offer it. Its `__init__.py` raises on import, so it
  can't answer `/api/health`:

```bash
make_release "$B" broken
make_index              # expect: offering ... <B> (Host setup 1) ...
```

- [ ] On Settings, press **Check now**. The badge reads **Update <B>**.
  Check that the Relay is running, then press **Update to <B>**, then
  **Update**.
- [ ] Watch for up to about 5 minutes: the install, a 90 s wait for `B`, then
  the old version's own health check. The banner reads "Restarting into
  <B>. This page reconnects by itself.", then "Checking <B> started…".
  While `B` fails, the page can't reach the base and shows it is
  disconnected. Once `G` is back, the page reconnects. While `G` passes its
  own health check, it may show "<B> failed to start; rolling back to
  <G>…" with the bar "Rolling back to <G>… (step 5 of 5)".
- [ ] **Outcome:** the banner reads "Update to <B> failed to start; still
  on <G>.". The outcome stripe reads "Update to <B> failed to start;
  rolled back to <G> on <d Mon HH:MM>.". **Update to <B>** is still
  offered, with "<B> failed to start here on <d Mon HH:MM>." above it. The
  footer reads "SP-Base v<G>".
- [ ] Check the host side:

```bash
health            # expect: "version":"<G>"
update_status     # expect: "phase": "failed", "reason": "failed_to_start", "rolled_back": true,
                  #   "rollback_error": null, "from" G, "to" B,
                  #   "error": "SP-Base <B> didn't start within 90 s: no answer from http://127.0.0.1:8080/api/health ..."
snapshot_gone     # expect: no venv.prev, no config.prev; no rollback marker
systemctl show sp-rtk-base-update.service -p Result     # expect: Result=exit-code
systemctl is-active sp-rtk-base                         # expect: active
sudo journalctl -u sp-rtk-base --since '10 min ago' | grep 'deliberately broken test release'   # B's crash
```

  In the second session, the update log shows `ERROR Rolling back: SP-Base
  <B> didn't start within 90 s: …`, then the unit failing with result
  'exit-code' after its stop-post lines restarted `G`.
- [ ] The Relay auto-starts on `G` again, as in A4.

#### A6. Turned off: disabling the path unit

`B` stays offered for A6 to A8. Never press **Update** in these steps.

- [ ] Turn Update off:

```bash
sudo systemctl disable --now sp-rtk-base-update.path
systemctl show sp-rtk-base-update.path -p UnitFileState -p ActiveState   # expect: disabled, inactive
```

- [ ] **Web UI:** within a few seconds, Settings shows "Update is turned off
  on this host." in amber, and **Update to <B>** is disabled.
- [ ] **A planted request does nothing.** Write a request as the service
  user, the way the app would. If anything picked it up, the updater would
  refuse it, and `status.json` would change:

```bash
sudo stat -c '%y' /var/lib/sp-rtk-base/update/status.json
echo '{"format": 1, "app": "0.0.0", "relay": "0.0.0"}' \
    | sudo -u sp-rtk-base tee /var/lib/sp-rtk-base/update/request.json >/dev/null
sleep 60
sudo ls /var/lib/sp-rtk-base/update                    # expect: request.json still there
sudo stat -c '%y' /var/lib/sp-rtk-base/update/status.json   # expect: same time as before
sudo journalctl -u sp-rtk-base-update --since '2 min ago' # expect: -- No entries --
```

- [ ] Remove the request **before** turning Update back on. Otherwise the
  path unit fires on it at once:

```bash
sudo rm /var/lib/sp-rtk-base/update/request.json
sudo systemctl enable --now sp-rtk-base-update.path
```

  The amber line goes away, and **Update to <B>** is enabled again.

#### A7. Turned off: `--no-update`

- [ ] Re-run the installer with `--no-update`. The pin keeps the base on
  `G`:

```bash
cd "$SRC" && sudo PIP_FIND_LINKS="$T/wheels" ./deploy/install.sh --no-update "$G"
```

  Expect `Update is turned off on this host (--no-update). To turn it on:`.
  `systemctl show sp-rtk-base-update.path -p UnitFileState` reports
  `disabled`.
- [ ] **Web UI:** "Update is turned off on this host." again.
- [ ] Repeat the planted-request check from A6, including removing the
  request afterwards. Expect the same result.
- [ ] A bare re-run keeps Update off:

```bash
cd "$SRC" && sudo PIP_FIND_LINKS="$T/wheels" ./deploy/install.sh "$G"
```

  Expect the warning `Update stays turned off on this host
  (sp-rtk-base-update.path is disabled). To turn it on:`, and the web UI
  still says it's turned off.
- [ ] Turn it back on with `sudo systemctl enable --now
  sp-rtk-base-update.path`. The amber line goes away.

#### A8. Refusals

Keep Settings open in one browser tab and Survey in another.

- [ ] On Survey, **Connect** the Console link. Settings shows "A Console
  link is connected. Disconnect it to update.", and **Update to <B>** is
  disabled.
- [ ] Press **Start Survey-In**. Settings shows "A Survey-in is running.
  Update once it has finished.", and **Update to <B>** is disabled.
- [ ] The API refuses too. Run this only while one of the two refusals
  above is showing. A 202 answer would start an Update to `B`, which would
  roll back:

```bash
RELAY=$(health | python3 -c 'import json,sys; print(json.load(sys.stdin)["relay_version"])')
curl -s -w ' HTTP %{http_code}\n' -X POST -H 'Content-Type: application/json' \
    -d "{\"app\": \"$B\", \"relay\": \"$RELAY\"}" http://127.0.0.1:8080/api/update
#   expect: {"code":"survey_running",...} HTTP 409 (or "console_connected" once the survey is cancelled)
```

- [ ] Press **Cancel Survey**. The Console link message returns. Press
  **Disconnect**. The message goes away, and **Update to <B>** is enabled.

#### A9. Clean up

- [ ] Stop offering the test releases, and put the base back on a real
  release. Drop `PIP_FIND_LINKS` if `R` is on PyPI:

```bash
stop_offering
cd "$SRC" && sudo PIP_FIND_LINKS="$T/wheels" ./deploy/upgrade.sh "$R"
health            # expect: "version":"<R>"
```

### Part B: managed-host

Start from a clean Pi OS install. Part B checks a bootstrap in this mode,
the Host-setup-missing block on a base upgraded by hand, and one happy
path without the network-provisioning unit.

#### B1. A pre-Update base shows nothing new

- [ ] As in A1, but in this mode:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone --branch v0.9.0 https://github.com/rodenj1/sp-rtk-base.git ~/sp-rtk-base-0.9.0
cd ~/sp-rtk-base-0.9.0 && sudo ./deploy/install.sh --mode managed-host 0.9.0
systemctl list-unit-files 'sp-rtk-base*'   # expect: sp-rtk-base.service only
```

- [ ] **Web UI:** there's no **Version & Update** card and no badge. Set up
  the Input, the Destination and auto-start, and start the Relay, as in A1.

#### B2. Build the test kit

- [ ] Repeat A2 exactly: clone, `env.sh`, `make-index.py`,
  `source /srv/sp-rtk-base-test/env.sh`, then build `R` (if it isn't on
  PyPI) and `G`. Don't build `B`.

#### B3. Host setup missing

- [ ] Upgrade to `R` with pip only, the way a base updated by hand gets
  there. This lays down no Update units:

```bash
cd "$SRC" && sudo PIP_FIND_LINKS="$T/wheels" ./deploy/upgrade.sh "$R"
systemctl list-unit-files 'sp-rtk-base-update*'    # expect: 0 unit files listed.
```

- [ ] **Web UI:** Settings has the **Version & Update** card. With no
  update offered yet it says "Update needs a one-time setup on this host.
  Run this on the base, then come back:", above a code block holding
  `curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/deploy/install.sh | sudo bash`.
- [ ] Offer `G`:

```bash
make_index && offer_test_releases
```

  The badge reads **Update <G>**. The card shows "Update needs a one-time
  setup on this host. Run this on the base, then come back:", with the
  same command block, once, and **Update to <G>** is disabled.

#### B4. Bootstrap: one `install.sh` re-run

- [ ] Run the one-time command. If `R` is on PyPI and tagged, copy it from
  the card and run it as shown. If not, run the same re-run from the
  checkout:

```bash
cd "$SRC" && sudo PIP_FIND_LINKS="$T/wheels" ./deploy/install.sh "$R"
```

  Expect `Update units installed; Update from the web UI is on`.
- [ ] Check the same `systemctl show` lines as in A3: `LoadState=loaded`,
  `SP_RTK_BASE_PLUMBING=<n>`, the path unit enabled and active.
  `systemctl list-unit-files 'sp-rtk-base*'` lists no
  `sp-rtk-base-net-provision`.
- [ ] **Web UI:** within a few seconds, the refusal and its command go
  away, and **Update to <G>** is enabled.

#### B5. Happy path

- [ ] Follow the update log in the second session
  (`sudo journalctl -u sp-rtk-base-update -f`). With the Relay running,
  update to `G` as in A4. Expect the same badge, phases and restart, then
  "Now on <G>.".
- [ ] Check the host side: `health` reports `G`, `update_status` reports
  `"phase": "done"`, and `snapshot_gone` shows no `venv.prev`. The update
  log has no `ERROR` lines (the `try-restart` of the absent
  network-provisioning unit is a no-op). The Relay auto-starts and
  corrections resume.

#### B6. Clean up

- [ ] Run `stop_offering`, then the `upgrade.sh "$R"` line from A9.

### Results

Paste this into the acceptance ticket, filled in. Mark anything that
didn't match with `[ ]`, and say what you saw instead.

```markdown
## Hardware acceptance: Update on a real Pi

- Date:
- Pi model / OS: (e.g. Pi 4B 4 GB, Raspberry Pi OS Bookworm 64-bit, `uname -a`)
- R (release under test): , Relay:
- R was: [ ] on PyPI  [ ] built from commit `<sha>`
- Test releases: G = , B =

### appliance (Part A)
- [ ] Bootstrap: 0.9.0 showed nothing new; one install.sh re-run gave Host setup (SP_RTK_BASE_PLUMBING=__) and the Version & Update card
- [ ] Happy path: badge, notes, Update with the Relay running, phases, restart, "Now on G.", Relay auto-started and corrections resumed
  - Time from pressing Update to "Now on G.": __ min
- [ ] Rollback: B failed its health check and was rolled back; banner, outcome stripe and "failed to start here" shown; no snapshot or marker left
  - Time from pressing Update to "still on G": __ min
  - status.json `error`:
- [ ] Turned off (path unit disabled): "Update is turned off on this host."; planted request did nothing
- [ ] Turned off (--no-update): same; a bare re-run kept it off
- [ ] Refusals: Console link connected; Survey-in running (UI and API 409)

### managed-host (Part B)
- [ ] Bootstrap: 0.9.0 showed nothing new; one install.sh re-run gave Host setup and the Version & Update card
- [ ] Host setup missing (after a pip-only upgrade): warning and refusal showed the one-time command
- [ ] Happy path without the network-provisioning unit: "Now on G.", corrections resumed

### Notes
(anything unexpected; paste the relevant `journalctl -u sp-rtk-base-update` lines)
```

---

## Multiple Pis

For a fleet, the easiest pattern is:

1. Configure one Pi end-to-end through the web UI.
2. Copy `/etc/sp-rtk-base/config.yaml` to every other Pi.
3. Run the installer with the same version pin on each.

If you need device-specific values (e.g. different mountpoint names
per location), keep a per-host `config.yaml` in your Ansible /
SaltStack repo and template it at deploy time.

---

## See also

- [`docs/release-process.md`](release-process.md) — how new versions
  get cut and published.
- [`docs/ci-setup.md`](ci-setup.md) — CI workflow internals.
- [`CHANGELOG.md`](../CHANGELOG.md) — what changed between versions.
