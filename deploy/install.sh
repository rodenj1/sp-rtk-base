#!/usr/bin/env bash
# ============================================================================
# sp-rtk-base — Raspberry Pi / Debian installer
# ============================================================================
#
# Installs sp-rtk-base from PyPI into an isolated venv at /opt/sp-rtk-base/
# under a dedicated `sp-rtk-base` system user, then enables a systemd
# service so the relay starts at boot. Also sets up headless network
# provisioning (issue #6/#11): ensures NetworkManager, installs the
# fixed-credential setup-AP connection profile, and enables the
# independent sp-rtk-base-net-provision.service.
#
# Usage:
#   sudo AP_PASSWORD=xxxx ./deploy/install.sh          # install latest from PyPI
#   sudo AP_PASSWORD=xxxx ./deploy/install.sh 0.2.0    # pin to a specific version
#   sudo VERSION=0.2.0 AP_PASSWORD=xxxx ./deploy/install.sh  # same, via env var
#
# Or one-shot from a fresh Pi:
#   curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/deploy/install.sh \
#       | sudo AP_PASSWORD=xxxx bash
#
# AP_PASSWORD is the fixed setup-AP WPA2 passphrase for this fleet (issue
# #6, story 8: one sticker template for every unit) — required the first
# time net_provision.yaml is written, has no default, and is only read
# then (never overwrites an existing net_provision.yaml). AP_SSID
# optionally overrides the setup-AP name (default: sp-rtk-base-setup).
#
# Re-running is safe: the script is idempotent (creates user/dirs if missing,
# upgrades the venv in place, reloads systemd).  Config in /etc/sp-rtk-base/
# is never overwritten.
# ============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration knobs (override via environment variables before invoking)
# ---------------------------------------------------------------------------
APP_NAME="sp-rtk-base"
SERVICE_USER="${SERVICE_USER:-sp-rtk-base}"
INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/sp-rtk-base}"
VENV_DIR="${INSTALL_PREFIX}/venv"
CONFIG_DIR="${CONFIG_DIR:-/etc/sp-rtk-base}"
STATE_DIR="${STATE_DIR:-/var/lib/sp-rtk-base}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
SYSTEMD_UNIT="${SYSTEMD_UNIT:-/etc/systemd/system/sp-rtk-base.service}"
NET_PROVISION_SYSTEMD_UNIT="${NET_PROVISION_SYSTEMD_UNIT:-/etc/systemd/system/sp-rtk-base-net-provision.service}"
POLKIT_RULES_DIR="${POLKIT_RULES_DIR:-/etc/polkit-1/rules.d}"
POLKIT_RULE_DEST="${POLKIT_RULE_DEST:-${POLKIT_RULES_DIR}/10-sp-rtk-base-net-provision.rules}"
# Fixed setup-AP identity (issue #6, story 8): every deployed unit ships
# the *same* SSID/password so one sticker template covers the fleet.
# AP_SSID defaults to NetProvisionConfig's DEFAULT_AP_SSID; AP_PASSWORD
# has no default on purpose (see NetProvisionConfig docstring) — it must
# be supplied via env var, e.g. from your provisioning pipeline's secret
# store, so it never ends up baked into source or PyPI.
AP_SSID="${AP_SSID:-sp-rtk-base-setup}"
AP_PASSWORD="${AP_PASSWORD:-}"
VERSION="${1:-${VERSION:-}}"          # empty => latest from PyPI

REPO_RAW_BASE="https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main"

# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YEL=$'\033[33m'
    C_BLU=$'\033[34m'; C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YEL=""; C_BLU=""; C_DIM=""; C_RESET=""
