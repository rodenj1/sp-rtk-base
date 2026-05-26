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

The service runs as the dedicated **`sp-rtk-base`** system user (no
shell, no home directory) added to the `dialout`, `bluetooth`, and
`plugdev` groups so it can talk to the GPS receiver (USB-serial
adapters land in `plugdev` on Raspberry Pi OS Bookworm).

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

From a fresh Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/deploy/install.sh \
    | sudo bash
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
6. Symlink the `sp-rtk-base` and `sp-rtk-base-gps-audit` CLIs into
   `/usr/local/bin/`.
7. Write a minimal default config to `/etc/sp-rtk-base/config.yaml`
   (only if one isn't already there — your existing config is never
   touched).
8. Install the `sp-rtk-base.service` systemd unit, enable + start it.
9. Print the LAN URL (`http://<pi-ip>:8080`) and a help summary.

The installer is **idempotent** — re-running it upgrades the venv,
reloads systemd, and restarts the service.

### Pin a specific version

```bash
curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/deploy/install.sh \
    | sudo bash -s -- 0.2.0
```

### Run the script from a cloned repo

```bash
git clone https://github.com/rodenj1/sp-rtk-base.git
cd sp-rtk-base
sudo ./deploy/install.sh            # latest
sudo ./deploy/install.sh 0.2.0      # pinned
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
together:

```bash
sudo tar czf sp-rtk-base-backup-$(date +%F).tar.gz \
    /etc/sp-rtk-base/ \
    /var/lib/sp-rtk-base/
```

To restore on a fresh Pi:

```bash
# (Run install.sh first, then…)
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
