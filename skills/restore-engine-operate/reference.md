# Operate API cheat sheet

Base: `$RESTORE_ENGINE_URL` · Auth: `Authorization: Bearer $RESTORE_ENGINE_TOKEN`

Prefix for authenticated JSON API: `/api` (except `/version`, `/health`).

## Read

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/version` | Build/version (no auth required on most installs) |
| GET | `/api/backups` | Optional `?offset=&limit=` |
| GET | `/api/restore-defaults` | Optional `?proxmox_node=` |
| GET | `/api/jobs` | `?state=&offset=&limit=` → `{items,total,…}` |
| GET | `/api/jobs/{id}` | Job record |
| GET | `/api/jobs/{id}/log` | Log lines |
| GET | `/api/jobs/stats` | Queue / concurrency |
| GET | `/api/plans` | Plans |
| GET | `/api/plans/{id}` | One plan |
| GET | `/api/groups` | Groups |
| GET | `/api/locations` | Locations |
| GET | `/api/plan-runs` | Optional `?plan_id=&limit=` |
| GET | `/api/plan-runs/{id}` | Aggregated run |
| GET | `/api/reports` | Compliance reports |
| GET | `/api/assurance/dashboard` | Assurance rollup |
| GET | `/api/compliance/dashboard` | Compliance rollup |
| GET | `/api/audit` | `?offset=&limit=` |
| GET | `/api/infra/metrics` | Live infra snapshot |

## Write (operator token)

| Method | Path | Body highlights |
| --- | --- | --- |
| POST | `/api/jobs/restore-selected` | `backup_ids`, nodes, storage, `proxmox_vmid_start`, flags |
| POST | `/api/jobs/restore-tag-group` | `tag`, same restore fields |
| POST | `/api/backups/resolve-tags` | `backup_ids?`, `proxmox_node` |
| POST | `/api/backups/estimate-size` | `backup_ids` (max 20) |
| POST | `/api/plans/{id}/check` | `at_or_before?` |
| POST | `/api/plans/{id}/run` | drill / confirm_dr / overwrite / power flags |
| POST | `/api/plans/{id}/assure` | assurance run |
| POST | `/api/plan-runs/{id}/cancel` | stop advancing |
| POST | `/api/plan-runs/{id}/teardown` | reclaim drill guests |
| POST | `/api/jobs/{id}/stop` | stop one job |
| POST | `/api/jobs/purge` | clear finished |
| POST | `/api/jobs/queue/pause` | pause dequeue |
| POST | `/api/jobs/queue/resume` | resume |
| POST | `/api/jobs/queue/stop` | stop active + drain pending |

Also CRUD under `/api/groups`, `/api/locations`, `/api/plans` (POST/PUT/DELETE).

## Common HTTP statuses

| Code | Meaning |
| --- | --- |
| 401 | Missing/bad token |
| 403 | Viewer token on mutating method |
| 400 | Validation (storage, isolation, overwrite confirm, unverified plan) |
| 409 | Another plan run already RUNNING |
| 404 | Unknown plan/job/run |

## `backup_id` shape

`{pbs_source_id}|vm/{vmid}/{timestamp}`  
Example: `main|vm/100/2026-05-01T01:00:00Z`
