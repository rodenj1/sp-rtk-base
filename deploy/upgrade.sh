#!/usr/bin/env bash
# ============================================================================
# sp-rtk-base — in-place upgrade
# ============================================================================
#
# Upgrades the venv at /opt/sp-rtk-base/venv/ to the latest sp-rtk-base
# (or a pinned version) from PyPI, then restarts the systemd service.
#
# Usage:
#   sudo ./deploy/upgrade.sh                  # upgrade to latest on PyPI
#   sudo ./deploy/upgrade.sh 0.3.0            # pin to a specific version
# ============================================================================

set -euo pipefail

INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/sp-rtk-base}"
VENV_DIR="${INSTALL_PREFIX}/venv"
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
echo "==> Upgrading to: ${target}"

"${VENV_DIR}/bin/pip" install --quiet --upgrade "$target"
new_ver="$("${VENV_DIR}/bin/python" -c 'import sp_rtk_base; print(sp_rtk_base.__version__)')"

echo "==> Restarting sp-rtk-base.service…"
systemctl restart sp-rtk-base.service

sleep 2
if systemctl is-active --quiet sp-rtk-base.service; then
    echo "✓ Upgrade complete: sp-rtk-base ${old_ver} → ${new_ver}"
    systemctl status sp-rtk-base --no-pager --lines=0
else
    echo "✗ Service failed to start after upgrade.  Check logs:" >&2
    echo "    sudo journalctl -u sp-rtk-base --no-pager -n 50" >&2
    exit 1
fi
