#!/usr/bin/env bash
# ============================================================================
# sp-rtk-base — Raspberry Pi / Debian installer
# ============================================================================
#
# Installs sp-rtk-base from PyPI into an isolated venv at /opt/sp-rtk-base/
# under a dedicated `sp-rtk-base` system user, then enables a systemd
# service so the relay starts at boot.
#
# Usage:
#   sudo ./deploy/install.sh                  # install latest from PyPI
#   sudo ./deploy/install.sh 0.2.0            # pin to a specific version
#   sudo VERSION=0.2.0 ./deploy/install.sh    # same, via env var
#
# Or one-shot from a fresh Pi:
#   curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/deploy/install.sh \
#       | sudo bash
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
for cmd in sp-rtk-base sp-rtk-base-gps-audit; do
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
else
    warn "Service failed to start.  Check logs with:"
    warn "  sudo journalctl -u sp-rtk-base --no-pager -n 50"
    systemctl status sp-rtk-base --no-pager || true
    exit 1
fi
