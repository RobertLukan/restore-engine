# Infrastructure metrics (Grafana + Netdata + PVE/PBS Metric Server)

restore-engine can embed **Grafana** for rich charts and always offers a lightweight **API snapshot** (CPU/RAM from PVE/PBS) on the **Infra** tab.

## Design rules

- **No node_exporter / Telegraf** on PVE or PBS.
- Use **PVE/PBS built-in** Metric Server (OpenTelemetry preferred; InfluxDB/Graphite also fine).
- **Netdata** for per-interface NIC charts on PVE:
  - **Lab:** install with [`deploy/observability/install-netdata-pve.sh`](../deploy/observability/install-netdata-pve.sh).
  - **Production:** optional (often already present); dashboards show “No data” for Netdata panels if absent.

## Reading bottlenecks (what to watch when restores are slow)

Open Grafana dashboard **Restore infra** during a restore. Set **Backup/PBS NIC**, **Storage/Ceph NIC**, and **link Gbit** to match the host (lab often `10`; DR often `40`).

| Signal (red / high) | Likely bound by | What to try |
|---------------------|-----------------|-------------|
| **CPU busy** ≥ ~85% | PVE CPU (decompress / qemu restore) | Lower `max_concurrent_restores` on that node; spread jobs across nodes |
| **IO wait** high, NICs modest | Storage / Ceph latency | Check Ceph health, fewer heavy VMs/node, interim ZFS/size=2. Panel uses `proxmox_node_cpustat_wait_seconds_total` (same as PVE Summary IO delay)—not `rate(iowait)/cpus`, which overstates wait because OTel iowait counters are jiffy-scaled. |
| **RAM used** ≥ ~92% | Memory pressure | Lower concurrency |
| **Backup NIC util** ≥ ~85% | PBS→PVE link (or PBS read feeding a full link) | Confirm no bwlimit; check PBS disk/CPU; don’t expect > link rate |
| **Storage NIC util** ≥ ~85% | Ceph/replication or storage uplink | Expected with size=3 write amp; separate PBS vs cluster NICs |
| Load1 ≫ CPU count | Runnable backlog | Same as CPU-bound |

Gauges answer “where is the ceiling?”; absolute Bps charts confirm which direction (RX vs TX).

## Quick start (lab)

### 1. Observability Compose profile

On the restore-engine host:

```bash
cd /opt/restore-engine   # or your checkout
export GRAFANA_ADMIN_PASSWORD='set-a-real-password'
docker compose --profile observability up -d
```

Services:

| Service | Port | Role |
|---------|------|------|
| Grafana | **3001** | Dashboards (anonymous Viewer enabled for iframe embed) |
| Prometheus | 9090 | Scrapes Netdata + OTel Collector |
| OTel Collector | **4317/4318** | Receives PVE/PBS OpenTelemetry metrics |

Core api/worker/redis are unchanged without the profile.

### 2. Install Netdata on lab PVE nodes

On each **lab** PVE node (as root):

```bash
# copy script from the restore-engine repo, then:
ALLOW_FROM=<prometheus-or-compose-host-ip> bash install-netdata-pve.sh
```

Edit [`deploy/observability/prometheus.yml`](../deploy/observability/prometheus.yml) `netdata-pve` `static_configs` (see `prometheus.targets.example.yml`), then reload Prometheus:

```bash
curl -X POST http://127.0.0.1:9090/-/reload
# or: docker compose --profile observability up -d prometheus
```

### 3. Point PVE (and PBS) Metric Server at OTel

In PVE UI: **Datacenter → Metric Server → Add → OpenTelemetry**

- Server: IP/DNS of the host running Compose  
- Port: `4318` (HTTP) or `4317` (gRPC)  
- Path: `/v1/metrics` (HTTP)  
- Protocol: http or https as appropriate for lab  

PBS: equivalent Metric Server / OpenTelemetry settings to the same collector.

### 4. Wire the Infra tab

In `config.docker.yaml`:

```yaml
monitoring:
  enabled: true
  api_snapshot: true
  grafana:
    base_url: "http://<lab-host>:3001"   # must be reachable from your browser
    dashboards:
      - id: infra
        title: "Infra"
        uid: "restore-infra"
        path: "/d/restore-infra/restore-infra?orgId=1&kiosk&theme=dark"
```

Recreate/restart **api** so config is picked up. Open **Infra** in the UI.

Set Grafana dashboard variables **Backup/PBS iface** and **Ceph/storage iface** to match lab bridges/bonds (e.g. `vmbr0`, `bond0`).

## Production / DR

1. Enable the same Compose profile on the DR restore-engine host (or point `grafana.base_url` at an existing Grafana).
2. **Netdata optional** — if nodes already have it, add scrape targets; if not, rely on Metric Server OTel + Infra API snapshot.
3. Change Prometheus targets and Grafana iface variables for DR NIC layout (see [dr-architecture.md](dr-architecture.md)).
4. Tighten Grafana later (disable anonymous, reverse proxy, SSO) — lab defaults favor embed convenience.

## API

- `GET /api/infra/metrics` — snapshot + monitoring/grafana embed config  
- `GET /api/infra/monitoring` — config only  

## Troubleshooting

- **Empty Grafana charts:** Prometheus targets, Netdata `:19999` reachability from the Prometheus container, Metric Server OTel to `:4318`.
- **Iframe blank:** `monitoring.grafana.base_url` must be a **browser** URL (not `http://grafana:3000`). Grafana has `GF_SECURITY_ALLOW_EMBEDDING=true`.
- **Netdata metric names differ by version:** adjust PromQL in the provisioned dashboard under `deploy/observability/grafana/dashboards/`.
