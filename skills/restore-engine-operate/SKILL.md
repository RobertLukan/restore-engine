---
name: restore-engine-operate
description: Operate restore-engine via HTTP API like the web UI (backups, restores, plans, jobs). Use when restoring or managing plans from OpenClaw.
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
        description: Base URL of restore-engine (e.g. http://localhost:8001)
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
```

On the OpenClaw / agent host:

```bash
export RESTORE_ENGINE_URL=http://localhost:8001   # no trailing slash
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

**Before any mutating call**, summarize the plan to the user and wait for confirmation when:

- `restore_mode=dr` or `overwrite=true` / `confirm_overwrite`
- `power_on`, `live_restore`, `qga_wait_sec>0`, or `allow_non_isolated`
- Plan run on a DR location (`confirm_dr`)
- Bulk restore (>5 VMs) or purge finished jobs

HTTP **409** on plan run = another plan is already RUNNING — do not retry blindly; list `GET /api/plan-runs` and wait/cancel.

## Workflows

### 0. Smoke check

```bash
{baseDir}/scripts/re-api.sh GET /version
{baseDir}/scripts/re-api.sh GET /api/jobs/stats
{baseDir}/scripts/re-api.sh GET /api/restore-defaults
```

Use `restore-defaults` for `next_free_proxmox_vmid`, default node/storage hints.

### 1. List backups (Backups tab)

```bash
{baseDir}/scripts/re-api.sh GET '/api/backups?limit=50&offset=0'
```

Each row has `backup_id` like `sourceId|vm/100/2026-…Z`, plus `vmid`, `name`, `timestamp`, `pve_storage`, `size_bytes`. Omit `limit` to fetch all (can be large).

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
  "http_check_url": ""
}
```

Storage: every selected node needs an entry in `proxmox_storage_by_node` **or** a non-empty `proxmox_storage` default.

Power-on / live / QGA require `network_mode` `unlink` or `remap` (plus `lab_bridge` for remap), or `allow_non_isolated=true`.

DR overwrite: `restore_mode=dr`, `overwrite=true`, `confirm_overwrite=true` — only reclaim guests previously owned by restore-engine.

### 3. Restore by tag group

```bash
{baseDir}/scripts/re-api.sh POST /api/jobs/restore-tag-group \
  '{"tag":"app-x","at_or_before":null,"proxmox_nodes":["pve1"],"proxmox_node":"pve1","proxmox_storage":"local-lvm","proxmox_storage_by_node":{"pve1":"local-lvm"},"proxmox_vmid_start":200,"restore_mode":"normal","power_on":false,"network_mode":"none"}'
```

### 4. Plans (Run / Drill / Assure)

```bash
{baseDir}/scripts/re-api.sh GET /api/plans
{baseDir}/scripts/re-api.sh GET /api/locations
{baseDir}/scripts/re-api.sh POST /api/plans/{plan_id}/check '{}'
{baseDir}/scripts/re-api.sh POST /api/plans/{plan_id}/run \
  '{"drill":true,"auto_teardown":false,"power_on":false,"allow_unverified":false,"confirm_dr":false,"overwrite":false}'
```

- Prefer **drill** (`drill:true`) for tests; keep powered off unless asked.
- Production Run may need plan VERIFIED or `allow_unverified` (only if user accepts).
- DR location → `confirm_dr:true` after explicit user OK.
- One RUNNING plan at a time (409 if busy).

Poll: `GET /api/plan-runs/{run_id}` · Cancel: `POST /api/plan-runs/{run_id}/cancel`.

### 5. Jobs / Restores tab

```bash
{baseDir}/scripts/re-api.sh GET '/api/jobs?limit=50&offset=0'
{baseDir}/scripts/re-api.sh GET /api/jobs/{job_id}
{baseDir}/scripts/re-api.sh GET /api/jobs/{job_id}/log
{baseDir}/scripts/re-api.sh POST /api/jobs/{job_id}/stop '{}'
{baseDir}/scripts/re-api.sh POST /api/jobs/purge '{}'
```

Jobs are newest-first. Purge removes **finished** only — confirm with user first.

Queue: `POST /api/jobs/queue/pause|resume|stop`.

### 6. Interpret outcomes

- Job `COMPLETED` with log lines about `WARNINGS:` → **success** (soft warning), not failure.
- Real PVE errors → `FAILED` with `error` field.
- See skill `restore-engine-domain` for enqueue order / plan lock invariants.

## Related

- Domain rules: `restore-engine-domain`
- Offline package: `restore-engine-offline-bundle`
- Endpoint cheat sheet: [reference.md](reference.md)