fi
log()  { echo "${C_BLU}==>${C_RESET} $*"; }
ok()   { echo "${C_GREEN}✓${C_RESET} $*"; }
warn() { echo "${C_YEL}!${C_RESET} $*"; }
die()  { echo "${C_RED}✗${C_RESET} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "This script must be run as root (try: sudo $0)"
command -v apt-get >/dev/null 2>&1 || die "apt-get not found; this installer targets Debian / Raspberry Pi OS"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found; systemd is required"

log "sp-rtk-base installer starting"
log "Target version : ${VERSION:-latest from PyPI}"
log "Install prefix : ${INSTALL_PREFIX}"
log "Service user   : ${SERVICE_USER}"
log "Config dir     : ${CONFIG_DIR}"
log "State dir      : ${STATE_DIR}"

# ---------------------------------------------------------------------------
# Step 1 — OS dependencies
# ---------------------------------------------------------------------------
log "Installing OS dependencies (python3-venv, build tools, BlueZ, libdbus)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    libdbus-1-dev \
    pkg-config \
    bluez \
    curl \
    ca-certificates \
    >/dev/null
ok "OS dependencies installed"

# ---------------------------------------------------------------------------
# Step 2 — Service user + groups
# ---------------------------------------------------------------------------
if id "$SERVICE_USER" >/dev/null 2>&1; then
    ok "Service user '${SERVICE_USER}' already exists"
else
    log "Creating system user '${SERVICE_USER}'…"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "Created system user '${SERVICE_USER}'"
fi

# Make sure the service user can talk to serial + Bluetooth.
# - dialout  : legacy /dev/ttyUSB*, /dev/ttyACM* on most distros
# - bluetooth: BlueZ D-Bus access for the BT input source
# - plugdev  : Raspberry Pi OS Bookworm + recent udev rules assign FTDI /
#              CP210x / CH340 USB-serial adapters to plugdev rather than
#              dialout, so a service that's only in dialout still gets EACCES
#              on /dev/ttyUSB0.  Belt-and-braces: be in both.
for grp in dialout bluetooth plugdev; do
    if getent group "$grp" >/dev/null 2>&1; then
        usermod -aG "$grp" "$SERVICE_USER"
    else
        warn "Group '${grp}' not found; skipping (Bluetooth / serial access may need manual setup)"
    fi
done
ok "Service user added to dialout + bluetooth + plugdev groups"

# ---------------------------------------------------------------------------
# Step 3 — Filesystem layout
# ---------------------------------------------------------------------------
log "Creating directories…"
install -d -m 0755 -o root            -g root            "$INSTALL_PREFIX"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
# Heal pre-existing installs whose CONFIG_DIR was created root:sp-rtk-base
# (the original v0.2.x installer) — the service user needs ownership so
# atomic-rename saves and write_text() on config.yaml both succeed.
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"
chmod 0750 "$CONFIG_DIR"
ok "Directories created"

# ---------------------------------------------------------------------------
# Step 4 — Python venv
# ---------------------------------------------------------------------------
if [[ -x "${VENV_DIR}/bin/python" ]]; then
    ok "Venv already present at ${VENV_DIR}"
else
    log "Creating Python venv at ${VENV_DIR}…"
    python3 -m venv "$VENV_DIR"
    ok "Created venv"
fi

log "Upgrading pip / setuptools / wheel inside the venv…"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip setuptools wheel

# ---------------------------------------------------------------------------
# Step 5 — Install (or upgrade) sp-rtk-base from PyPI
# ---------------------------------------------------------------------------
if [[ -n "$VERSION" ]]; then
    pin="${APP_NAME}==${VERSION}"
else
    pin="${APP_NAME}"
fi

log "Installing ${pin} from PyPI…"
"${VENV_DIR}/bin/pip" install --quiet --upgrade "$pin"
installed_version="$("${VENV_DIR}/bin/python" -c 'import sp_rtk_base; print(sp_rtk_base.__version__)')"
ok "Installed sp-rtk-base ${installed_version}"

# Make sure the whole tree is readable by the service user.
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_PREFIX"

# ---------------------------------------------------------------------------
# Step 6 — Symlink console scripts into /usr/local/bin
# ---------------------------------------------------------------------------
log "Linking console scripts into ${BIN_DIR}…"
for cmd in sp-rtk-base sp-rtk-base-gps-audit sp-rtk-base-net-provision; do
    src="${VENV_DIR}/bin/${cmd}"
    dst="${BIN_DIR}/${cmd}"
    if [[ -x "$src" ]]; then
        ln -sfn "$src" "$dst"
        ok "  ${dst} → ${src}"
    else
        warn "  ${src} not present (skipped)"
    fi
done

# ---------------------------------------------------------------------------
# Step 7 — Default config file (only if missing)
# ---------------------------------------------------------------------------
default_cfg="${CONFIG_DIR}/config.yaml"
if [[ ! -e "$default_cfg" ]]; then
    log "Writing default config to ${default_cfg}…"
    # NOTE: keep this heredoc in sync with the AppConfig pydantic model in
    #       src/sp_rtk_base/models/config_models.py.  The unit test
    #       tests/unit/test_install_default_config.py extracts this block
    #       and validates it against the model so the two cannot drift.
    cat >"$default_cfg" <<'YAML'
# sp-rtk-base config file — edit through the web UI at http://<host>:8080
# or by hand here; the service must be restarted after manual edits:
#   sudo systemctl restart sp-rtk-base

settings:
    metrics_enabled: true

destinations: []
base_positions: []
YAML
    ok "Wrote default config"
else
    ok "Config already present at ${default_cfg} (contents left untouched)"
fi

# Always (re)apply ownership + mode so a re-run of the installer heals
# pre-existing installs whose config was created root-owned and ended up
# read-only for the service user — that's the EACCES "[Errno 13]
# Permission denied: '/etc/sp-rtk-base/config.yaml'" failure mode when
# the web UI tries to save Bluetooth / input settings.  The service runs
# as ${SERVICE_USER}, so the file must be writable by that user.
chown "${SERVICE_USER}:${SERVICE_USER}" "$default_cfg"
chmod 0640 "$default_cfg"
ok "Config ownership normalised to ${SERVICE_USER}:${SERVICE_USER} (mode 0640)"

# ---------------------------------------------------------------------------
# Step 7.5 — Validate config can be loaded by the package
# ---------------------------------------------------------------------------
# Catches future schema drift between this installer and the AppConfig model
# *before* systemd tries to start the service.
log "Validating ${default_cfg} loads cleanly into AppConfig…"
if sudo -u "$SERVICE_USER" \
        SP_RTK_BASE_CONFIG="$default_cfg" \
        "${VENV_DIR}/bin/python" - <<'PY' 2>&1
from sp_rtk_base.services.config_service import ConfigService
ConfigService().load_config()
PY
then
    ok "Config validated"
else
    die "Config at ${default_cfg} failed to validate (see traceback above).
This usually means the installer's default config has drifted out of sync
with the AppConfig pydantic model.  Please file an issue:
  https://github.com/rodenj1/sp-rtk-base/issues/new"
fi

# ---------------------------------------------------------------------------
# Step 7.6 — Enable Bluetooth (best-effort)
# ---------------------------------------------------------------------------
# Raspberry Pi OS Bookworm ships with Bluetooth rfkill-soft-blocked by default.
# systemd-rfkill restores that "blocked" state on every boot, so even after
# `rfkill unblock bluetooth` the device flips back on the next reboot.
#
# We do two things to neutralise that, both safe and idempotent:
#
#   1. `rfkill unblock bluetooth` — clears the live soft-block.  The next
#      clean shutdown will let systemd-rfkill save the unblocked state to
#      /var/lib/systemd/rfkill/, so subsequent boots come up unblocked.
#
#   2. Set `BluetoothEnabled=true` in NetworkManager.state.  Newer
#      NetworkManager (1.42+) manages a per-radio enabled flag and will
#      push a fresh rfkill block on startup if this is unset / false.
#      Older NM ignores the line entirely — it's a no-op there.
#
# On non-Pi / non-NM hosts the file simply won't exist; the block is skipped.
log "Unblocking Bluetooth rfkill + nudging NetworkManager to leave it on…"

if command -v rfkill >/dev/null 2>&1; then
    rfkill unblock bluetooth 2>/dev/null || true
fi

nm_state="/var/lib/NetworkManager/NetworkManager.state"
if [[ -f "$nm_state" ]]; then
    if grep -q '^BluetoothEnabled=' "$nm_state"; then
        sed -i 's/^BluetoothEnabled=.*/BluetoothEnabled=true/' "$nm_state"
    else
        echo 'BluetoothEnabled=true' >>"$nm_state"
    fi
    # Reload NM so the change is picked up immediately (best-effort).
    systemctl reload-or-restart NetworkManager 2>/dev/null || true
fi
ok "Bluetooth rfkill cleared (idempotent)"

# ---------------------------------------------------------------------------
# Step 8 — systemd unit
# ---------------------------------------------------------------------------
unit_src=""
if [[ -f "$(dirname "$0")/sp-rtk-base.service" ]]; then
    unit_src="$(dirname "$0")/sp-rtk-base.service"
    log "Installing systemd unit from ${unit_src}…"
    install -m 0644 -o root -g root "$unit_src" "$SYSTEMD_UNIT"
else
    log "Downloading systemd unit from GitHub…"
    curl -fsSL "${REPO_RAW_BASE}/deploy/sp-rtk-base.service" -o "$SYSTEMD_UNIT"
    chmod 0644 "$SYSTEMD_UNIT"
fi
ok "systemd unit installed at ${SYSTEMD_UNIT}"

log "Reloading systemd and enabling sp-rtk-base.service…"
systemctl daemon-reload
systemctl enable sp-rtk-base.service >/dev/null
systemctl restart sp-rtk-base.service
ok "Service enabled and (re)started"

# ---------------------------------------------------------------------------
# Step 8.2 — Ensure NetworkManager is present and managing the network (issue #11)
# ---------------------------------------------------------------------------
# Raspberry Pi OS Bookworm ships NetworkManager by default, but the
# one-line curl|bash installer targets "any Debian-based host" — ensure
# it's here rather than assuming it. Non-NetworkManager network stacks
# (dhcpcd, iwd, connman, …) are out of scope (issue #6); we don't try to
# migrate an existing one away, just make sure NM itself is ready.
if command -v nmcli >/dev/null 2>&1; then
    ok "NetworkManager already installed"
else
    log "Installing NetworkManager…"
    apt-get install -y --no-install-recommends network-manager >/dev/null
    ok "Installed NetworkManager"
fi
systemctl enable --now NetworkManager.service >/dev/null 2>&1 || true
ok "NetworkManager enabled"

# A dhcpcd/udev override that marks wlan0 unmanaged is the classic reason
# NetworkManager is running but the setup AP never comes up. We don't
# force a fix here (stopping a live network service mid curl|bash is
# exactly the kind of surprise a headless install shouldn't spring), just
# surface it loudly so it isn't a silent dead end.
wlan0_state="$(nmcli -t -f DEVICE,STATE device status 2>/dev/null | awk -F: '$1=="wlan0"{print $2}')"
if [[ "$wlan0_state" == "unmanaged" ]]; then
    warn "wlan0 is unmanaged by NetworkManager (commonly caused by dhcpcd or a
udev/NM config override) — the setup AP cannot come up until it's managed.
See docs/deployment-pi.md for troubleshooting."
fi

