#!/usr/bin/env bash
# ============================================================================
# sp-rtk-base — uninstall
# ============================================================================
#
# Stops + disables the systemd service, removes the venv and console-script
# symlinks, and (interactively) offers to remove the service user, config
# directory, and state directory.
#
# Usage:
#   sudo ./deploy/uninstall.sh                 # interactive
#   sudo ./deploy/uninstall.sh --purge         # remove everything, no prompts
#   sudo ./deploy/uninstall.sh --keep-data     # keep config + state (default
#                                              # if --purge is not passed)
# ============================================================================

set -euo pipefail

SERVICE_USER="${SERVICE_USER:-sp-rtk-base}"
INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/sp-rtk-base}"
CONFIG_DIR="${CONFIG_DIR:-/etc/sp-rtk-base}"
STATE_DIR="${STATE_DIR:-/var/lib/sp-rtk-base}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
SYSTEMD_UNIT="${SYSTEMD_UNIT:-/etc/systemd/system/sp-rtk-base.service}"
NET_PROVISION_SYSTEMD_UNIT="${NET_PROVISION_SYSTEMD_UNIT:-/etc/systemd/system/sp-rtk-base-net-provision.service}"
POLKIT_RULE_DEST="${POLKIT_RULE_DEST:-/etc/polkit-1/rules.d/10-sp-rtk-base-net-provision.rules}"

PURGE=false
KEEP_DATA=false
case "${1:-}" in
    --purge)     PURGE=true ;;
    --keep-data) KEEP_DATA=true ;;
    "" )         ;;  # interactive
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
esac

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0" >&2; exit 1; }

# Capture the setup-AP SSID before anything below removes net_provision.yaml
# or the venv used to parse it (issue #11). Falls back to the model's
# DEFAULT_AP_SSID, matching install.sh, if the file is missing or was
# never customised — a best-effort delete against that name is harmless
# if no such connection profile exists. Parsed with the venv's own PyYAML
# (still intact at this point — the app tree isn't removed until later)
# rather than a shell regex, so a quoted SSID containing spaces or YAML
# escapes round-trips the same way install.sh wrote it.
DEFAULT_AP_SSID="sp-rtk-base-setup"
AP_SSID="$DEFAULT_AP_SSID"
net_provision_cfg="${CONFIG_DIR}/net_provision.yaml"
venv_python="${INSTALL_PREFIX}/venv/bin/python"
if [[ -f "$net_provision_cfg" && -x "$venv_python" ]]; then
    parsed_ssid="$("$venv_python" - "$net_provision_cfg" <<'PY' 2>/dev/null || true
import sys
import yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
ssid = data.get("ap_ssid") if isinstance(data, dict) else None
if isinstance(ssid, str) and ssid:
    print(ssid)
PY
)"
    [[ -n "$parsed_ssid" ]] && AP_SSID="$parsed_ssid"
fi

ask() {
    local prompt="$1"
    if $PURGE;     then return 0; fi
    if $KEEP_DATA; then return 1; fi
    read -r -p "${prompt} [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]]
}

echo "==> Stopping + disabling sp-rtk-base.service…"
systemctl stop    sp-rtk-base.service 2>/dev/null || true
systemctl disable sp-rtk-base.service 2>/dev/null || true

echo "==> Stopping + disabling sp-rtk-base-net-provision.service…"
systemctl stop    sp-rtk-base-net-provision.service 2>/dev/null || true
systemctl disable sp-rtk-base-net-provision.service 2>/dev/null || true

if [[ -f "$SYSTEMD_UNIT" ]]; then
    echo "==> Removing systemd unit ${SYSTEMD_UNIT}"
    rm -f "$SYSTEMD_UNIT"
    systemctl daemon-reload
fi

if [[ -f "$NET_PROVISION_SYSTEMD_UNIT" ]]; then
    echo "==> Removing systemd unit ${NET_PROVISION_SYSTEMD_UNIT}"
    rm -f "$NET_PROVISION_SYSTEMD_UNIT"
    systemctl daemon-reload
fi

if command -v nmcli >/dev/null 2>&1 && nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$AP_SSID"; then
    echo "==> Removing setup-AP connection profile '${AP_SSID}'"
    nmcli connection delete id "$AP_SSID" 2>/dev/null || true
fi

if [[ -f "$POLKIT_RULE_DEST" ]]; then
    echo "==> Removing polkit rule ${POLKIT_RULE_DEST}"
    rm -f "$POLKIT_RULE_DEST"
    systemctl try-restart polkit.service 2>/dev/null \
        || systemctl try-restart polkitd.service 2>/dev/null \
        || true
fi

echo "==> Removing console-script symlinks from ${BIN_DIR}"
rm -f "${BIN_DIR}/sp-rtk-base" "${BIN_DIR}/sp-rtk-base-gps-audit" "${BIN_DIR}/sp-rtk-base-net-provision"

if [[ -d "$INSTALL_PREFIX" ]]; then
    echo "==> Removing app tree ${INSTALL_PREFIX}"
    rm -rf "$INSTALL_PREFIX"
fi

if [[ -d "$CONFIG_DIR" ]]; then
    if ask "Remove config directory ${CONFIG_DIR}?"; then
        rm -rf "$CONFIG_DIR"
        echo "  ✓ Removed ${CONFIG_DIR}"
    else
        echo "  ✓ Kept ${CONFIG_DIR}"
    fi
fi

if [[ -d "$STATE_DIR" ]]; then
    if ask "Remove state directory ${STATE_DIR}?"; then
        rm -rf "$STATE_DIR"
        echo "  ✓ Removed ${STATE_DIR}"
    else
        echo "  ✓ Kept ${STATE_DIR}"
    fi
fi

if id "$SERVICE_USER" >/dev/null 2>&1; then
    if ask "Remove system user '${SERVICE_USER}'?"; then
        userdel "$SERVICE_USER" 2>/dev/null || true
        echo "  ✓ Removed user ${SERVICE_USER}"
    else
        echo "  ✓ Kept user ${SERVICE_USER}"
    fi
fi

echo
echo "✓ sp-rtk-base uninstalled."
