#!/usr/bin/env bash
# ============================================================================
# sp-rtk-base — shared appliance network-artifact teardown
# ============================================================================
#
# Sourced (never executed directly) by both deploy/uninstall.sh and
# deploy/install.sh (mode-switch appliance -> managed-host, issue #27/#30)
# so the two copies of "remove the appliance network takeover" can't drift
# out of sync the way they would as hand-duplicated inline blocks.
#
# Callers must have these set before calling the function below:
#   NET_PROVISION_SYSTEMD_UNIT   path to the net-provision systemd unit file
#   POLKIT_RULE_DEST             path to the installed polkit rule
#   DNSMASQ_WILDCARD_CONF        path to the dnsmasq-shared.d wildcard drop-in (issue #34)
# ============================================================================

# teardown_appliance_network_artifacts <ap_ssid>
#
# Stops/disables sp-rtk-base-net-provision.service, removes its unit file,
# deletes the setup-AP nmcli connection profile named <ap_ssid>, removes the
# polkit rule, and removes the dnsmasq-shared.d wildcard DNS drop-in. Every
# step is best-effort and idempotent — safe to call against a host that
# never had any of this installed (a plain managed-host install, or a
# second teardown in a row).
teardown_appliance_network_artifacts() {
    local ap_ssid="$1"

    echo "==> Stopping + disabling sp-rtk-base-net-provision.service…"
    systemctl stop    sp-rtk-base-net-provision.service 2>/dev/null || true
    systemctl disable sp-rtk-base-net-provision.service 2>/dev/null || true

    if [[ -f "$NET_PROVISION_SYSTEMD_UNIT" ]]; then
        echo "==> Removing systemd unit ${NET_PROVISION_SYSTEMD_UNIT}"
        rm -f "$NET_PROVISION_SYSTEMD_UNIT"
        systemctl daemon-reload
    fi

    if command -v nmcli >/dev/null 2>&1 && nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$ap_ssid"; then
        echo "==> Removing setup-AP connection profile '${ap_ssid}'"
        nmcli connection delete id "$ap_ssid" 2>/dev/null || true
    fi

    if [[ -f "$POLKIT_RULE_DEST" ]]; then
        echo "==> Removing polkit rule ${POLKIT_RULE_DEST}"
        rm -f "$POLKIT_RULE_DEST"
        systemctl try-restart polkit.service 2>/dev/null \
            || systemctl try-restart polkitd.service 2>/dev/null \
            || true
    fi

    if [[ -f "$DNSMASQ_WILDCARD_CONF" ]]; then
        echo "==> Removing wildcard DNS drop-in ${DNSMASQ_WILDCARD_CONF}"
        rm -f "$DNSMASQ_WILDCARD_CONF"
    fi
}
