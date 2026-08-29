# Restore Engine

[![CI](https://github.com/RobertLukan/restore-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/RobertLukan/restore-engine/actions/workflows/ci.yml)

Proxmox Backup Server → Proxmox VE restore orchestrator (API/UI + worker + Redis queue).

Dark dashboard for operators: multi-select backup table, bulk enqueue, recovery plans, drills, and a restores progress tab. UI patterns are similar to the separate [migration-engine](https://github.com/RobertLukan/migration-engine) project (VMware → Proxmox).

## Features

- Configure **multiple PBS servers** and Proxmox VE in the UI
- List VM backups across every server, datastore, and namespace (each row shows its source)
- Select multiple backups at once (checkboxes + row click)
- Bulk restore with load-balanced nodes, per-node storage, VMID allocation, live restore, bandwidth limit
- **Recovery plans**: groups, locations, ordered runs, readiness check + VERIFIED gate, compliance reports (RTO)
- **Drills**: powered-off restore, optional auto-teardown, optional schedule (interval hours)
- **Assurance**: policy on plans (require QGA/HTTP, max RTO); drill outcomes set ASSURED/FAILED; dashboard + Assure now
- **Compliance**: read-only posture rollup (readiness + assurance + schedule + evidence links)
- **Audit**: browser for recent operator actions (also `GET /api/audit`)
- **Power-on + QGA** and optional **HTTP check** after guest agent is up; guest hostname vs PVE name is a warning on mismatch
- **Network isolation**: unlink NICs or remap to a lab bridge before power-on (gated unless location is isolated)
- **DR overwrite**: destroy existing VMID then restore (explicit confirm; owned guests only)
- **Email / webhook notifications** on readiness fail, plan-run terminal, optional ad-hoc job fail
- Redis-backed **concurrency slots**, job **TTL**, **audit log**, optional **API tokens** (operator/viewer)
- Track per-job progress and logs via Redis + worker
- Ownership stamp after restore (required for reclaim/teardown); stamp failure fails the job

## Multiple PBS servers, datastores, and namespaces

Sources are configured as a list under `pbs_servers`. Each server holds its
connection details plus one or more `mounts`, where a mount ties a
`(datastore, namespace)` to the **PVE storage ID** that references it:

```yaml
pbs_servers:
  - id: main
    host: 10.0.0.10
    port: 8007
    verify_ssl: false
    api_token_id: root@pam!restore
    api_token_secret: SECRET
    mounts:
      - datastore: main
        namespace: ""            # root namespace
        pve_storage: pbs-main
      - datastore: main
        namespace: team-a
        pve_storage: pbs-main-teamA
```

This mirrors how Proxmox works: **PVE binds one datastore and one namespace per
storage definition**, so each `(datastore, namespace)` you want to restore from
needs its own PVE storage entry, and that storage ID is what goes in `pve_storage`.
The backup list aggregates all sources and the restore uses the correct
`pve_storage` per selected backup automatically. A legacy single `pbs:` block plus
`proxmox.pbs_storage` is still read and shown as one server.

PBS and Proxmox VE auth: set **either** an API token (`api_token_id` +
`api_token_secret`) **or** `user` + `password`. When both are present, the token
is used.

## Restore by tags (groups)

VM tags are stored inside each backup's guest config (not in the PBS snapshot
list), so the app reads them via the PVE `vzdump/extractconfig` API (which also
works for encrypted datastores) and caches them per snapshot in Redis.

- On the **Backups** tab, click **Load tags** to populate the Tags column, then
  filter by tag and use **Select filtered** to bulk-select a group.
- **Restore by tag group** restores the newest backup per VM whose config carries
  a chosen tag, as of an optional point in time (blank = now). Internally it takes
  the latest snapshot per VMID at or before the cutoff and keeps those whose config
  includes the tag.

Because tags require one `extractconfig` call per inspected snapshot, they are
resolved on demand and cached (snapshots are immutable, so the cache is durable).

## Prerequisites

1. One or more **Proxmox Backup Servers** with API tokens that can read the datastores/namespaces you want to list.
2. **Proxmox VE** with each PBS datastore/namespace added as a storage backend; put that storage ID in the matching `mounts[].pve_storage`.
3. API token on PVE with permission to create VMs and restore from those PBS storages.

## Quick start (local)

```bash
cd restore-engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Edit config.yaml with your PBS + PVE endpoints and tokens
redis-server   # or use Docker redis only

# Terminal 1
uvicorn main:app --host 0.0.0.0 --port 8001

# Terminal 2
python worker.py
```

Open http://localhost:8001 and sign in with `ui.password` from `config.yaml`.

## Ports

| Port | Service |
|------|---------|
| **8001** | restore-engine UI (local dev and Docker Compose default) |
| **8006** | Proxmox VE API (`proxmox.port` in config) |
| **8007** | Proxmox Backup Server API (`pbs_servers[].port` in config) |

Do not publish restore-engine on **8007** in public docs — that is PBS’s usual API port. You may remap the Compose host port locally (see [DOCKER.md](DOCKER.md)).

## Docker Compose

Full guide (Apple Silicon → amd64, LXC checklist, air-gap, Redis on host): **[DOCKER.md](DOCKER.md)**.

```bash
cp config.docker.example.yaml config.docker.yaml
# edit config.docker.yaml — keep redis.url host as "redis"
# uncomment volume mounts under api/worker in docker-compose.yml
docker compose up --build -d
```

Dashboard: http://localhost:8001

## Performance tuning

The **Settings** tab has a *Restore performance* section; the **Restores** tab mirrors concurrency live:

- **Max concurrent restores** — change anytime (including mid-batch). The worker re-reads this every second; in-flight jobs are not killed when you lower the limit.
- **Pause / Resume** — pause stops claiming new jobs and plan group enqueue; running restores finish or fail on their own. Resume continues the queue.
- **Stop pending** — pause + cancel all PENDING jobs; in-flight keeps going (use per-job Stop to abort a running restore).
- Per-job **progress % / speed / ETA** from PVE task logs (best-effort). **Speed (gross)** follows PVE progress and includes sparse/zero regions. **Est. network** scales the PBS snapshot size (sum of archive sizes) by restore progress — an approximation of wire throughput, not a measured NIC counter.
- Backup list shows **Size (gross)** from PBS; use **Estimate sizes** for an on-demand **non-zero** logical estimate from `.fidx` (better proxy for restore effort — zeros are fast). See [docs/dr-overview.md](docs/dr-overview.md).
- **Default bandwidth limit** and **Live restore by default** in Settings pre-fill restore dialogs.

Per restore batch, the **Restore selected** dialog also lets you set a **bandwidth limit** (passed to Proxmox as `bwlimit`), toggle **live restore**, and **multi-select Proxmox nodes** with a **storage dropdown per node**. Jobs load-balance across nodes; VMIDs are cluster-wide unique.

Note: Proxmox-side throttles (a `bwlimit` in `datacenter.cfg` or on the storage, and PBS Traffic Control rules) apply on top of these and are the most common cause of slow restores.

## Workflow

1. **Settings** — save and verify PBS + Proxmox connections; optional SMTP/webhook notifications and API tokens in config.
2. **Groups / Locations / Plans** — define inventory groups, a recovery location (including network isolation / HTTP check), and a plan; optional schedule for automatic drills.
3. **Check** a plan (readiness), then **Run** or **Drill** with point-in-time; download reports from the Plans tab.
4. **Assurance** — set policy, **Assure now**, watch ASSURED/FAILED (does not perform production failback).
5. **Compliance** — read-only posture across plans (readiness, assurance, schedule, report links).
6. **Backups** — refresh list, optionally **Estimate sizes** (non-zero), select backups, **Restore selected** (ad-hoc) with isolation / overwrite options.
7. **Restores** — watch job state and progress; stop queued jobs if needed.
8. **Infra** — Grafana embed (optional) and/or live PVE/PBS CPU/RAM snapshot.
9. **Audit** — browse recent operator actions when investigating who changed what.

DR workflow and safety defaults: **[docs/dr-overview.md](docs/dr-overview.md)** (generic). Site-specific topology belongs in your internal runbooks.

Infrastructure metrics (Grafana, Netdata, PVE/PBS OpenTelemetry): **[docs/infra-metrics.md](docs/infra-metrics.md)**.

## Production checklist

Lab defaults favor convenience. For anything beyond a closed lab:

1. **TLS** — put a reverse proxy (Caddy/nginx) in front of `:8001` with HTTPS and secure cookies; do not expose plain HTTP on a shared network.
2. **`ui.password` / `ui.session_secret`** — strong unique values; the API **refuses to start** if a password is set but the session secret is missing, shorter than 32 characters, or the dev placeholder.
3. **`worker.require_verified_to_run: true`** (or `plans.require_verified_to_run`) — block production Runs until readiness is VERIFIED (drills/Assure can still use `allow_unverified` where intended).
4. **Secrets** — keep `config.docker.yaml` out of git; prefer API tokens over passwords; treat SMTP password and PBS/PVE tokens as sensitive.
5. **Redis** — Compose Redis is unauthenticated on the internal Docker network only; do not publish Redis ports to the host in production.
6. **Single worker (or documented scale)** — concurrency slots are Redis-backed; still prefer one worker replica unless you understand shared-queue behavior (`DOCKER.md`).
7. **Isolation** — power-on / QGA / HTTP only with unlink/remap or an isolated location; avoid `allow_non_isolated` on shared L2.
8. **Notifications** — enable SMTP or webhook so failed readiness/drills are not UI-only.

See also **Safety notes** below and **[DOCKER.md](DOCKER.md)**.

## Safety notes (power-on)

Power-on, live-restore, and QGA require **network isolation** (`unlink` / `remap`), an **isolated** location flag, or an explicit **allow non-isolated** override. On a shared production L2, prefer powered-off drills.

## API tokens

In `config.yaml` / `config.docker.yaml`:

```yaml
ui:
  password: "..."
  session_secret: "..."
  api_tokens:
    - name: automation
      token: "long-random-secret"
      role: operator   # or viewer (GET only)
```

Send `Authorization: Bearer <token>` instead of a session cookie. Operator actions are recorded in the Redis audit log (`GET /api/audit` and the **Audit** tab).

## Notifications

Under `notifications:` configure SMTP and/or a webhook URL. Events (opt-in): readiness check failed, plan/drill finished, optional ad-hoc job failed. Send is best-effort and never fails a restore. Settings UI can send a test email.

## Archive path

Restores use the PVE PBS storage reference of the backup's own source:

`{mount.pve_storage}:backup/vm/{vmid}/{timestamp}`

Example: `pbs-main:backup/vm/100/2026-05-01T01:00:00Z`

Each PBS snapshot is identified by `vm/{vmid}/{timestamp}` (the backup time is
included so a specific snapshot is restored, and multiple snapshots of the same
VM are selectable independently).

## PCI passthrough and API user privileges

Guest tags and most restores work with a normal Proxmox API user (for example
`flaskapp@pve`). Backups that include **PCI passthrough** (`hostpci*`) are
different: Proxmox only allows **root** to set `hostpci` config for non-mapped
devices. Restoring such a backup with a non-root token/user fails with:

```text
only root can set 'hostpci0' config for non-mapped devices
```

Options:

- Restore with `root@pam` (password or API token) when the backup has passthrough, or
- Remove / avoid `hostpci*` on the source VM before backup (or use mapped PCI
  devices that non-root may manage), or
- Use a drill/clone without passthrough for recovery tests.

The worker surfaces that Proxmox message on the failed job; it is not a PBS or
tag-matching problem.

## Development & tests

```bash
cd restore-engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
# optional coverage:
pytest --cov=. --cov-report=term-missing --cov-fail-under=0
```

The suite is **offline** (FakeRedis + mocks) against `tests/fixtures/minimal_config.yaml` — no live PBS, Proxmox VE, or Redis required. It covers VMID allocation, archive volids, PBS parsing, auth, health/version, plans/jobs domain logic, worker `process_job` (mocked), hygiene/audit, and selected HTTP routes.

**Feature vs test gaps:** see **[docs/gap-analysis.md](docs/gap-analysis.md)**.

Contributing and security: **[CONTRIBUTING.md](CONTRIBUTING.md)**, **[SECURITY.md](SECURITY.md)**.

## Related project

- [migration-engine](https://github.com/RobertLukan/migration-engine) — VMware → Proxmox migration orchestrator