# ---------------------------------------------------------------------------
# Step 8.3 — Network-provisioning config (issue #11)
# ---------------------------------------------------------------------------
# Separate file from config.yaml (services/net_provision/config_loader.py
# reads it independently — issue #9). Written only if absent, same
# only-if-absent contract as config.yaml above: re-running the installer
# must never clobber a site's tuned knobs. ap_password has no default in
# the AppConfig model on purpose, so unlike config.yaml there is no safe
# default to synthesise here — it must come from $AP_PASSWORD (issue #6,
# story 8: one fixed SSID/password sticker for the whole fleet).
net_provision_cfg="${CONFIG_DIR}/net_provision.yaml"
if [[ ! -e "$net_provision_cfg" ]]; then
    if [[ -z "$AP_PASSWORD" ]]; then
        die "AP_PASSWORD is not set and ${net_provision_cfg} does not exist yet.
Set AP_PASSWORD to the fixed setup-AP passphrase for this fleet (WPA2: 8-63
chars), e.g.:
  sudo AP_PASSWORD='your-sticker-password' ./deploy/install.sh
Optionally override the SSID too with AP_SSID (default: ${AP_SSID}).
If you already have a device provisioned, copy its ${net_provision_cfg}
onto this one instead of generating a new password."
    fi
    log "Writing network-provisioning config to ${net_provision_cfg}…"
    # A WPA2 passphrase may legally contain " or \, either of which would
    # break out of the YAML double-quoted scalar below (or worse, get
    # interpreted as a second key) if interpolated raw. Escape both before
    # they go anywhere near the heredoc.
    yaml_dq_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
    ap_ssid_yaml="$(yaml_dq_escape "$AP_SSID")"
    ap_password_yaml="$(yaml_dq_escape "$AP_PASSWORD")"
    cat >"$net_provision_cfg" <<YAML
