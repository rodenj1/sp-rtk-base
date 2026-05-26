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

PURGE=false
KEEP_DATA=false
case "${1:-}" in
    --purge)     PURGE=true ;;
    --keep-data) KEEP_DATA=true ;;
    "" )         ;;  # interactive
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
esac

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0" >&2; exit 1; }

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

if [[ -f "$SYSTEMD_UNIT" ]]; then
    echo "==> Removing systemd unit ${SYSTEMD_UNIT}"
    rm -f "$SYSTEMD_UNIT"
    systemctl daemon-reload
fi

echo "==> Removing console-script symlinks from ${BIN_DIR}"
rm -f "${BIN_DIR}/sp-rtk-base" "${BIN_DIR}/sp-rtk-base-gps-audit"

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
