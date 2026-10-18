# Domain reference

## Key files

| Topic | Files |
| --- | --- |
| Enqueue / LJF | `jobs.py` (`sort_rows_largest_first`, `enqueue_largest_first_enabled`, `enqueue_restores`) |
| Plan exclusivity | `plans.py` (busy / RUNNING), `main.py` (409 mapping) |
| Soft-success | `pve_client.py` (`pve_exitstatus_is_success`, `wait_for_task`), `worker.py` |
| Jobs list / purge | `job_hygiene.py`, `main.py` purge route, `static/index.html` Restores |
| Session secret | `main.py` (`validate_session_secret`), `tests/test_session_secret.py` |
| Compose / image tag | `docker-compose.yml`, `DOCKER.md` |

## Tests to keep green when touching invariants

- `tests/test_enqueue_order.py`
- `tests/test_wait_for_task.py` (or equivalent WARNINGS coverage)
- `tests/test_plan_run_busy.py`
- `tests/test_jobs_list_purge.py`

## Operator-facing docs

- `README.md` — features, production checklist, safety notes
- `DOCKER.md` — enqueue order note, offline section, secrets/Redis
- `docs/dr-overview.md` — DR workflow (generic)