# sp-rtk-base network-provisioning config — headless field onboarding
# (issue #6). ap_ssid / ap_password are the fixed setup-AP credentials
# printed on this fleet's sticker. Restart the provisioning service
# after manual edits:
#   sudo systemctl restart sp-rtk-base-net-provision

ap_ssid: "${ap_ssid_yaml}"
ap_password: "${ap_password_yaml}"
YAML
    ok "Wrote network-provisioning config (setup AP: ${AP_SSID})"
else
    ok "Network-provisioning config already present at ${net_provision_cfg} (contents left untouched)"
fi

# Always (re)apply ownership + mode, same reasoning as config.yaml above:
# a re-run must heal a pre-existing file created by hand (e.g. per the
# docs' manual-creation fallback) that ended up root-owned, or the
# sudo -u "$SERVICE_USER" read just below fails with EACCES.
chown "${SERVICE_USER}:${SERVICE_USER}" "$net_provision_cfg"
chmod 0640 "$net_provision_cfg"

log "Validating ${net_provision_cfg} loads cleanly into NetProvisionConfig…"
net_provision_creds="$(sudo -u "$SERVICE_USER" \
        SP_RTK_BASE_NET_CONFIG="$net_provision_cfg" \
        "${VENV_DIR}/bin/python" - <<'PY'
