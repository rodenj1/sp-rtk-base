# `deploy/` — Production deployment artifacts

These files install `sp-rtk-base` from PyPI onto a Raspberry Pi (or any
Debian-based host) as a systemd-managed service running under a dedicated
`sp-rtk-base` system user.

| File | Purpose |
|---|---|
| [`install.sh`](install.sh) | One-shot installer (system user, dirs, venv, pip, systemd) |
| [`upgrade.sh`](upgrade.sh) | Upgrade the installed version + restart service |
| [`uninstall.sh`](uninstall.sh) | Remove service + (optionally) config + state |
| [`sp-rtk-base.service`](sp-rtk-base.service) | Hardened systemd unit |

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
/etc/sp-rtk-base/config.yaml                    operator configuration
/var/lib/sp-rtk-base/                           runtime state
/etc/systemd/system/sp-rtk-base.service         systemd unit
```

Service runs as the dedicated `sp-rtk-base` system user (member of
`dialout` + `bluetooth` groups).
