# Domain reference

## Key files

| Topic | Files |
| --- | --- |
| Enqueue / LJF | `jobs.py` (`sort_rows_largest_first`, `enqueue_largest_first_enabled`, `enqueue_restores`) |
| Multi-PVE | `clusters.py`, `ui.py` (`pve_clusters_view`, credentials persist), `sources.py` |
| Plan exclusivity | `plans.py` (busy / RUNNING), `main.py` (409 mapping) |
| Batch tags / cleanup | `pve_client.py` (`batch_tag_for_id`, destroy wait), `plans.py` (cleanup_*), `main.py` `/api/cleanup/*`, `ui.plan_run_cleanup_enabled` |
| HA | `worker.py` / `pve_client.py` HA helpers, plan `add_to_ha`, `scripts/add-ha.py` |
| Soft-success | `pve_client.py` (`pve_exitstatus_is_success`, `wait_for_task`), `worker.py` |
| Jobs list / purge | `job_hygiene.py`, `main.py` purge route, `static/index.html` Restores |
| Session secret | `main.py` (`validate_session_secret`), `tests/test_session_secret.py` |
| Compose / networking | `docker-compose.yml` (host network), `docker-compose.bridge.yml`, `scripts/lab-mac.sh`, `DOCKER.md` |

## Tests to keep green when touching invariants

- `tests/test_enqueue_order.py`
- `tests/test_wait_for_task.py` (or equivalent WARNINGS coverage)
- `tests/test_plan_run_busy.py`
- `tests/test_jobs_list_purge.py`
- `tests/test_clusters.py`
- `tests/test_ha.py`
- `tests/test_plan_run_cleanup.py`
- `tests/test_guest_ownership.py`

## Operator-facing docs

- `README.md` — features, production checklist, safety notes
- `DOCKER.md` — host vs bridge networking, Mac lab, enqueue order, offline, secrets/Redis
- `docs/dr-overview.md` — DR workflow (generic)