from sp_rtk_base.services.net_provision.config_loader import load_net_provision_config
cfg = load_net_provision_config()
print(cfg.ap_ssid)
print(cfg.ap_password)
PY
)" || die "Config at ${net_provision_cfg} failed to validate (see traceback above)."
provisioned_ap_ssid="$(sed -n '1p' <<<"$net_provision_creds")"
provisioned_ap_password="$(sed -n '2p' <<<"$net_provision_creds")"
ok "Config validated (setup AP: ${provisioned_ap_ssid})"

# ---------------------------------------------------------------------------
# Step 8.4 — Setup-AP NetworkManager connection profile (issue #11)
# ---------------------------------------------------------------------------
# NmcliAdapter._ap_up()/_ap_down() (issue #8) call `nmcli connection up/down
# id <ap_ssid>` — they never create the profile, only activate one that
# already exists. This is that profile. Idempotent: only created if
# missing, and always built from whatever ap_ssid/ap_password is *actually*
# in net_provision.yaml (not $AP_SSID/$AP_PASSWORD directly), so a re-run
# after a hand-edited config or a restored backup stays in sync.
if nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$provisioned_ap_ssid"; then
    ok "Setup-AP connection profile '${provisioned_ap_ssid}' already present"
else
    log "Creating setup-AP connection profile '${provisioned_ap_ssid}'…"
    # `mode ap` is a wifi type-specific keyword nmcli recognizes directly;
    # everything else is a raw <setting>.<property> override, documented
    # as requiring a literal `--` separator before the pairs (`nmcli
    # connection add help`) — kept explicit here for portability across
    # nmcli versions even though 1.36+ also accepts them without it.
    nmcli connection add \
        type wifi \
        ifname wlan0 \
        con-name "$provisioned_ap_ssid" \
        autoconnect no \
        ssid "$provisioned_ap_ssid" \
        mode ap \
        -- \
        802-11-wireless.band bg \
        ipv4.method shared \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$provisioned_ap_password" \
        >/dev/null
    ok "Setup-AP connection profile installed"
fi

# ---------------------------------------------------------------------------
# Step 8.5 — Polkit rule for headless network provisioning (issue #9)
# ---------------------------------------------------------------------------
# NetworkManager's default policy only grants connection up/down without
# interactive auth to a user with an *active local session*.  A systemd
# service account has no session, so every `nmcli connection up/down` call
# from sp-rtk-base-net-provision.service would otherwise be silently
# refused.  This rule grants the service user unconditional control.
if [[ -d /etc/polkit-1 ]]; then
    polkit_rule_src="$(dirname "$0")/polkit/10-sp-rtk-base-net-provision.rules"
    log "Installing polkit rule for ${SERVICE_USER} → NetworkManager control…"
    install -d -m 0755 -o root -g root "$POLKIT_RULES_DIR"
    if [[ -f "$polkit_rule_src" ]]; then
        install -m 0644 -o root -g root "$polkit_rule_src" "$POLKIT_RULE_DEST"
    else
        curl -fsSL "${REPO_RAW_BASE}/deploy/polkit/10-sp-rtk-base-net-provision.rules" \
            -o "$POLKIT_RULE_DEST"
        chmod 0644 "$POLKIT_RULE_DEST"
    fi
    # polkit picks up rules.d changes automatically, but restart it
    # (best-effort — package name varies by distro) so the new rule is
    # live before the net-provision service starts below.
    systemctl try-restart polkit.service 2>/dev/null \
        || systemctl try-restart polkitd.service 2>/dev/null \
        || true
    ok "Polkit rule installed at ${POLKIT_RULE_DEST}"
