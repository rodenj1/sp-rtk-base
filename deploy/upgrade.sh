#!/usr/bin/env bash
# ============================================================================
# sp-rtk-base — in-place upgrade
# ============================================================================
#
# Upgrades the venv at /opt/sp-rtk-base/venv/ to the latest sp-rtk-base
# (or a pinned version) from PyPI, then restarts the systemd service.
# The Relay moves with it, to the newest version that app allows, and the
# venv is handed back to the service user so the web UI's Update (which
# runs pip as that user) can work on it afterwards.
#
# Pip-only: this never touches the systemd units or other host files. To
# change those, re-run deploy/install.sh.
#
# Usage:
#   sudo ./deploy/upgrade.sh                  # upgrade to latest on PyPI
#   sudo ./deploy/upgrade.sh 0.3.0            # pin to a specific version
# ============================================================================

set -euo pipefail

INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/sp-rtk-base}"
VENV_DIR="${INSTALL_PREFIX}/venv"
SERVICE_USER="${SERVICE_USER:-sp-rtk-base}"
RELAY_NAME="sp-rtk-base-relay"
VERSION="${1:-}"

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0" >&2; exit 1; }
[[ -x "${VENV_DIR}/bin/pip" ]] || {
    echo "Venv not found at ${VENV_DIR}." >&2
    echo "Did you run deploy/install.sh first?" >&2
    exit 1
}

old_ver="$("${VENV_DIR}/bin/python" -c 'import sp_rtk_base; print(sp_rtk_base.__version__)' 2>/dev/null || echo 'unknown')"

if [[ -n "$VERSION" ]]; then
    target="sp-rtk-base==${VERSION}"
else
    target="sp-rtk-base"
fi

echo "==> Currently installed: sp-rtk-base ${old_ver}"
echo "==> Upgrading to: ${target} (with the newest ${RELAY_NAME} it allows)"

# Pip runs as root here, so whatever it writes is root-owned. Hand the
# whole prefix back to the service user afterwards, even if pip fails
# partway, or the next Update from the web UI fails on root-owned files.
give_back_to_service_user() {
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_PREFIX"
}
trap give_back_to_service_user EXIT
# The Relay is named without a version, so pip moves it to the newest one
# the target app's own requirement allows. Otherwise an older Relay that
# still satisfies that requirement stays put.
"${VENV_DIR}/bin/pip" install --quiet --upgrade "$target" "$RELAY_NAME"
give_back_to_service_user
trap - EXIT
new_ver="$("${VENV_DIR}/bin/python" -c 'import sp_rtk_base; print(sp_rtk_base.__version__)')"

echo "==> Restarting sp-rtk-base.service…"
systemctl restart sp-rtk-base.service

# sp-rtk-base-net-provision.service (issue #9) is a separate, independent
# unit — only touch it if a prior install actually set it up.
if systemctl list-unit-files sp-rtk-base-net-provision.service >/dev/null 2>&1; then
    echo "==> Restarting sp-rtk-base-net-provision.service…"
    systemctl restart sp-rtk-base-net-provision.service 2>/dev/null || true
fi

sleep 2
if systemctl is-active --quiet sp-rtk-base.service; then
    echo "✓ Upgrade complete: sp-rtk-base ${old_ver} → ${new_ver}"
    systemctl status sp-rtk-base --no-pager --lines=0
else
    echo "✗ Service failed to start after upgrade.  Check logs:" >&2
    echo "    sudo journalctl -u sp-rtk-base --no-pager -n 50" >&2
    exit 1
fi
