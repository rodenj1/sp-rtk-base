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
| [`sp-rtk-base-update.service`](sp-rtk-base-update.service) | Oneshot unit that installs an Update the web UI asked for (ADR 0005) |
| [`sp-rtk-base-portal-agent.service`](sp-rtk-base-portal-agent.service) | The Portal agent: idle until the base is Claimed, answers the app on `/run/sp-rtk-base-portal-agent/control.sock`, keeps the Claim in its `StateDirectory`, denied the LAN and all IPv6 but `::1` by `IPAddressDeny`, no `AF_INET6` sockets, its own resolv.conf of public DNS resolvers (see [The Portal agent's network](#the-portal-agents-network)) |
| [`sp-rtk-base-update.path`](sp-rtk-base-update.path) | Starts the Update unit when the app writes its request file; disabled by `install.sh --no-update` |
| [`plumbing-version`](plumbing-version) | The Host setup these files make up, as an integer. `install.sh` writes it into the Update unit; raise it whenever a file `install.sh` lays down changes (CI checks) |
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
/etc/systemd/system/sp-rtk-base-update.service  systemd unit — installs an Update (oneshot, ADR 0005)
/etc/systemd/system/sp-rtk-base-update.path     starts it on /var/lib/sp-rtk-base/update/request.json
/etc/systemd/system/sp-rtk-base-portal-agent.service  systemd unit — Portal agent (idle until Claimed)
/run/sp-rtk-base-portal-agent/control.sock      the agent's control socket (service user only)
/var/lib/sp-rtk-base-portal-agent/              the Claim: base key (0600), claim.json, base_id; never touched by install.sh
/etc/sp-rtk-base-portal-agent/resolv.conf       the agent's public DNS resolvers, bind-mounted over its /etc/resolv.conf (root's; absent if public DNS is blocked)
/etc/systemd/system/sp-rtk-base-portal-agent.service.d/resolver.conf  only if public DNS is blocked: lets the agent use the host's LAN resolver (install.sh warns)
/var/lib/sp-rtk-base/update/                    Update request file and status.json
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

### The Portal agent's network

The agent's unit keeps it off the LAN whatever its code does. It may not
open an IPv6 socket at all (`RestrictAddressFamilies=AF_INET AF_UNIX`),
and `IPAddressDeny` refuses it the private and link-local IPv4 ranges and
every IPv6 address but `::1`: on a dual-stack LAN each device also has a
global IPv6 address, so only "no IPv6" keeps those out. Its clients ask
for IPv4 addresses only, so a dual-stack Portal is reached over IPv4
without waiting on an IPv6 attempt. Loopback stays open for the base's own
UI on `127.0.0.1:8080`.

DNS is the one thing it needs from the network beyond the Portal, and a
home LAN's resolver is usually the router, whose admin page shares its
address (`IPAddressAllow` can't be limited to port 53). So `install.sh`
checks whether public resolvers (1.1.1.1, 9.9.9.9, 8.8.8.8, over IPv4;
override with `PORTAL_AGENT_PUBLIC_DNS`) answer from this host:

- **They answer** (the normal case): it writes the ones that did to
  `/etc/sp-rtk-base-portal-agent/resolv.conf`, which the unit bind-mounts
  over `/etc/resolv.conf` (`BindReadOnlyPaths=`). Nothing on the LAN is
  allowed. A host whose own `/etc/resolv.conf` names systemd-resolved
  (`127.0.0.53`) or the router changes nothing: the agent reads only its
  file. The one exception is an `/etc/nsswitch.conf` with `resolve`
  (libnss-resolve) before `dns`: then glibc asks systemd-resolved over
  its Unix socket and never reads resolv.conf, but resolved does the
  LAN lookup outside the sandbox, so the agent still gets no LAN address.
- **They're blocked**: it falls back to the host's resolvers. A drop-in,
  `sp-rtk-base-portal-agent.service.d/resolver.conf`, clears the bind
  mount and allows the host's IPv4 LAN resolver, and `install.sh` warns
  that the agent can then reach that address **on any port**. A loopback
  resolver (`127.0.0.53`) needs no allowance.

Re-running `install.sh` checks again and removes whichever file is stale;
`uninstall.sh` removes both.

Public resolvers aren't always what they seem: some home networks (a
Firewalla, for one) intercept every query to port 53, even one addressed
to 1.1.1.1, and may answer the Portal's name with a LAN address, which the
agent is denied. So the agent looks up the Portal host, and the Doorbell
host, over **DNS-over-HTTPS** instead: an RFC 8484 query for A records to
`https://1.1.1.1/dns-query` (Cloudflare), then `https://8.8.8.8/dns-query`
(Google). Both are named by IP, so DoH needs no DNS, and both certificates
cover those IPs. (Quad9's 9.9.9.9 needs HTTP/2 for DoH, which the agent's
client doesn't speak.)

- It keeps only public answers: a private, loopback or link-local one is
  dropped and logged, as for the Doorbell host.
- It connects to that address but as the host name: TLS (SNI and the
  certificate check) and the HTTP `Host` header are still the Portal's, so
  nothing else changes.
- An answer is kept for its TTL, at least 5 minutes and at most an hour,
  so polls don't each cost a lookup (one is about 5 KB with its TLS
  handshake: under 50 MB a month at the 5-minute floor). A connection to a kept address that fails
  looks it up again next time.
- If DoH fails (blocked, or no answer within 3 s), the agent logs it once
  and uses the resolver above, trying DoH again every 5 minutes. A LAN
  answer from there is still blocked by the unit, as before.
- A development override (`SP_RTK_BASE_PORTAL_URL`) on an IP address or
  `localhost` skips DoH. Any other name is looked up over DoH too, so an
  override name only the local DNS knows falls back to it (with the
  warning).

To check DoH from the base: `journalctl -u sp-rtk-base-portal-agent`
shows `using system DNS` when it fell back, `Dropped the DoH answer ...`
for a private answer.
