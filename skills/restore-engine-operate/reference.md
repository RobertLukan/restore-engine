# Operate API cheat sheet

Base: `$RESTORE_ENGINE_URL` · Auth: `Authorization: Bearer $RESTORE_ENGINE_TOKEN`

Prefix for authenticated JSON API: `/api` (except `/version`, `/health`).

Default URL: host-network lab `http://127.0.0.1:8000`; bridge overlay often `http://localhost:8001`.

## Read

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/version` | Build/version (no auth required on most installs) |
| GET | `/api/backups` | Optional `?offset=&limit=` |
| GET | `/api/restore-defaults` | Optional `?proxmox_node=` |
| GET | `/api/jobs` | `?state=&offset=&limit=` → `{items,total,…}` |
| GET | `/api/jobs/{id}` | Job record (may include `batch_id`, `pve_cluster_id`) |
| GET | `/api/jobs/{id}/log` | Log lines |
| GET | `/api/jobs/stats` | Queue / concurrency |
| GET | `/api/plans` | Plans (`add_to_ha`, assurance fields, …) |
| GET | `/api/plans/{id}` | One plan |
| GET | `/api/groups` | Groups |
| GET | `/api/locations` | Locations (`pve_cluster_id`, isolation, …) |
| GET | `/api/plan-runs` | Optional `?plan_id=&limit=` — runs include `batch_id` |
| GET | `/api/plan-runs/{id}` | Aggregated run |
| GET | `/api/cleanup/{batch_id}` | Cleanup progress (needs cleanup enabled) |
| GET | `/api/reports` | Compliance reports |
| GET | `/api/assurance/dashboard` | Assurance rollup |
| GET | `/api/compliance/dashboard` | Compliance rollup |
| GET | `/api/audit` | `?offset=&limit=` |
| GET | `/api/infra/metrics` | Live infra snapshot |

Settings/credentials (session UI; usually not Bearer-operated): `/api/ui/credentials`, `/api/ui/worker`, tests under `/api/ui/test/*`.

## Write (operator token)

| Method | Path | Body highlights |
| --- | --- | --- |
| POST | `/api/jobs/restore-selected` | `backup_ids`, nodes, storage, `proxmox_vmid_start`, `pve_cluster_id`, flags, `add_to_ha` |
| POST | `/api/jobs/restore-tag-group` | `tag`, same restore fields |
| POST | `/api/backups/resolve-tags` | `backup_ids?`, `proxmox_node` |
| POST | `/api/backups/estimate-size` | `backup_ids` (max 20) |
| POST | `/api/plans/{id}/check` | `at_or_before?` |
| POST | `/api/plans/{id}/members` | preview members |
| POST | `/api/plans/{id}/run` | drill / confirm_dr / overwrite / power flags |
| POST | `/api/plans/{id}/assure` | assurance run |
| POST | `/api/plan-runs/{id}/cancel` | stop advancing |
| POST | `/api/plan-runs/{id}/teardown` | reclaim drill guests for that run |
| POST | `/api/cleanup/preview` | `batch_id`, `pve_cluster_id?`, `plan_run_id?` |
| POST | `/api/cleanup/execute` | same; starts async destroy — poll GET |
| POST | `/api/jobs/{id}/stop` | stop one job |
| POST | `/api/jobs/purge` | clear finished |
| POST | `/api/jobs/queue/pause` | pause dequeue |
| POST | `/api/jobs/queue/resume` | resume |
| POST | `/api/jobs/queue/stop` | stop active + drain pending |

Also CRUD under `/api/groups`, `/api/locations`, `/api/plans` (POST/PUT/DELETE). Plan fields include `add_to_ha`, `ha_group`.

## Common HTTP statuses

| Code | Meaning |
| --- | --- |
| 401 | Missing/bad token |
| 403 | Viewer on mutate, or cleanup disabled (`plan_run_cleanup_enabled`) |
| 400 | Validation (storage, isolation, overwrite confirm, unverified plan, bad batch_id) |
| 409 | Another plan run already RUNNING |
| 404 | Unknown plan/job/run |
| 502 | Upstream PVE/PBS failure on cleanup preview/execute |

## `backup_id` shape

`{pbs_source_id}|vm/{vmid}/{timestamp}`  
Example: `main|vm/100/2026-05-01T01:00:00Z`

## Batch id / tags

- Plan run stores `batch_id` (12 hex).
- Guests tagged `re-<batch_id>` and `restore-engine`.
- Cleanup/teardown only destroy dual-tagged (or provenance-proven) guests for that batch on the chosen `pve_cluster_id`.
