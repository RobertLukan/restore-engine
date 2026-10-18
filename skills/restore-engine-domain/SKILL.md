---
name: restore-engine-domain
description: restore-engine domain rules (PBS→PVE): multi-PVE, LJF enqueue, one RUNNING plan, batch cleanup, HA, WARNINGS=success, jobs purge, safety/secrets. Use when editing jobs/plans/worker/UI.
compatibility: Cursor Agent Skills + OpenClaw (AgentSkills SKILL.md). Offline-safe; no network needed to apply.
metadata:
  openclaw:
    emoji: "💾"
    always: true
---

# restore-engine domain rules

PBS → PVE restore orchestrator: FastAPI UI/API + Redis queue + worker. Apply these invariants when editing or reviewing this repo.

## Architecture (do not invent a second queue)

| Piece | Role |
| --- | --- |
| `main.py` | HTTP API + static UI |
| `ui.py` | Settings / credentials (multi-PBS, multi-PVE), session, `plan_run_cleanup_enabled` |
| `worker.py` | Dequeues jobs; drives PVE restore/start/HA |
| Redis | Job keys, FIFO queue, concurrency slots, plans/audit/cleanup progress |
| `clusters.py` | Load/resolve `pve_clusters[]`; default cluster id |
| `pve_client.py` | Proxmox task poll / soft-success / batch tags / destroy wait |
| `jobs.py` | Enqueue, VMID allocation, largest-first sort, ownership stamp |
| `plans.py` | Plans, readiness, drills, assurance, batch_id, cleanup, one RUNNING lock |
| `job_hygiene.py` | List/paginate/purge terminal jobs |

**Default lab UI (host-network compose):** `http://127.0.0.1:8000`. Redis on host: `redis://127.0.0.1:6380/0`. Bridge overlay still publishes UI on **8001** (`docker-compose.bridge.yml`).

## Hard invariants

### 1. Largest-first enqueue (default on)

- Config: `worker.enqueue_largest_first` (default **true**).
- Within **one batch** (UI restore-selected / tag-group, or **one plan group**), sort by `nonzero_bytes` else PBS `size_bytes`, largest first.
- Plan **groups** stay sequential barriers — do not parallelize groups.
- Sequential VMIDs from `vmid_start` follow that size order within the batch.
- Legacy call/VMID order: set `enqueue_largest_first: false`.
- Code: `jobs.sort_rows_largest_first` / `enqueue_restores`.

### 2. One plan run at a time

- Starting Run / Drill / Assure while **any** plan is `RUNNING` → **HTTP 409**.
- Do not remove or weaken this lock without an explicit product decision.
- Code: `plans.py` busy check; API maps to 409 in `main.py`.

### 3. Multi-PVE clusters

- Config: `pve_clusters[]` (id, host, tokens, default_node, storage, …). Legacy single `proxmox:` block still maps to cluster `default`.
- PBS mounts carry `pve_cluster_id` + `pve_storage` (which PVE datastore name mounts that PBS).
- Locations and jobs carry `pve_cluster_id`; restores/cleanup target that cluster only.
- Code: `clusters.py`, `ui.py` credentials persist, `sources.py` / wizard.

### 4. Plan batch tags and cleanup

- Each plan run gets a `batch_id` (12 hex) → guest tag `re-<batch_id>` plus ownership tag `restore-engine`.
- Teardown / Cleanup destroy only guests that pass the dual-tag (or provenance) gate for that batch.
- Cleanup UI/API is **opt-in**: `ui.plan_run_cleanup_enabled` (default **false**). When off → `/api/cleanup/*` returns 403.
- Destroy waits for PVE task completion (`worker.destroy_task_timeout_sec`, `0` = unlimited) so large disks finish reclaim.
- Code: `pve_client.batch_tag_for_id`, `plans.preview_cleanup_by_batch` / `execute_cleanup_by_batch`, Cleanup tab in `static/index.html`.

### 5. PVE `WARNINGS: N` is success

- `pve_exitstatus_is_success`: `OK` and `WARNINGS: N` (any N) → success.
- Real errors → fail the job. Soft warnings (e.g. OVMF / no efidisk) must surface in logs, not mark FAILED.
- Worker records soft warnings via helper; do not treat warning text as hard failure elsewhere.

### 6. Jobs UI / API listing

- Jobs list: **newest first**.
- Support pagination beyond a hard 200 cap (`Load more` / offset+limit).
- **Clear finished** → purge terminal jobs only; keep queued/running/active.
- Code: `job_hygiene.collect_jobs`, `paginate`, `purge_terminal_jobs`.

### 7. Ownership, reclaim, HA

- After restore, ownership stamp is required for reclaim/teardown. Stamp failure → job FAILED.
- Plan flag `add_to_ha` registers each restored QEMU with PVE HA after stamp/power policy (`started` if powered on, else `stopped`). Optional `ha_group`.
- Teardown / cleanup removes HA first when the run had `add_to_ha`.

### 8. Safety gates (power-on / live / QGA)

- Power-on, live-restore, QGA, HTTP check require network isolation (`unlink` / `remap`), an **isolated** location, or explicit `allow_non_isolated`.
- Prefer powered-off drills on shared production L2.
- Production: `worker.require_verified_to_run: true` (or `plans.require_verified_to_run`) so Runs need readiness VERIFIED (drills/Assure may use `allow_unverified` where intended).

### 9. Secrets never in git

- Never commit `config.yaml` / `config.docker.yaml` with real tokens.
- Image ships `config.docker.example.yaml` only (`.dockerignore` excludes real config).
- If `ui.password` is set: `ui.session_secret` required, ≥32 chars, not the dev placeholder — API refuses to start otherwise.
- Prefer API tokens over passwords; do not publish Redis beyond localhost in production.
- Ignore local lab files such as `.pbs-proxy-target`.

## Config touchpoints

- Multi-PBS: `pbs_servers[].mounts[]` with `datastore` / `namespace` / `pve_cluster_id` / `pve_storage`.
- Multi-PVE: `pve_clusters[]`.
- Archive path: `{pve_storage}:backup/vm/{vmid}/{timestamp}`.
- Tags live in guest config via PVE `extractconfig` (cached in Redis) — not in PBS snapshot list alone.
- PCI passthrough (`hostpci*`) often needs `root@pam`; non-root fails with Proxmox's hostpci message — not a PBS/tag bug.

## When changing behavior

1. Prefer extending existing helpers over new parallel paths.
2. Add/adjust unit tests under `tests/` for enqueue order, wait_for_task warnings, plan busy, jobs list/purge, clusters, HA, plan-run cleanup.
3. Keep UI copy in `static/index.html` aligned (Restores, Cleanup, Settings, plan-busy errors).
4. Update `README.md` / `DOCKER.md` and skills when operator-visible behavior changes.

## Related skills

- Operate via HTTP API (like the web UI): `restore-engine-operate`
- Offline / air-gap package and lab deploy: `restore-engine-offline-bundle`

## Deeper reference

- Invariants detail and file map: [reference.md](reference.md)
