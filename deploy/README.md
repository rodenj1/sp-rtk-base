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

```bash
curl -fsSL https://raw.githubusercontent.com/rodenj1/sp-rtk-base/main/deploy/install.sh \
    | sudo bash
```

## Layout summary

```
/opt/sp-rtk-base/venv/                          isolated Python venv
/usr/local/bin/sp-rtk-base                      operator CLI (symlink)
/usr/local/bin/sp-rtk-base-gps-audit            u-blox audit CLI (symlink)
/usr/local/bin/sp-rtk-base-net-provision        network-provisioning CLI (symlink)
/etc/sp-rtk-base/config.yaml                    operator configuration
/etc/sp-rtk-base/net_provision.yaml             network-provisioning config (not written by install.sh — see issue #11)
/var/lib/sp-rtk-base/                           runtime state (incl. durable provisioning clocks)
/etc/systemd/system/sp-rtk-base.service         systemd unit — web UI + relay
/etc/systemd/system/sp-rtk-base-net-provision.service   systemd unit — network provisioning (independent, issue #9)
/etc/polkit-1/rules.d/10-sp-rtk-base-net-provision.rules  NetworkManager control for the service account
```

Both services run as the dedicated `sp-rtk-base` system user (member of
`dialout` + `bluetooth` groups). The two units have no dependency on each
other by design: the network-provisioning loop keeps self-healing
Ethernet/WiFi/AP state even while the web app is down, and vice versa.
`sp-rtk-base-net-provision.service` will fail loudly and restart on a
loop until `net_provision.yaml` exists — that file is deliberately not
synthesised with defaults, since its AP password has no safe default.
