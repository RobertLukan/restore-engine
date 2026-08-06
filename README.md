# Restore Engine

Proxmox Backup Server → Proxmox VE restore orchestrator (API/UI + worker + Redis queue).

Designed to mirror the operator experience of [migration-engine](../migration-engine): dark dashboard, multi-select backup table, bulk enqueue, and a restores progress tab.

## Features

- Configure **multiple PBS servers** and Proxmox VE in the UI
- List VM backups across every server, datastore, and namespace (each row shows its source)
- Select multiple backups at once (checkboxes + row click)
- Bulk restore with:
  - target node
  - target storage
  - sequential VMID allocation
  - optional live restore
  - optional per-batch bandwidth limit
- Track per-job progress and logs via Redis + worker
- **Recovery plans** (enterprise-style): saved inventory groups, recovery locations, ordered plan run with point-in-time

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

## Docker Compose

Full guide (Apple Silicon → amd64, LXC checklist, air-gap, Redis on host): **[DOCKER.md](DOCKER.md)**.

```bash
cp config.docker.example.yaml config.docker.yaml
# edit config.docker.yaml — keep redis.url host as "redis"
# uncomment volume mounts under api/worker in docker-compose.yml
docker compose up --build -d
```

Dashboard: http://localhost:8001 (migration-engine typically uses 8000 on the same host).

## Performance tuning

The **Settings** tab has a *Restore performance* section:

- **Max concurrent restores** (`worker.max_concurrent_restores`) — how many restores run in parallel. The worker re-reads this every second, so changes take effect without a restart. Increase only if PBS/PVE network and target storage have headroom; parallel restores to the *same* storage often contend on IOPS.
- **Default bandwidth limit** (`proxmox.restore_bwlimit`, KiB/s, `0` = unlimited) and **Live restore by default** (`proxmox.live_restore_default`) — these pre-fill the restore dialog.

Per restore batch, the **Restore selected** dialog also lets you set a **bandwidth limit** (passed to Proxmox as `bwlimit`) and toggle **live restore** (VM boots early and streams data in the background — best for fast time-to-usable; leave off for fastest total restore).

Note: Proxmox-side throttles (a `bwlimit` in `datacenter.cfg` or on the storage, and PBS Traffic Control rules) apply on top of these and are the most common cause of slow restores.

## Workflow

1. **Settings** — save and verify PBS + Proxmox connections.
2. **Groups / Locations / Plans** — define inventory groups (tags and/or VMIDs), a recovery location, and a plan that orders groups onto that location; **Run** with an optional point-in-time.
3. **Backups** — refresh list, select one or more VM backups, click **Restore selected** (ad-hoc).
4. **Restores** — watch job state and progress; stop queued jobs if needed.

## Archive path

Restores use the PVE PBS storage reference of the backup's own source:

`{mount.pve_storage}:backup/vm/{vmid}/{timestamp}`

Example: `pbs-main:backup/vm/100/2026-05-01T01:00:00Z`

Each PBS snapshot is identified by `vm/{vmid}/{timestamp}` (the backup time is
included so a specific snapshot is restored, and multiple snapshots of the same
VM are selectable independently).

## Development & tests

```bash
cd restore-engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The suite covers VMID allocation, the PVE archive volid format, PBS snapshot
parsing, auth/session enforcement, `/health` (ok/degraded), `/version`, and
recovery plan group/location CRUD plus ordered plan-run advancement. It
runs offline against `tests/fixtures/minimal_config.yaml` and does not require a
live PBS, Proxmox VE, or Redis.

## Related project

- [migration-engine](../migration-engine) — VMware → Proxmox migration orchestrator (UI pattern source)
