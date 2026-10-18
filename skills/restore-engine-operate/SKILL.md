---
name: restore-engine-operate
description: Operate restore-engine via HTTP API like the web UI (backups, restores, plans, cleanup, jobs). Use when restoring or managing plans from OpenClaw.
compatibility: Cursor Agent Skills + OpenClaw. Needs network to restore-engine URL; Bearer operator token.
metadata:
  openclaw:
    emoji: "🖥️"
    requires:
      bins: ["curl"]
      env: ["RESTORE_ENGINE_URL", "RESTORE_ENGINE_TOKEN"]
    primaryEnv: RESTORE_ENGINE_TOKEN
    envVars:
      - name: RESTORE_ENGINE_URL
        description: Base URL of restore-engine (e.g. http://127.0.0.1:8000 host-network, or http://localhost:8001 bridge)
      - name: RESTORE_ENGINE_TOKEN
        description: ui.api_tokens entry with role operator (or viewer for read-only)
---

# Operate restore-engine (API client)

Drive the **same** `/api/*` endpoints as the web GUI. Do **not** talk to PBS/PVE directly — restore-engine holds those credentials.

## Setup (once)

In restore-engine config (`config.docker.yaml` / `config.yaml`):

```yaml
ui:
  api_tokens:
    - name: openclaw
      token: "long-random-secret"
      role: operator   # viewer = GET only
  # Opt-in Cleanup tab + /api/cleanup/*
  plan_run_cleanup_enabled: false
```

Multi-PVE / multi-PBS are configured under Settings (or wizard): `pve_clusters[]`, `pbs_servers[].mounts[].pve_cluster_id` + `pve_storage`. Prefer the UI for credentials; agents usually only need the operate token.

On the OpenClaw / agent host:

```bash
# Host-network compose default:
export RESTORE_ENGINE_URL=http://127.0.0.1:8000
# Bridge overlay (docker-compose.bridge.yml) often uses:
# export RESTORE_ENGINE_URL=http://localhost:8001
export RESTORE_ENGINE_TOKEN=long-random-secret
```

Never put the token in chat logs, git, or this skill file.

## Call pattern

Prefer the helper (wraps auth + JSON):

```bash
{baseDir}/scripts/re-api.sh GET /api/backups
{baseDir}/scripts/re-api.sh GET /version
{baseDir}/scripts/re-api.sh POST /api/jobs/restore-selected '{"backup_ids":["…"],…}'
```

Or raw curl:

```bash
curl -sS -H "Authorization: Bearer $RESTORE_ENGINE_TOKEN" \
  -H "Content-Type: application/json" \
  "$RESTORE_ENGINE_URL/api/backups"
```

Auth: session cookie **or** `Authorization: Bearer <token>`. Viewer tokens cannot POST/PUT/DELETE (403).

## Safety defaults (match cautious GUI use)

| Action | Default |
| --- | --- |
| Restore mode | `normal` (new VMIDs) |
| Power-on / live / QGA | **off** unless user explicitly asks |
| Network | `network_mode=none` |
| Overwrite / DR reclaim | **off**; require explicit confirm |
| `allow_non_isolated` | **false** unless user insists |
| Batch cleanup | only if `ui.plan_run_cleanup_enabled` and user confirms destroy |

**Before any mutating call**, summarize the plan to the user and wait for confirmation when:

- `restore_mode=dr` or `overwrite=true` / `confirm_overwrite`
- `power_on`, `live_restore`, `qga_wait_sec>0`, or `allow_non_isolated`
- Plan run on a DR location (`confirm_dr`)
- Bulk restore (>5 VMs) or purge finished jobs
- Cleanup execute (permanent destroy of batch-tagged guests)

HTTP **409** on plan run = another plan is already RUNNING — do not retry blindly; list `GET /api/plan-runs` and wait/cancel.

HTTP **403** on `/api/cleanup/*` = cleanup disabled in Settings — tell the user to enable **Plan run cleanup**.

## Workflows

### 0. Smoke check

```bash
{baseDir}/scripts/re-api.sh GET /version
{baseDir}/scripts/re-api.sh GET /api/jobs/stats
{baseDir}/scripts/re-api.sh GET /api/restore-defaults
{baseDir}/scripts/re-api.sh GET /api/locations
```

Use `restore-defaults` for `next_free_proxmox_vmid`, default node/storage hints. Pass `?proxmox_node=` / cluster context as the UI does when multi-PVE.

### 1. List backups (Backups tab)

```bash
{baseDir}/scripts/re-api.sh GET '/api/backups?limit=50&offset=0'
```

Each row has `backup_id` like `sourceId|vm/100/2026-…Z`, plus `vmid`, `name`, `timestamp`, `pve_storage`, `size_bytes`, and cluster/source fields. Omit `limit` to fetch all (can be large).

Optional tags (like UI **Load tags**):

```bash
{baseDir}/scripts/re-api.sh POST /api/backups/resolve-tags \
  '{"backup_ids":["id1","id2"],"proxmox_node":"pve1"}'
```

### 2. Enqueue selected restores (same as UI Restore)

1. `GET /api/restore-defaults`
2. Pick `backup_ids` (show names/VMIDs to user; confirm)
3. `POST /api/jobs/restore-selected`:

```json
{
  "backup_ids": ["main|vm/100/2026-05-01T01:00:00Z"],
  "proxmox_nodes": ["pve1"],
  "proxmox_node": "pve1",
  "proxmox_storage": "local-lvm",
  "proxmox_storage_by_node": {"pve1": "local-lvm"},
  "proxmox_vmid_start": 200,
  "pve_cluster_id": "default",
  "live_restore": false,
  "bwlimit": 0,
  "restore_mode": "normal",
  "power_on": false,
  "qga_wait_sec": 0,
  "network_mode": "none",
  "lab_bridge": "",
  "overwrite": false,
  "confirm_overwrite": false,
  "allow_non_isolated": false,
  "http_check_url": "",
  "add_to_ha": false,
  "ha_group": ""
}
```

Storage: every selected node needs an entry in `proxmox_storage_by_node` **or** a non-empty `proxmox_storage` default.

Power-on / live / QGA require `network_mode` `unlink` or `remap` (plus `lab_bridge` for remap), or `allow_non_isolated=true`.

DR overwrite: `restore_mode=dr`, `overwrite=true`, `confirm_overwrite=true` — only reclaim guests previously owned by restore-engine.

Enqueue order within the batch is **largest-first** by default (`worker.enqueue_largest_first`).

### 3. Restore by tag group

```bash
{baseDir}/scripts/re-api.sh POST /api/jobs/restore-tag-group \
  '{"tag":"app-x","at_or_before":null,"proxmox_nodes":["pve1"],"proxmox_node":"pve1","proxmox_storage":"local-lvm","proxmox_storage_by_node":{"pve1":"local-lvm"},"proxmox_vmid_start":200,"pve_cluster_id":"default","restore_mode":"normal","power_on":false,"network_mode":"none"}'
```

### 4. Plans (Run / Drill / Assure)

```bash
{baseDir}/scripts/re-api.sh GET /api/plans
{baseDir}/scripts/re-api.sh GET /api/locations
{baseDir}/scripts/re-api.sh POST /api/plans/{plan_id}/check '{}'
{baseDir}/scripts/re-api.sh POST /api/plans/{plan_id}/run \
  '{"drill":true,"auto_teardown":false,"power_on":false,"allow_unverified":false,"confirm_dr":false,"overwrite":false}'
```

Plans may set `add_to_ha` / `ha_group` (CRUD on `/api/plans`). Location chooses `pve_cluster_id`, isolation, storage/nodes.

- Prefer **drill** (`drill:true`) for tests; keep powered off unless asked.
- Production Run may need plan VERIFIED or `allow_unverified` (only if user accepts).
- DR location → `confirm_dr:true` after explicit user OK.
- One RUNNING plan at a time (409 if busy).

Poll: `GET /api/plan-runs/{run_id}` — note `batch_id` on the run.  
Cancel: `POST /api/plan-runs/{run_id}/cancel`.  
Teardown drill guests: `POST /api/plan-runs/{run_id}/teardown`.

### 5. Cleanup tab (batch destroy)

Requires `ui.plan_run_cleanup_enabled: true`. Same dual-tag gate as teardown (`restore-engine` + `re-<batch_id>`).

```bash
# Preview (no destroy)
{baseDir}/scripts/re-api.sh POST /api/cleanup/preview \
  '{"batch_id":"<12hex or re-12hex>","pve_cluster_id":"default","plan_run_id":""}'

# Execute — CONFIRM with user first (permanent)
{baseDir}/scripts/re-api.sh POST /api/cleanup/execute \
  '{"batch_id":"<batch>","pve_cluster_id":"default","plan_run_id":""}'

# Poll until status completed/failed
{baseDir}/scripts/re-api.sh GET /api/cleanup/{batch_id}
```

`batch_id` comes from the plan run (`batch_id` field) or Cleanup UI. Large disks can take a long time; destroy waits for PVE reclaim.

### 6. Jobs / Restores tab

```bash
{baseDir}/scripts/re-api.sh GET '/api/jobs?limit=50&offset=0'
{baseDir}/scripts/re-api.sh GET /api/jobs/{job_id}
{baseDir}/scripts/re-api.sh GET /api/jobs/{job_id}/log
{baseDir}/scripts/re-api.sh POST /api/jobs/{job_id}/stop '{}'
{baseDir}/scripts/re-api.sh POST /api/jobs/purge '{}'
```

Jobs are newest-first. Purge removes **finished** only — confirm with user first.

Queue: `POST /api/jobs/queue/pause|resume|stop`.

### 7. Interpret outcomes

- Job `COMPLETED` with log lines about `WARNINGS:` → **success** (soft warning), not failure.
- Real PVE errors → `FAILED` with `error` field.
- See skill `restore-engine-domain` for enqueue order / plan lock / batch cleanup invariants.

## Related

- Domain rules: `restore-engine-domain`
- Offline package: `restore-engine-offline-bundle`
- Endpoint cheat sheet: [reference.md](reference.md)
