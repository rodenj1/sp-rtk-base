# larson-base monitoring deployment runbook

Push sp-rtk-base metrics from a remote-site Raspberry Pi to a home
Kubernetes cluster's Prometheus, over public HTTPS, with Basic Auth at
the Traefik edge.  Only the `/api/v1/write` endpoint of Prometheus is
exposed publicly — everything else 404s.

This runbook is the deployment-time companion to the in-app Dashboard
(which already shows live rates and uptime locally).  Use it when the
operator can't be on-site to look at the Dashboard themselves.

```
┌──────────────────────────────┐                         ┌─────────────────────────────────────────────┐
│ Remote site — larson-base     │                         │ Home k8s cluster                             │
│   sp-rtk-base :8080 /metrics  │                         │   Traefik LB :443                            │
│           ▲                   │                         │      │                                       │
│           │  loopback scrape  │                         │      ▼                                       │
│   Grafana Alloy ──── HTTPS ───┼────── public 443 ──────┼─► IngressRoute prom-rw.YOUR-DOMAIN           │
│      + BasicAuth              │                         │      Path(/api/v1/write) only • BasicAuth   │
│      + WAL (24h buffer)       │                         │      → Service prometheus-operated:9090     │
│      + external_labels site=… │                         │                                              │
└──────────────────────────────┘                         │   Prometheus (kube-prometheus-stack)         │
                                                          │     enableRemoteWriteReceiver: true          │
                                                          │   Grafana → "SP-Base · larson-base"          │
                                                          └─────────────────────────────────────────────┘
```

Conventions in this doc:

- `YOUR-DOMAIN.example` — your real public domain.
- `monitoring` — the namespace where kube-prometheus-stack lives.
- `THE-SHARED-SECRET` — the random password used by Alloy + the htpasswd entry.  **Never commit this anywhere.**

---

## Part A — larson-base install

Run as root on the Pi.

### A.1  Install Grafana Alloy

```bash
curl -fsSL https://apt.grafana.com/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/grafana.gpg
echo "deb [signed-by=/usr/share/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
  | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt update
sudo apt install -y alloy
```

The package ships with a working `alloy.service` systemd unit reading
`/etc/alloy/config.alloy` and using `/var/lib/alloy/data` for the WAL.

### A.2  Drop the Alloy config

Copy [`alloy/config.alloy.example`](alloy/config.alloy.example) to
`/etc/alloy/config.alloy`, replacing `YOUR-DOMAIN.example`.

```bash
sudo cp /path/to/sp-rtk-base/docs/monitoring/alloy/config.alloy.example /etc/alloy/config.alloy
sudo sed -i 's/YOUR-DOMAIN\.example/your-real-domain.com/' /etc/alloy/config.alloy
```

### A.3  Drop the shared-secret password file (mode 0600)

```bash
sudo install -m 0600 -o alloy -g alloy /dev/null /etc/alloy/remote-write.pass
sudo tee /etc/alloy/remote-write.pass > /dev/null <<< 'THE-SHARED-SECRET'
```

Use the same plaintext password the cluster-side htpasswd entry was
generated from (Part B.2).

### A.4  Enable and verify

```bash
sudo systemctl enable --now alloy
sudo systemctl status alloy --no-pager
sudo journalctl -u alloy -f
```

Expect `level=info msg="Done replaying WAL"` then periodic successful
remote_write logs.  An auth failure looks like
`level=error msg="non-recoverable error" status=401`.

---

## Part B — Cluster-side setup

All four file types below live in your ArgoCD-managed manifests repo.
Templates with placeholders are in [`k8s/`](k8s/).

### B.1  Enable the remote-write receiver on the Prometheus CR

Edit your kube-prometheus-stack values overlay — see
[`k8s/values-overlay-snippet.yaml`](k8s/values-overlay-snippet.yaml) for
the exact diff.  In short:

```yaml
prometheus:
  prometheusSpec:
    enableRemoteWriteReceiver: true
```

After ArgoCD syncs, verify on a fresh Prom pod:

```bash
kubectl -n monitoring exec sts/prometheus-kube-prometheus-prometheus -- \
  wget -qO- http://localhost:9090/api/v1/status/flags \
  | jq '.["web.enable-remote-write-receiver"]'
# → "true"
```

