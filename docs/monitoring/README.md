# Monitoring sp-rtk-base with Prometheus + Grafana

sp-rtk-base exposes a Prometheus metrics endpoint at `/metrics` on the
same port as the UI (8080 by default).  This directory contains:

| File | Purpose |
|---|---|
| [`grafana-dashboard-sp-rtk-base.json`](grafana-dashboard-sp-rtk-base.json) | Unified Grafana dashboard.  Import into Grafana → New → Import → upload JSON.  Templated on a `$base` variable so one dashboard works for one base station or many. |
| [`prometheus-scrape-config.example.yml`](prometheus-scrape-config.example.yml) | Drop-in `scrape_config` snippet — point Prometheus at your sp-rtk-base instance. |

## Quick start

1. **Tell Prometheus to scrape sp-rtk-base.**  Paste the contents of
   `prometheus-scrape-config.example.yml` into your `prometheus.yml`'s
   `scrape_configs:` list and adjust the target + `base:` label.
   Reload Prometheus.

   ```yaml
   scrape_configs:
     - job_name: sp-rtk-base
       scrape_interval: 30s
       static_configs:
         - targets: ["rtk-base.lan:8080"]
           labels:
             base: home
   ```

2. **Verify the scrape is succeeding** in Prom's UI:
   `http://your-prometheus:9090/targets` — the `sp-rtk-base` job should
   show `UP`.

3. **Import the dashboard.**  In Grafana: *Dashboards → New →
   Import* → upload `grafana-dashboard-sp-rtk-base.json` → pick your
   Prom datasource.  The **Base** dropdown at the top populates from
   the metric labels and defaults to "All".

## Why the `base` label

Every panel filters with `{base=~"$base"}`.  This lets one dashboard
work whether you have:

- **One base station** (`base: home`) — the dropdown shows just that one.
- **Multiple base stations** (`base: home`, `base: field-01`, …) — pick
  one to focus on, pick several to compare side-by-side, or leave
  "All" selected to overlay every base station on each panel.

If you scrape without setting `base`, the dashboard's variable will
have no values to choose from and the queries will return nothing.
The label is just a free-text identifier — pick whatever's meaningful
per base station (location name, callsign, antenna ID, etc.).

## What's exposed

The current metric surface (15 series) covers:

- Service state: `sp_rtk_base_relay_running`, `sp_rtk_base_relay_uptime_seconds`
- Input source: `sp_rtk_base_input_connected`, `sp_rtk_base_input_bytes_received`,
  `sp_rtk_base_input_seconds_since_last_data` *(the canonical "is data flowing"
  watchdog)*
- Hub throughput: `sp_rtk_base_chunks_distributed`, `sp_rtk_base_frames_parsed`
- Destinations (labelled by `destination`):
  - `sp_rtk_base_dest_connected`
  - `sp_rtk_base_dest_bytes_sent`
  - `sp_rtk_base_dest_messages_sent`
  - `sp_rtk_base_dest_messages_dropped`
  - `sp_rtk_base_dest_errors`
  - `sp_rtk_base_dest_queue_depth`
- Destination counts: `sp_rtk_base_active_destinations`, `sp_rtk_base_total_destinations`

These are all gauges that update on each `relay.get_status()` poll.
You can still use `rate()` on the byte/message counters — they
increase monotonically until a relay restart.

## Logs (optional)

The bundled dashboard also has a **Logs** panel at the bottom that
queries a **Loki** datasource.  It's purely additive — if you don't
have Loki, just delete that panel after importing or ignore it
(Grafana will render it empty).

To make it light up, ship `sp-rtk-base.service` logs to a Loki instance
with at least these labels:

| Label | Value | Why |
|---|---|---|
| `base` | matches the `base` value you set in your Prom scrape | Lets the dashboard's `$base` dropdown filter the Logs panel together with the metrics panels |
| `service` | `sp-rtk-base` | Lets the Logs panel pin to just the relay app even if other services share the same `base` |

Any log shipper that talks Loki works.  Two common shapes:

**Grafana Alloy on the host** (recommended if you're already using
Alloy for metrics — single binary, same config file):

```alloy
loki.source.journal "sp_rtk_base" {
  forward_to = [loki.write.target.receiver]
  matches    = "_SYSTEMD_UNIT=sp-rtk-base.service"
  labels = {
    base    = "home",
    service = "sp-rtk-base",
  }
}

loki.write "target" {
  endpoint { url = "http://loki.your-network.local:3100/loki/api/v1/push" }
}
```

**Promtail** (if you're already running Promtail elsewhere):

```yaml
scrape_configs:
  - job_name: sp-rtk-base
    journal:
      matches: _SYSTEMD_UNIT=sp-rtk-base.service
      labels:
        base: home
        service: sp-rtk-base
```

Then in Grafana → Connections → Data sources, add a Loki source
pointed at your Loki, and the dashboard's `$DS_LOKI` dropdown will
pick it up on next import (or via *Dashboard settings → Variables*).

The Logs panel uses LogQL:

```logql
{base=~"$base", service="sp-rtk-base"}
```

so once labels are right you'll see live logs filtered by whichever
base station the `$base` selector is set to.

## Disabling /metrics

The endpoint is gated by the `metrics_enabled` setting (Settings page
in the UI, or `settings.metrics_enabled: false` in your YAML config).
When disabled the endpoint returns 404.

## Scraping over the internet

The `/metrics` endpoint has **no authentication**.  If you want a
Prometheus elsewhere on the internet to scrape it, do not expose
`:8080` directly — terminate at a reverse proxy / mesh VPN that adds
auth.  Push-mode (agent on the host, remote_write to a private
receiver) is the cleaner pattern for remote-site deployments — that
path is out of scope here.
