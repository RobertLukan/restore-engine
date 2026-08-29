# Gap analysis and test coverage

Updated inventory of restore-engine product features vs automated tests (offline suite).  
**Suite size (approx.):** 32+ test modules / **174 tests**.  
**Style:** FakeRedis + mocks; no live PBS/PVE/Redis. **Coverage tooling:** `pytest-cov` (see README). **CI:** [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs `pytest` on push/PR to `main`.  
**Baseline coverage (this pass):** ~**73%** statements (`pytest --cov=. --cov-fail-under=0`).

Manual lab evidence (utility-host restores, PBS mount, `timeout_sec`, operator-chosen UI host port) is noted separately from automated coverage.

---

## Product feature inventory

| Area | Status | Notes |
|------|--------|-------|
| Auth (password session, API tokens) | **Implemented** | Tokens config-only; no Settings UI |
| Multi-PBS inventory / backups list | **Implemented** | |
| Tag resolve (`extractconfig`) | **Implemented** | Manual “Load tags” (not auto) |
| Size estimate (fidx) | **Implemented** | Uses PBS `ns=` param |
| Restore selected / by tag | **Implemented** | |
| Job queue, concurrency, pause/stop | **Implemented** | |
| Worker restore pipeline | **Implemented** | Highest test risk historically |
| DR mode / ownership / teardown | **Implemented** | |
| Groups / locations / plans | **Implemented** | Name patterns, VMID ranges, picker; tags optional |
| Plan readiness / run / schedule / drills | **Implemented** | |
| Assurance / compliance dashboards | **Implemented** | |
| Reports (MD/HTML) | **Implemented** | |
| Notifications email + webhook | **Implemented** | Email test endpoint; no webhook test UI |
| Infra API snapshot + Grafana embed | **Implemented** | Optional compose profile |
| Audit log | **Implemented** | |
| Job TTL / hygiene | **Implemented** | |
| LXC restore | **Missing** | QEMU only |
| Version from git | **Partial** | Hardcoded `0.1.0` |

---

## Test coverage matrix (modules)

| Module | Depth | Key tests |
|--------|-------|-----------|
| `plans.py` | **Well** (domain) | `test_plans`, `test_plan_readiness`, `test_plan_teardown`, `test_assurance` |
| `jobs.py` | **Well** | `test_dr_restore`, `test_load_balance`, `test_qga_power_on`, `test_post_b4` |
| `progress_parse.py` | **Well** | `test_progress_parse` |
| `sources.py` / `pbs_client.py` | **Well–partial** | `test_sources`, `test_pbs_parse`, `test_pbs_auth` |
| `pbs_wire.py` | **Partial** | parse/usage/cache; reader I/O mocked |
| `pve_client.py` | **Partial** | archive, ownership, submit, QGA wait; thin on connect/storages |
| `queue_control.py` / `concurrency.py` | **Well–partial** | `test_queue_control`, `test_post_b4` |
| `reports.py` | **Partial** | render/dashboard; thin HTTP download |
| `notifications.py` | **Partial** | email + webhook (`test_notifications`) |
| `metrics_collect.py` | **Thin** | config + mocked `/api/infra/*` |
| `main.py` HTTP | **Partial** | auth, health, estimate, tags; plans/jobs HTTP in P0 |
| `ui.py` | **Partial** | credentials; thin on storages/nodes/tests |
| `worker.py` | **P0 added** | `test_worker_process` |
| `audit.py` / `job_hygiene.py` | **P0 added** | `test_audit`, `test_job_hygiene` |
| `static/index.html` | **None** | Manual / no e2e |

---

## Highest risk (features that can look “done” but break)

1. Worker `process_job` (submit → poll → stamp → net → power/off)  
2. HTTP wiring for plans/runs/jobs (auth, body validation, error mapping)  
3. Config mistakes (`pve_storage` must be PBS type; short Proxmox timeout)  
4. PBS API quirks (`ns` vs `namespace` on files/reader)  
5. UI-only flows (storage-by-node, support bundle) — untested automatically  

---

## Manually verified in lab (not a substitute for unit tests)

- Docker offline bundle on Proxmox utility host  
- Restore to Ceph with correct PBS storage ID  
- `proxmox.timeout_sec: 30` fixing sluggish API / storages list  
- Disabling auto tag load for large inventories  

---

## Product gaps (not test gaps)

| Gap | Severity |
|-----|----------|
| Group `source_ids` not exposed in UI | Done (Groups form) |
| Flexible group selectors (name patterns, VMID ranges, selection→group) | Done |
| No Settings UI for API tokens | Low |
| No webhook “test send” endpoint | Low |
| No LXC restore | By design for now |
| Hardcoded `/version` | Low |
| FakeRedis duplicated across tests | P2 cleanup |

---

## Remediation backlog

### P0 (this pass)

- Gap doc + `pytest-cov`  
- `test_worker_process` (happy / cancel / submit fail)  
- `test_job_hygiene`, `test_audit`  
- HTTP: plans check/run/cancel; jobs restore-selected/stop/stats  
- Webhook `post_webhook` tests  

### P1

- GitHub Actions: `pytest` on PR  
- More `main.py` routes: reports download, assurance/compliance HTTP  
- `ui.py` proxmox-storages / test-email  
- Raise `--cov-fail-under` once baseline is known  

### P2

- Shared FakeRedis fixture module  
- Playwright smoke against lab  
- Live integration job (optional, gated)  

---

## How to run coverage

```bash
pip install -r requirements-dev.txt
pytest --cov=. --cov-report=term-missing --cov-fail-under=0
```

Exclude noise as needed: `--cov-config` / omit `tests/*`, `.venv/*` (add `.coveragerc` in a later pass if desired).
