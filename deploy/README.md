# `deploy/` — Production deployment artifacts

These files install `sp-rtk-base` from PyPI onto a Raspberry Pi (or any
Debian-based host) as a systemd-managed service running under a dedicated
`sp-rtk-base` system user.

| File | Purpose |
|---|---|
| [`install.sh`](install.sh) | One-shot installer (system user, dirs, venv, pip, systemd) |
| [`upgrade.sh`](upgrade.sh) | Upgrade the installed version + restart service |
| [`uninstall.sh`](uninstall.sh) | Remove service + (optionally) config + state |
| [`sp-rtk-base.service`](sp-rtk-base.service) | Hardened systemd unit for the web UI + relay |
| [`sp-rtk-base-net-provision.service`](sp-rtk-base-net-provision.service) | Independent systemd unit for the headless network-provisioning supervisor (issue #9) |
| [`polkit/10-sp-rtk-base-net-provision.rules`](polkit/10-sp-rtk-base-net-provision.rules) | Grants the service account NetworkManager control (no session to authenticate against otherwise) |

See **[`docs/deployment-pi.md`](../docs/deployment-pi.md)** for the full
runbook covering layout, day-2 operations, backup/restore, nginx
reverse proxy, troubleshooting, and fleet management.

## Quick install on a fresh Pi

`AP_PASSWORD` is required the first time (issue #6, story 8: one fixed
setup-AP SSID/password sticker for the whole fleet — it's never baked
into source, so it has to come from you):

```bash
curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/deploy/install.sh \
    | sudo AP_PASSWORD='your-sticker-password' bash
```

`AP_SSID` optionally overrides the setup-AP name (default:
`sp-rtk-base-setup`). Neither is needed on a re-run once
`net_provision.yaml` already exists — it's written only if absent.

## Layout summary

```
/opt/sp-rtk-base/venv/                          isolated Python venv
/usr/local/bin/sp-rtk-base                      operator CLI (symlink)
/usr/local/bin/sp-rtk-base-gps-audit            u-blox audit CLI (symlink)
/usr/local/bin/sp-rtk-base-net-provision        network-provisioning CLI (symlink)
/etc/sp-rtk-base/config.yaml                    operator configuration
/etc/sp-rtk-base/net_provision.yaml             network-provisioning config (written from $AP_SSID/$AP_PASSWORD, only if absent — issue #11)
/var/lib/sp-rtk-base/                           runtime state (incl. durable provisioning clocks)
/etc/systemd/system/sp-rtk-base.service         systemd unit — web UI + relay
/etc/systemd/system/sp-rtk-base-net-provision.service   systemd unit — network provisioning (independent, issue #9)
/etc/polkit-1/rules.d/10-sp-rtk-base-net-provision.rules  NetworkManager control for the service account
(NetworkManager connection profile named after ap_ssid)   setup-AP profile (issue #11) — lives in NetworkManager's own store, not a plain file
```

Both services run as the dedicated `sp-rtk-base` system user (member of
`dialout` + `bluetooth` groups). The two units have no dependency on each
other by design: the network-provisioning loop keeps self-healing
Ethernet/WiFi/AP state even while the web app is down, and vice versa.
`install.sh` also ensures NetworkManager is installed and enabled, and
creates the setup-AP connection profile `NmcliAdapter` activates via
`nmcli connection up/down` — both idempotently, only if missing.