else
    warn "polkit not found at /etc/polkit-1; skipping polkit rule (nmcli calls from
sp-rtk-base-net-provision.service may need manual polkit configuration)"
fi

# ---------------------------------------------------------------------------
# Step 8.6 — Network-provisioning systemd unit (issue #9)
# ---------------------------------------------------------------------------
# Deliberately independent of sp-rtk-base.service (issue #6, story 17): no
# dependency between the two units either direction. net_provision.yaml
# and its AP connection profile were written above (issue #11), so this
# should come up clean on a fresh install rather than fail-and-restart.
net_provision_unit_src=""
if [[ -f "$(dirname "$0")/sp-rtk-base-net-provision.service" ]]; then
    net_provision_unit_src="$(dirname "$0")/sp-rtk-base-net-provision.service"
    log "Installing systemd unit from ${net_provision_unit_src}…"
    install -m 0644 -o root -g root "$net_provision_unit_src" "$NET_PROVISION_SYSTEMD_UNIT"
else
    log "Downloading network-provisioning systemd unit from GitHub…"
    curl -fsSL "${REPO_RAW_BASE}/deploy/sp-rtk-base-net-provision.service" \
        -o "$NET_PROVISION_SYSTEMD_UNIT"
    chmod 0644 "$NET_PROVISION_SYSTEMD_UNIT"
fi
ok "systemd unit installed at ${NET_PROVISION_SYSTEMD_UNIT}"

log "Reloading systemd and enabling sp-rtk-base-net-provision.service…"
systemctl daemon-reload
systemctl enable sp-rtk-base-net-provision.service >/dev/null
if systemctl restart sp-rtk-base-net-provision.service 2>/dev/null \
        && sleep 2 && systemctl is-active --quiet sp-rtk-base-net-provision.service; then
    ok "Network-provisioning service enabled and (re)started"
else
    warn "sp-rtk-base-net-provision.service is enabled but not running.
Check logs with: sudo journalctl -u sp-rtk-base-net-provision --no-pager -n 50"
fi

# ---------------------------------------------------------------------------
# Step 9 — Final status
# ---------------------------------------------------------------------------
sleep 2
if systemctl is-active --quiet sp-rtk-base.service; then
    listen_addr="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [[ -z "$listen_addr" ]] && listen_addr="localhost"
    echo
    echo "${C_GREEN}╔══════════════════════════════════════════════════════════╗${C_RESET}"
    echo "${C_GREEN}║ sp-rtk-base ${installed_version} is running                                ║${C_RESET}"
    echo "${C_GREEN}╚══════════════════════════════════════════════════════════╝${C_RESET}"
    echo
    echo "  Web UI:   http://${listen_addr}:8080"
    echo "  Config:   ${default_cfg}"
    echo "  State:    ${STATE_DIR}/"
    echo "  Logs:     sudo journalctl -u sp-rtk-base -f"
    echo "  Status:   systemctl status sp-rtk-base"
    echo "  Stop:     sudo systemctl stop sp-rtk-base"
    echo "  Upgrade:  sudo ${INSTALL_PREFIX}/venv/bin/pip install -U sp-rtk-base && \\"
    echo "            sudo systemctl restart sp-rtk-base"
    echo
    echo "  Network-provisioning service (issue #9/#11, independent unit):"
    echo "    Setup AP: ${provisioned_ap_ssid} (password in ${net_provision_cfg})"
    echo "    Config:   ${net_provision_cfg}"
    echo "    Logs:     sudo journalctl -u sp-rtk-base-net-provision -f"
    echo "    Status:   systemctl status sp-rtk-base-net-provision"
    echo
else
    warn "Service failed to start.  Check logs with:"
    warn "  sudo journalctl -u sp-rtk-base --no-pager -n 50"
    systemctl status sp-rtk-base --no-pager || true
    exit 1
fi
