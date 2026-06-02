# Monitoring sp-rtk-base with Prometheus + Grafana

sp-rtk-base exposes a Prometheus metrics endpoint at `/metrics` on the
same port as the UI (8080 by default).  This directory contains:

| File | Purpose |
|---|---|
| [`grafana-dashboard-sp-rtk-base.json`](grafana-dashboard-sp-rtk-base.json) | Unified Grafana dashboard.  Import into Grafana → New → Import → upload JSON.  Templated on a `$site` variable so one dashboard works for one site or many. |
| [`prometheus-scrape-config.example.yml`](prometheus-scrape-config.example.yml) | Drop-in `scrape_config` snippet — point Prometheus at your sp-rtk-base instance. |

## Quick start

1. **Tell Prometheus to scrape sp-rtk-base.**  Paste the contents of
   `prometheus-scrape-config.example.yml` into your `prometheus.yml`'s
   `scrape_configs:` list and adjust the target + `site:` label.
   Reload Prometheus.

   ```yaml
   scrape_configs:
     - job_name: sp-rtk-base
       scrape_interval: 30s
       static_configs:
         - targets: ["rtk-base.lan:8080"]
           labels:
             site: home
   ```

2. **Verify the scrape is succeeding** in Prom's UI:
   `http://your-prometheus:9090/targets` — the `sp-rtk-base` job should
   show `UP`.

3. **Import the dashboard.**  In Grafana: *Dashboards → New →
   Import* → upload `grafana-dashboard-sp-rtk-base.json` → pick your
   Prom datasource.  The `$site` dropdown at the top populates from
   the metric labels and defaults to "All".

## Why the `site` label

Every panel filters with `{site=~"$site"}`.  This lets one dashboard
work whether you have:

- **One instance** (`site: home`) — the dropdown shows just that one.
- **Multiple instances** (`site: home`, `site: field-01`, …) — pick
  one to focus on, pick several to compare side-by-side, or leave
  "All" selected to overlay every site on each panel.

If you scrape without setting `site`, the dashboard's variable will
have no values to choose from and the queries will return nothing.
The label is just a free-text identifier — pick whatever's meaningful.

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
