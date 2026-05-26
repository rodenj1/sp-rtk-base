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
for grp in dialout bluetooth; do
    if getent group "$grp" >/dev/null 2>&1; then
        usermod -aG "$grp" "$SERVICE_USER"
    else
        warn "Group '${grp}' not found; skipping (Bluetooth / serial access may need manual setup)"
    fi
done
ok "Service user added to dialout + bluetooth groups"

# ---------------------------------------------------------------------------
# Step 3 — Filesystem layout
# ---------------------------------------------------------------------------
log "Creating directories…"
install -d -m 0755 -o root          -g root          "$INSTALL_PREFIX"
install -d -m 0750 -o root          -g "$SERVICE_USER" "$CONFIG_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
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
    cat >"$default_cfg" <<'YAML'
# sp-rtk-base config file — edit through the web UI at http://<host>:8080
# or by hand here; the service must be restarted after manual edits:
#   sudo systemctl restart sp-rtk-base

settings:
    metrics_enabled: true

input:
    source_type: tcp
    tcp_host: 127.0.0.1
    tcp_port: 19800

destinations: []
base_positions: []
YAML
    chown "root:${SERVICE_USER}" "$default_cfg"
    chmod 0640 "$default_cfg"
    ok "Wrote default config (group-readable by ${SERVICE_USER})"
else
    ok "Config already present at ${default_cfg} (left untouched)"
fi

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
