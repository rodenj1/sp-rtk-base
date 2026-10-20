# Monitoring sp-rtk-base with Prometheus + Grafana

sp-rtk-base exposes a Prometheus metrics endpoint at `/metrics` on the
same port as the UI (8080 by default).  This directory contains:

| File | Purpose |
|---|---|
| [`grafana-dashboard-sp-rtk-base.json`](grafana-dashboard-sp-rtk-base.json) | Unified Grafana dashboard.  Import into Grafana → New → Import → upload JSON.  Templated on a `$base` variable so one dashboard works for one base station or many. |
| [`prometheus-scrape-config.example.yml`](prometheus-scrape-config.example.yml) | Drop-in `scrape_config` snippet — point Prometheus at your sp-rtk-base instance. |
| [`prometheus-alerts.example.yml`](prometheus-alerts.example.yml) | Example alert rules for Signal Quality: degraded for 10 minutes, or no data for 5 minutes while the Relay runs. See [Alerts](#alerts). |

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

The current metric surface covers:

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
- Signal Quality (how well the antenna hears the satellites, exactly as the
  dashboard and survey-in card show it, in both modes):
  - `sp_rtk_base_signal_quality`: verdict, 0 Good, 1 Marginal, 2 Poor
  - `sp_rtk_base_signal_band_strength_dbhz{band="L1"|"L2"}`: Band strength
    (mean C/N0 of the 4 strongest signals in the band)
  - `sp_rtk_base_signal_usable_satellites`: satellites with a signal ≥ 35 dB-Hz
  - `sp_rtk_base_signal_available`: 1 with current data; 0 when stale or no
    data, when the other three are NaN (the series never disappear)
- Bluetooth link strength (how well the base station hears its Bluetooth
  module on a Bluetooth Input profile, exactly as the dashboard shows it;
  not satellite reception):
  - `sp_rtk_base_bluetooth_link_strength`: verdict, 0 Good, 1 Marginal, 2 Poor
  - `sp_rtk_base_bluetooth_link_median_db`: the lower median reading in
    whole relative dB, not dBm (0 = comfortable, negative = that many dB
    below it)
  - Both are NaN with no reading, while the Relay is stopped, or when the
    Input isn't Bluetooth
- Network links (the base station's own Ethernet and Wi-Fi links and its
  Uplink, exactly as the dashboard's Network card shows them): see
  [Network links](#network-links) below.

Apart from the Network link counters below, these are all gauges that
update on each scrape of `/metrics`.

### Network links

A background sampler reads every Network link every 2 s; each scrape
exports its latest snapshot and never reads the host itself. What the
dashboard's Network card shows, and the sampler's limitations, are in
[`docs/network-links.md`](../network-links.md). Labels are
`interface` (such as `eth0`, `wlan0`) and `kind` (`ethernet` or `wifi`);
the `ethernet_*` and `wifi_*` metrics carry only `interface`. No SSID, BSSID, MAC or IP
address appears in any label.

| Metric | Type | Unit | Labels | Value |
|---|---|---|---|---|
| `sp_rtk_base_network_link_up` | gauge | 0/1 | `interface`, `kind` | 1 Connected, 0 Disconnected; NaN with No reading |
| `sp_rtk_base_network_link_uplink` | gauge | 0/1 | `interface`, `kind` | 1 on the link that is the Uplink; NaN while the Uplink has no reading |
| `sp_rtk_base_network_uplink_other` | gauge | 0/1 | none | 1 when the Uplink isn't a Network link (a VPN such as `wg0`); NaN while the Uplink has no reading |
| `sp_rtk_base_network_link_changes_total` | counter | transitions | `interface`, `kind` | up/down transitions the sampler has seen since the app started |
| `sp_rtk_base_network_{receive,transmit}_bytes_total` | counter | bytes | `interface`, `kind` | kernel totals (`/sys/class/net/*/statistics`) |
| `sp_rtk_base_network_{receive,transmit}_packets_total` | counter | packets | `interface`, `kind` | kernel totals |
| `sp_rtk_base_network_{receive,transmit}_errors_total` | counter | packets | `interface`, `kind` | kernel totals |
| `sp_rtk_base_network_{receive,transmit}_drops_total` | counter | packets | `interface`, `kind` | kernel totals |
| `sp_rtk_base_network_ethernet_speed_bits_per_second` | gauge | bits/s | `interface` | negotiated speed; NaN when Disconnected or No reading |
| `sp_rtk_base_network_ethernet_full_duplex` | gauge | 0/1 | `interface` | 1 full, 0 half duplex; NaN when Disconnected or No reading |
| `sp_rtk_base_wifi_link_strength` | gauge | level | `interface` | Wi-Fi link strength verdict, 0 Good (≥ -67 dBm), 1 Marginal (-68 to -77), 2 Poor (≤ -78), on the lower median of the last 10 s of readings; station mode only; NaN when Disconnected or No reading |
| `sp_rtk_base_wifi_link_strength_dbm` | gauge | dBm | `interface` | the latest Wi-Fi link strength reading (not the median); NaN when Disconnected or No reading |
| `sp_rtk_base_wifi_frequency_hertz` | gauge | Hz | `interface` | channel frequency while associated; NaN when Disconnected or No reading (never read unassociated: it hops while scanning) |
| `sp_rtk_base_wifi_tx_bitrate_bits_per_second` | gauge | bits/s | `interface` | bitrate of the last frames sent; NaN when Disconnected or No reading |
| `sp_rtk_base_wifi_rx_bitrate_bits_per_second` | gauge | bits/s | `interface` | bitrate of the last frames received; NaN when Disconnected or No reading |
| `sp_rtk_base_wifi_tx_failed_total` | counter | frames | `interface` | frames the access point never acknowledged, as the driver counts them; resets with each association |
| `sp_rtk_base_network_last_sample_timestamp_seconds` | gauge | Unix seconds | none | time of the last sample in which every source read (nl80211 for every Wi-Fi interface too); NaN before the first |

**Missing values.**

- **No reading** (a failed read, or the last good one over 6 s old) and
  **Disconnected** make a gauge NaN; its series stays.
- **Not supported:** a reading the hardware never gives (such as Ethernet
  speed on an adapter that doesn't report it) has no series at all. It
  appears once a successful read gives it while the link is up.
- **Counters never go NaN.** During No reading (and, for
  `wifi_tx_failed_total`, while Disconnected) they hold their last value.
- **Wi-Fi details come from nl80211 on their own.** If it fails, only the
  `wifi_*` gauges go NaN; the link's state and kernel counters stay current.
- **Interfaces coming and going:** when an interface vanishes (say a USB
  adapter is unplugged), every series for it disappears with it.

**Counter resets.** The counters are exported exactly as the kernel
reports them, with no accumulation on our side. When an interface
reappears, its kernel counters (and `link_changes_total`) start again at
zero: that is an ordinary counter reset, which `rate()` and `increase()`
already handle. `wifi_tx_failed_total` also resets with every
association (the driver counts per association), likewise an ordinary
reset.

Example queries:

```promql
# Receive throughput per link, bytes/s
rate(sp_rtk_base_network_receive_bytes_total[5m])

# Errors and drops per link over the last hour
increase(sp_rtk_base_network_receive_errors_total[1h])
  + increase(sp_rtk_base_network_receive_drops_total[1h])

# Links that keep dropping: more than 3 up/down transitions in 15 min
increase(sp_rtk_base_network_link_changes_total[15m]) > 3

# Wi-Fi sends failing, per minute
rate(sp_rtk_base_wifi_tx_failed_total[5m]) * 60

# Weak Wi-Fi: Poor link strength for 5 min
min_over_time(sp_rtk_base_wifi_link_strength[5m]) == 2

# Wi-Fi fallen back to slow rates: under 20 Mbit/s sent for 10 min
max_over_time(sp_rtk_base_wifi_tx_bitrate_bits_per_second[10m]) < 20e6

# Which link is the Uplink
sp_rtk_base_network_link_uplink == 1

# The sampler has stopped: no full sample for over a minute
time() - sp_rtk_base_network_last_sample_timestamp_seconds > 60
```

NaN compares false, so the last query (like any threshold on a NaN gauge)
doesn't fire before the first full sample. To catch that too, add
`or sp_rtk_base_network_last_sample_timestamp_seconds != sp_rtk_base_network_last_sample_timestamp_seconds`,
which is true only for NaN.

## Alerts

[`prometheus-alerts.example.yml`](prometheus-alerts.example.yml) has two
rules for an unattended base; add it under `rule_files:` in `prometheus.yml`:

| Alert | Fires when |
|---|---|
| `SignalQualityDegraded` | `sp_rtk_base_signal_quality >= 1` for 10m: Marginal or Poor long enough to rule out a passing obstruction |
| `SignalQualityUnavailable` | `sp_rtk_base_signal_available == 0 and sp_rtk_base_relay_running == 1` for 5m: the Relay runs but no RTCM, or no MSM, is arriving |

The dashboard's **▶ Signal Quality** row shows the verdict, Band strength
(with the 41 / 44 dB-Hz Good lines) and Usable satellites.
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