Find the Service name the chart created (you'll need it in B.4):

```bash
kubectl -n monitoring get svc | grep prometheus
# typically: prometheus-operated  (port 9090, headless)
# or:        <release>-kube-prometheus-prometheus
```

### B.2  Generate the htpasswd-format SealedSecret

On a workstation with `htpasswd` (`apache2-utils`) and `kubeseal`:

```bash
# 1. Generate one user.  Use a long random password (also paste it
#    into /etc/alloy/remote-write.pass on larson-base).
htpasswd -nbB larson-base 'THE-SHARED-SECRET' > users.txt

# 2. Render the plain Secret YAML (do NOT commit).
kubectl create secret generic prom-rw-basicauth \
  --namespace=monitoring \
  --from-file=users=users.txt \
  --dry-run=client -o yaml > prom-rw-basicauth.secret.yaml

# 3. Seal it for your cluster (controller name + ns are the sealed-secrets
#    Helm chart defaults; substitute if you installed it differently).
kubeseal --controller-name sealed-secrets-controller \
         --controller-namespace kube-system \
         --format yaml \
         < prom-rw-basicauth.secret.yaml \
         > prom-rw-basicauth.sealedsecret.yaml

# 4. Commit the SealedSecret.  Wipe the plain secret + htpasswd input.
shred -u prom-rw-basicauth.secret.yaml users.txt
```

The SealedSecret decrypts in-cluster to a `Secret` named
`prom-rw-basicauth` in the `monitoring` namespace.  See
[`k8s/prom-rw-basicauth.sealedsecret.yaml.example`](k8s/prom-rw-basicauth.sealedsecret.yaml.example)
for an annotated shape stub (NOT a real credential — your kubeseal step
produces the actual encrypted blob).

### B.3  Traefik Middleware

Apply [`k8s/prom-rw-middleware.yaml`](k8s/prom-rw-middleware.yaml).  It
references the `prom-rw-basicauth` Secret produced by B.2.

### B.4  Traefik IngressRoute (the security boundary)

Apply [`k8s/prom-rw-ingressroute.yaml`](k8s/prom-rw-ingressroute.yaml).
Key constraint: the route uses **exact** `Path(/api/v1/write)`, not
`PathPrefix`.  Everything else on the `prom-rw.YOUR-DOMAIN` host returns
404 because no other route matches — Prom's admin / query / config API
is unreachable from outside.

Edit two values:

- `Host(\`prom-rw.YOUR-DOMAIN.example\`)` — your real hostname
- `services[0].name: prometheus-operated` — whatever B.1 reported

Pick the right TLS shape for your cluster:

- **cert-manager**: keep the `secretName: prom-rw-tls` line and add a
  `Certificate` CR pointing at your ClusterIssuer that writes into it.
- **Traefik built-in resolver**: replace `secretName:` with
  `certResolver: <your-resolver-name>`.

### B.5  DNS

Add an `A`/`CNAME` for `prom-rw.YOUR-DOMAIN.example` → your Traefik LB
IP/CNAME.  Same shape as every other IngressRoute hostname you already
serve.

### B.6  Grafana dashboard

Import [`grafana-dashboard-sp-rtk-base.json`](grafana-dashboard-sp-rtk-base.json)
into Grafana (Dashboards → Import → paste JSON).  The dashboard has a
`$site` template variable populated from
`label_values(sp_rtk_base_relay_running, site)`, so the same panels work
for every site you onboard later.

### B.7  ArgoCD wiring

Drop the four cluster-side files into the manifests directory ArgoCD
reconciles for the `monitoring` namespace:

```
clusters/<your-cluster>/monitoring/
├── prom-rw-basicauth.sealedsecret.yaml      (B.2 — actual SealedSecret)
├── prom-rw-middleware.yaml                  (B.3)
├── prom-rw-ingressroute.yaml                (B.4)
└── (cert-manager Certificate, if applicable)
```

The values-overlay change from B.1 goes into whatever values file your
existing kube-prometheus-stack Application references.

---

## Verification

### Cluster side (do BEFORE configuring Alloy on the Pi)

```bash
# IngressRoute + Middleware materialised
kubectl -n monitoring get ingressroute prom-rw -o yaml
kubectl -n monitoring get middleware prom-rw-basicauth -o yaml

# SealedSecret unsealed correctly
kubectl -n monitoring get secret prom-rw-basicauth \
  -o jsonpath='{.data.users}' | base64 -d | head -1
# → larson-base:$2y$05$…

# Receiver enabled in Prom
kubectl -n monitoring exec sts/prometheus-kube-prometheus-prometheus -- \
  wget -qO- http://localhost:9090/api/v1/status/flags \
  | jq '.["web.enable-remote-write-receiver"]'
# → "true"

# Cert valid + serving the right hostname
echo | openssl s_client -servername prom-rw.YOUR-DOMAIN.example \
  -connect prom-rw.YOUR-DOMAIN.example:443 2>/dev/null \
  | openssl x509 -noout -subject -dates
```

### End-to-end checks from the public internet

```bash
# 1. Bare GET → 404 (Path() doesn't match).
curl -sI https://prom-rw.YOUR-DOMAIN.example/
# → HTTP/2 404

# 2. Right path, no auth → 401.
curl -sI -X POST https://prom-rw.YOUR-DOMAIN.example/api/v1/write
# → HTTP/2 401

# 3. Right path, right auth, empty body → 400 (Prom rejects empty payload).
#    Proves the full path works.
curl -i -u larson-base:'THE-SHARED-SECRET' \
  -X POST https://prom-rw.YOUR-DOMAIN.example/api/v1/write \
  -H 'Content-Type: application/x-protobuf' --data-binary ''
# → HTTP/2 400 ; body mentions "failed to decode write request"

# 4. Any other Prom path under the same host → 404 (proves no leak).
curl -sI -X GET 'https://prom-rw.YOUR-DOMAIN.example/api/v1/query?query=up'
# → HTTP/2 404
curl -sI https://prom-rw.YOUR-DOMAIN.example/-/healthy
# → HTTP/2 404
```

### After Alloy is enabled on larson-base

On the Pi:

```bash
sudo journalctl -u alloy --since '5 min ago' \
  | grep -iE 'sent|remote_write|status='
# Expect: 200s, no 401s/403s.
```

On any workstation pointed at home Grafana, in Explore:

```promql
sp_rtk_base_relay_running{site="larson-base"}
# → 1 (or 0 if the relay engine is stopped)

rate(sp_rtk_base_input_bytes_received{site="larson-base"}[1m])
# → matches the Dashboard rate display on larson-base, modulo polling jitter

(time() - timestamp(sp_rtk_base_relay_uptime_seconds{site="larson-base"})) < 120
# → 1  (data is fresh; this is the "is the pipeline alive" indicator)
```

### Failure-recovery smoke (do BEFORE shipping the Pi remote)

1. Stop Traefik (or block port 443 at the home firewall) for 10 minutes.
2. On larson-base, watch the Alloy WAL grow:
   ```bash
   sudo du -sh /var/lib/alloy/data/
   ```
3. Restart Traefik.
4. In Grafana, verify back-filled samples appear for the outage window
   with timestamps inside the window (not all clumped at restore time).

If samples are missing or timestamps look wrong:

- Bump `max_keepalive_time` in the Alloy WAL block (default 24h here).
- Check `df /var/lib/alloy/data` — WAL can't grow into a full disk.

---

## Operational expectations once shipped

| Dashboard panel | Healthy value |
|---|---|
| Pipeline freshness | green (last sample < 2 min old) |
| Relay running | 1 |
| RTCM in | non-zero B/s while GPS is feeding |
| Seconds since last RTCM | < 10 s |
| Destination errors | 0 (or flat) |

When something goes wrong:

- **Pipeline freshness flips red** → larson-base or its internet link is
  down.  Same-day causes: power glitch, ISP outage, Pi unresponsive.
- **Relay running is 0 but freshness is green** → service crashed but
  the Pi is up.  SSH in via Raspberry Pi Connect; check
  `systemctl status sp-rtk-base`, `journalctl -u sp-rtk-base`.
- **Seconds since last RTCM climbing** → GPS receiver is connected but
  not sending data.  Likely a sky-view problem at the antenna; check
  satellite count in the Live Position card.
- **Destination errors > 0** → an output is failing (NTRIP caster
  unreachable, SurePath credential expired).  Inspect the per-destination
  metrics.

---

## See also

- The app's local Dashboard at `http://larson-base:8080/` already shows
  live rates, uptime, and destination state — this metrics push is for
  when you *can't* point a browser at it.
- Top-level deploy runbook: [`../deployment-pi.md`](../deployment-pi.md).
