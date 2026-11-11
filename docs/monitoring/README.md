# Monitoring sp-rtk-base with Prometheus + Grafana

sp-rtk-base exposes a Prometheus metrics endpoint at `/metrics` on the
same port as the UI (8080 by default).  This directory contains:

| File | Purpose |
|---|---|
| [`grafana-dashboard-sp-rtk-base.json`](grafana-dashboard-sp-rtk-base.json) | Fleet Grafana dashboard.  Import into Grafana → New → Import → upload JSON.  A [Fleet overview](#fleet-overview) table at the top lists every base; Fleet trends and one section per base follow a `$base` variable, so one dashboard works for one base station or many.  See [The dashboard's layout](#the-dashboards-layout). |
| [`prometheus-scrape-config.example.yml`](prometheus-scrape-config.example.yml) | Drop-in `scrape_config` snippet — point Prometheus at your sp-rtk-base instance. |
| [`prometheus-alerts.example.yml`](prometheus-alerts.example.yml) | Example alert rules for Signal Quality: degraded for 10 minutes, or no data for 5 minutes while the Relay runs. See [Alerts](#alerts). |

## Quick start

1. **Tell Prometheus to scrape sp-rtk-base.**  Paste the contents of
   `prometheus-scrape-config.example.yml` into your `prometheus.yml`'s
   `scrape_configs:` list and adjust the target + `base:` label.
   Name the job what you like (see
   [The job name and the `base` label](#the-job-name-and-the-base-label)).
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
   `http://your-prometheus:9090/targets` — the job (`sp-rtk-base` above) should
   show `UP`.

3. **Import the dashboard.**  In Grafana: *Dashboards → New →
   Import* → upload `grafana-dashboard-sp-rtk-base.json` → pick your
   Prom datasource.  The **Job** dropdown at the top finds the scrape
   job your bases are under by itself.  The **Base** dropdown next to it
   lists every base Prometheus scrapes for that job, Down ones included,
   and defaults to "All".  Keep the dashboard's UID
   (`sp-rtk-base-overview`): the Fleet overview's base links point at it.

## The job name and the `base` label

- **The scrape job can have any name.**  The dashboard's **Job**
  variable finds it: it lists the jobs that export
  `sp_rtk_base_relay_running` and selects the first.  A hand-written
  `scrape_config` names the job `job_name`; Grafana Alloy names it after
  the scrape component, such as `prometheus.scrape.sp_rtk_base`.  If
  several jobs export sp-rtk-base metrics, pick one in the Job dropdown.
  Every query filters on `job="$job"`, and the Fleet overview is
  anchored on Prometheus's own scrape status, `up{job="$job"}`, so that
  a base Prometheus can't scrape keeps its row as Down.  Scrape all your
  bases under one job: the dashboard shows one job at a time.
- **Every target must carry a `base` label**, as below.  This one is a
  requirement.

Every panel below the Fleet overview filters with `{base=~"$base"}`.
This lets one dashboard work whether you have:

- **One base station** (`base: home`) — the dropdown shows just that one.
- **Multiple base stations** (`base: home`, `base: field-01`, …) — pick
  one to focus on, pick several to compare them in Fleet trends, or
  leave "All" selected to get a section for every base.

If you scrape without setting `base`, the dashboard's variable will
have no values to choose from and the queries will return nothing.
The label is just a free-text identifier — pick whatever's meaningful
per base station (location name, callsign, antenna ID, etc.).

## The dashboard's layout

From the top:

1. **Fleet overview:** a table with one row per base, worst
   [Base health](#base-health) first.  It always shows the whole fleet,
   whatever the Base selector says.  Click a base's name to select just
   that base.  See [Fleet overview](#fleet-overview).
2. **Fleet trends:** the selected bases overlaid on four graphs, one
   colour per base with a legend by base.  A base's colour comes from its
   name, so it keeps it however you filter.
   - Wi-Fi link strength, for bases whose Uplink is Wi-Fi only, with the
     Poor line (-78 dBm);
   - CPU temperature, with the 80 °C line;
   - CPU busy %;
   - L1 Band strength, with the 41 dB-Hz line.
3. **One section per selected base,** open, titled with the base's name,
   Base health, version, Input kind and Uplink (as of the last refresh of
   the Base selector).  Each section has:
   - a Base health strip.  When the base is Down it says so, and that its
     panels show the last data before it went Down;
   - status tiles: Relay and uptime, Input and its kind, Last RTCM, Signal
     Quality, Bluetooth link strength, Wi-Fi link strength (dBm, Wi-Fi link
     band, and whether the link is the Uplink), CPU temperature, and
     undervoltage events in the last 24 h;
   - graphs: Input bytes/s; Band strength L1 / L2 with the 41 / 44 dB-Hz
     lines; Wi-Fi link strength split by Wi-Fi link band (5 GHz blue,
     2.4 GHz aqua) with the Good and Poor lines; Bluetooth link strength in
     relative dB; CPU busy and memory used % on one 0–100 axis; CPU
     temperature with the 70 °C amber and 80 °C red lines;
   - a Destinations table: connected, bytes/s, messages/s, drops/s, queue
     depth and errors/s.
4. **Logs · base:** a collapsed row per selected base with its
   `sp-rtk-base.service` logs (needs Loki, see [Logs](#logs-optional)).
   Grafana's classic dashboard rows can't nest, so the Logs rows follow
   the base sections rather than sitting inside them; with one base
   selected, its Logs row is directly below its section.

Something that doesn't apply says so in words and never takes a status
colour: Bluetooth link strength reads **n/a** only on an Input that
isn't Bluetooth (a Bluetooth Input with no current reading reads **No
reading**, never n/a), Wi-Fi reads **—** on a base with no Wi-Fi interface, and an
idle Wi-Fi radio behind an Ethernet Uplink reads a grey **Disconnected**
that doesn't count against Base health.

**Mixed fleets.**  You can update bases one at a time.  A base still on an
older sp-rtk-base version doesn't export the newer readings (Host
resources, Wi-Fi link band, Input kind, version), so those read **—** for
it in the Fleet overview, its section title and its tiles, and its graphs
for them stay empty.  It still gets a Base health, judged on what it
exports.

## Fleet overview

The table at the top of the dashboard has one row per base, sorted
worst [Base health](#base-health) first and then by base name.  It
always shows the whole fleet: it ignores the Base selector.  Click a
base's name to set the Base selector to that base, keeping the time
range.

Each column is one instant query reduced to one series per base, and
the rows are merged on `base`.  Columns, in order: Base, Base health,
Relay, Input, Last RTCM, Signal Quality, Destinations (active/total),
Bluetooth link strength, Uplink, Wi-Fi link strength, CPU %, Memory %,
CPU temperature, Undervoltage events in the last 24 h, Relay uptime and
Version.

- **No reading** (a NaN gauge) and **—** (no series at all: a base on an
  older version, a reading that's Not supported, or a Down base) are both
  muted grey, never a status colour.
- **Bluetooth link strength** reads `n/a · <kind>` only when the Input
  isn't Bluetooth (`kind` from `sp_rtk_base_input_info`); a Bluetooth
  Input with no current reading reads **No reading**.
- **Wi-Fi link strength** is the latest reading in dBm with the Wi-Fi link
  band, coloured Good (≥ -67 dBm), Marginal (-68 to -77) or Poor (≤ -78).
  It reads **—** with no Wi-Fi interface and a grey **Disconnected** for an
  idle radio (as behind an Ethernet Uplink).  A connected Wi-Fi link that
  isn't the Uplink is greyed and marked `not Uplink`: it doesn't count
  against Base health.  With more than one Wi-Fi interface, one is shown.

## Base health

A base's single rollup state as the monitoring stack sees it: **OK**,
**Degraded**, **Fault** or **Down**.  It is computed by the Fleet
overview's query, not exported, because a base can't report itself
Down.  This section is the one place the rule and its thresholds are
written down; the dashboard implements exactly this.

The worst state that applies wins:

| State | When | From |
|---|---|---|
| **Down** | Prometheus can't scrape the base | `up{job="$job"} == 0` |
| **Fault** | the Relay is stopped, including a deliberate stop | `sp_rtk_base_relay_running == 0` |
| | the Input is disconnected | `sp_rtk_base_input_connected == 0` |
| | no RTCM for over 60 s | `sp_rtk_base_input_seconds_since_last_data > 60` |
| **Degraded** | Signal Quality is Marginal or Poor | `sp_rtk_base_signal_quality >= 1` |
| | an enabled Destination is disconnected | `sp_rtk_base_active_destinations < sp_rtk_base_total_destinations` |
| | Bluetooth link strength is Poor | `sp_rtk_base_bluetooth_link_strength == 2` |
| | Wi-Fi link strength is Poor *and* that link is the Uplink | `sp_rtk_base_wifi_link_strength == 2` on the interface where `sp_rtk_base_network_link_uplink == 1` |
| | CPU temperature is 80 °C or above | `sp_rtk_base_host_cpu_temperature_celsius >= 80` |
| | any undervoltage event in the last hour | `increase(sp_rtk_base_host_undervoltage_events_total[1h]) > 0` |
| | less than 10 % of memory available | `sp_rtk_base_host_memory_available_bytes / sp_rtk_base_host_memory_total_bytes < 0.1` |
| | less than 10 % of the root filesystem free | `sp_rtk_base_host_root_filesystem_available_bytes / sp_rtk_base_host_root_filesystem_size_bytes < 0.1` |
| **OK** | none of the above | |

- **An absent or NaN reading counts as not failing**, so a base on an older
  version is judged on what it exports, and No reading never makes a base
  Degraded.  (No RTCM yet, `-1`, isn't over 60 s either; a stopped Relay is
  already a Fault.)
- **Wi-Fi counts only on the Uplink**, so an idle or unused radio behind an
  Ethernet Uplink never flags a base.
- **Last RTCM** uses the same 60 s as Fault; in the table it turns amber
  from 10 s.

To tune a threshold, edit the Base health query of the Fleet overview
panel (and this table).  Each check there is one line, labelled with
`check`; in Grafana's *Explore*, drop the outer `max by (base)` to see
which checks fire for a base.

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
- Host resources (the base computer's own temperature, power supply and
  more): see
  [Host resources](#host-resources) below.

- Input kind, build version and the app's own process: see
  [Input kind, build version and process](#input-kind-build-version-and-process)
  below.

Apart from the Network link counters and the process metrics below, these
are all gauges that update on each scrape of `/metrics`.

### Input kind, build version and process

| Metric | Type | Unit | Labels | Value |
|---|---|---|---|---|
| `sp_rtk_base_input_info` | gauge (info-style) | — | `kind` | always 1; `kind` is the active Input profile's type (`bluetooth`, `serial` or `tcp`); absent with no Input profile |
| `sp_rtk_base_build_info` | gauge (info-style) | — | `version` | always 1; `version` is the installed sp-rtk-base version; always present |
| `process_*` (standard set) | various | various | none | the client library's standard process metrics for the app's own process: `process_cpu_seconds_total`, `process_resident_memory_bytes`, `process_virtual_memory_bytes`, `process_start_time_seconds`, `process_open_fds`, `process_max_fds` |

An info-style metric's series *is* its value: join on it, or read its
label, rather than graphing it. For example, `sp_rtk_base_input_info`
tells a Bluetooth link strength of NaN on a non-Bluetooth Input (shown as
"n/a": it doesn't apply) apart from NaN on a Bluetooth Input (shown as
"No reading").

The process metrics are the one collect-on-scrape exception: everything
else is exported from state the app already holds (or a background
sampler's latest snapshot), while these read `/proc/self` during the
scrape. They cover only the app's own process, never the host.

```promql
# The app's CPU use, as a share of one core
rate(process_cpu_seconds_total{base="home"}[5m])

# Which version each base runs
sp_rtk_base_build_info
```

### Network links

A background sampler reads every Network link every 2 s; each scrape
exports its latest snapshot and never reads the host itself. What the
dashboard's Network card shows, and the sampler's limitations, are in
[`docs/network-links.md`](../network-links.md). Labels are
`interface` (such as `eth0`, `wlan0`) and `kind` (`ethernet` or `wifi`);
the `ethernet_*` and `wifi_*` metrics carry only `interface` (bar
`wifi_link_band`, which adds `band`). No SSID, BSSID, MAC or IP
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
| `sp_rtk_base_wifi_link_band` | gauge (info-style) | — | `interface`, `band` | always 1; `band` is the Wi-Fi link band the link is associated on, from its channel frequency: `2.4GHz` (2400–2500 MHz), `5GHz` (5150–5925 MHz) or `6GHz` (5925–7125 MHz), each including its lower edge only; no series when Disconnected, on No reading, or outside every band |
| `sp_rtk_base_network_last_sample_timestamp_seconds` | gauge | Unix seconds | none | time of the last sample in which every source read (nl80211 for every Wi-Fi interface too); NaN before the first |

**Missing values.**

- **No reading** (a failed read, or the last good one over 6 s old) and
  **Disconnected** make a gauge NaN; its series stays.
- **Not supported:** a reading the hardware never gives (such as Ethernet
  speed on an adapter that doesn't report it) has no series at all. It
  appears once a successful read gives it while the link is up.
- **Counters never go NaN.** During No reading (and, for
  `wifi_tx_failed_total`, while Disconnected) they hold their last value.
- **`wifi_link_band` disappears instead of going NaN.** It's an info
  metric: its series *is* its value (the `band` label), so a NaN series
  would still claim a band. With no current in-band frequency
  (Disconnected, No reading, or a frequency outside every band) it has
  no series, and it reappears, possibly with another `band`, on the next
  in-band reading.
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

# Wi-Fi link strength split by Wi-Fi link band: one series per base,
# interface and band (absent while the link has no band)
sp_rtk_base_wifi_link_strength_dbm
  * on(base, interface) group_left(band) sp_rtk_base_wifi_link_band

# Which link is the Uplink
sp_rtk_base_network_link_uplink == 1

# The sampler has stopped: no full sample for over a minute
time() - sp_rtk_base_network_last_sample_timestamp_seconds > 60
```

NaN compares false, so the last query (like any threshold on a NaN gauge)
doesn't fire before the first full sample. To catch that too, add
`or sp_rtk_base_network_last_sample_timestamp_seconds != sp_rtk_base_network_last_sample_timestamp_seconds`,
which is true only for NaN.

### Host resources

A second background sampler, separate from the Network link one, reads
the base computer's own **Host resources** every 2 s; each scrape exports
its latest snapshot and never reads the host itself. A failure in one
sampler never touches the other's freshness. Only the CPU time counter
has a label (`mode`); there is no per-core breakdown.

| Metric | Type | Unit | Labels | Value |
|---|---|---|---|---|
| `sp_rtk_base_host_cpu_seconds_total` | counter | seconds | `mode` | CPU time since boot by mode (`user`, `nice`, `system`, `idle`, `iowait`, `irq`, `softirq`, `steal`; fewer on older kernels), summed over every core, from the aggregate `cpu` line of `/proc/stat`. Guest time is already inside `user` and `nice`. A mode whose kernel total falls (iowait can) is clamped: it never goes backwards. Holds its last value with No reading; absent before the first reading |
| `sp_rtk_base_host_memory_total_bytes` | gauge | bytes | none | total usable RAM (`MemTotal`); NaN with No reading |
| `sp_rtk_base_host_memory_available_bytes` | gauge | bytes | none | the kernel's estimate of RAM available to start new applications without swapping (`MemAvailable`, not `MemFree`); no swap; NaN with No reading |
| `sp_rtk_base_host_cpu_temperature_celsius` | gauge | °C | none | the SoC temperature, from the thermal zone whose type is `cpu-thermal` (found by type, never by zone number); NaN with No reading; absent when Not supported |
| `sp_rtk_base_host_undervoltage` | gauge | 0/1 | none | 1 if the Pi firmware saw undervoltage in roughly the last 2 s, from the alarm of the hwmon device named `rpi_volt` (found by name, never by hwmon number), else 0; NaN with No reading; absent when Not supported |
| `sp_rtk_base_host_undervoltage_events_total` | counter | events | none | each 0→1 rise of that alarm the 2 s sampler has seen since sp-rtk-base started; holds on No reading; absent when Not supported |
| `sp_rtk_base_host_root_filesystem_size_bytes` | gauge | bytes | none | size of the root filesystem; NaN with No reading |
| `sp_rtk_base_host_root_filesystem_available_bytes` | gauge | bytes | none | space on the root filesystem a non-root user can use (excludes the root-reserved blocks); NaN with No reading. Its read-only flag is ignored: the service unit's hardening always sets it |
| `sp_rtk_base_host_last_sample_timestamp_seconds` | gauge | Unix seconds | none | time of the last sample in which every reading read (a Not supported one counts as read); NaN before the first |

**Missing values.**

- **Not supported:** a host with no `cpu-thermal` zone (not a Pi: a dev
  laptop or a container) has no temperature series at all, and one with no
  `rpi_volt` device has neither undervoltage series.
- **No reading** (the read raised, or the last good one is over 6 s old)
  makes a gauge NaN; its series stays. A counter holds its last value.

**Service hardening.** The host sampler reads system-wide files under
`/proc` (`/proc/stat`, `/proc/meminfo`) and `/sys` (`/sys/class/thermal`,
`/sys/class/hwmon`) without root. `ProcSubset=pid` must never be added to
the service unit: it hides `/proc`'s system-wide files, so the CPU and
memory readings would stay at No reading.

**Undervoltage.** The kernel polls the Pi firmware every 2 s and reports
only whether undervoltage happened in roughly that last window: the alarm
doesn't latch, so a dip between two scrapes has already cleared from
`sp_rtk_base_host_undervoltage` by the next scrape. The sampler reads it
every 2 s and counts each rise, so query the counter, for example
`increase(sp_rtk_base_host_undervoltage_events_total[24h]) > 0`, rather
than the gauge. Because the kernel's 2 s cycle and the sampler's drift
against each other, the sampler can occasionally miss a window. The counter restarts from 0 when
sp-rtk-base restarts, which `increase()` handles.

**Examples.** CPU busy % over 5 minutes (everything but idle and iowait):

```promql
100 * (1 - sum by (instance) (rate(sp_rtk_base_host_cpu_seconds_total{mode=~"idle|iowait"}[5m]))
         / sum by (instance) (rate(sp_rtk_base_host_cpu_seconds_total[5m])))
```

Memory used % and free disk:

```promql
100 * (1 - sp_rtk_base_host_memory_available_bytes / sp_rtk_base_host_memory_total_bytes)
sp_rtk_base_host_root_filesystem_available_bytes
```

## Alerts

[`prometheus-alerts.example.yml`](prometheus-alerts.example.yml) has two
rules for an unattended base; add it under `rule_files:` in `prometheus.yml`:

| Alert | Fires when |
|---|---|
| `SignalQualityDegraded` | `sp_rtk_base_signal_quality >= 1` for 10m: Marginal or Poor long enough to rule out a passing obstruction |
| `SignalQualityUnavailable` | `sp_rtk_base_signal_available == 0 and sp_rtk_base_relay_running == 1` for 5m: the Relay runs but no RTCM, or no MSM, is arriving |

Each base's section on the dashboard shows the Signal Quality verdict
and Band strength (with the 41 / 44 dB-Hz lines).
You can still use `rate()` on the byte/message counters — they
increase monotonically until a relay restart.

## Logs (optional)

The bundled dashboard also has a collapsed **Logs** row for each
selected base that queries a **Loki** datasource.  It's purely
additive — if you don't have Loki, leave it collapsed (Grafana will
render it empty).

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
