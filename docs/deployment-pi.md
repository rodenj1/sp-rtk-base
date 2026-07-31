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

From a fresh Pi. `AP_PASSWORD` is required the first time — issue #6's
story 8 fixes one setup-AP SSID/password across the whole fleet, printed
once on a sticker template, so it's never baked into source and has to
come from you:

```bash
curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/deploy/install.sh \
    | sudo AP_PASSWORD='your-sticker-password' bash
```

`AP_SSID` optionally overrides the setup-AP name (default:
`sp-rtk-base-setup`). Both are only consulted the first time
`net_provision.yaml` is written — a re-run with `net_provision.yaml`
already in place ignores them.

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
7. Write a minimal default config to `/etc/sp-rtk-base/config.yaml`
   (only if one isn't already there — your existing config is never
   touched).
8. Install the `sp-rtk-base.service` systemd unit, enable + start it.
9. Ensure NetworkManager is installed and enabled, write
   `net_provision.yaml` from `$AP_SSID`/`$AP_PASSWORD` (only if absent),
   and install the setup-AP NetworkManager connection profile (issue #11)
   — all idempotent.
10. Install a polkit rule granting `sp-rtk-base` NetworkManager control,
    and install + enable `sp-rtk-base-net-provision.service`.
11. Print the LAN URL (`http://<pi-ip>:8080`), the setup-AP SSID, and a
    help summary.

The installer is **idempotent** — re-running it upgrades the venv,
reloads systemd, and restarts the service.

### Pin a specific version

```bash
curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/deploy/install.sh \
    | sudo AP_PASSWORD='your-sticker-password' bash -s -- 0.2.0
```

### Run the script from a cloned repo

```bash
git clone https://github.com/rodenj1/sp-rtk-base.git
cd sp-rtk-base
sudo AP_PASSWORD='your-sticker-password' ./deploy/install.sh            # latest
sudo AP_PASSWORD='your-sticker-password' ./deploy/install.sh 0.2.0      # pinned
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

### Default config (`/etc/sp-rtk-base/config.yaml`)

```yaml
# sp-rtk-base config file — edit through the web UI at http://<host>:8080
# or by hand here; the service must be restarted after manual edits:
#   sudo systemctl restart sp-rtk-base

settings:
    metrics_enabled: true

destinations: []
base_positions: []
```

This is just a starting point — the **vast majority of configuration
is done through the web UI** at `http://<pi-ip>:8080`.  Anything you
save in the UI is written back to this same YAML file.

There is intentionally no `input:` block in the default config; the
operator chooses Serial / Bluetooth / TCP from the **Input** page on
first launch, and the YAML is populated then.  (`input:` is an
optional field on `AppConfig`.)

### Network-provisioning unit (`/etc/systemd/system/sp-rtk-base-net-provision.service`)

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
the first time it runs, **only if the file is absent** — a re-run never
overwrites a site's provisioned config, same contract as `config.yaml`
(issue #11). `ap_password` has no default in the model on purpose, so if
you skip `AP_PASSWORD` on a truly fresh install the script fails loudly
with instructions rather than guessing:

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
server (port 80) and a wildcard DNS responder (port 53) — this is why
the systemd unit grants `AmbientCapabilities=CAP_NET_BIND_SERVICE`.

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

To restore on a fresh Pi:

```bash
# (Run install.sh first, then…)
sudo systemctl stop sp-rtk-base sp-rtk-base-net-provision
sudo tar xzf sp-rtk-base-backup-2026-05-20.tar.gz -C /
sudo systemctl start sp-rtk-base sp-rtk-base-net-provision
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

Either form always removes the setup-AP NetworkManager connection
profile (read out of `net_provision.yaml` before anything else is
touched) alongside the systemd units and polkit rule — it's
installer-created infrastructure, not site data, so it isn't gated
behind the config/state `[y/N]` prompts.

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
