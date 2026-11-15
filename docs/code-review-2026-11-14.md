# Code review — 2026-11-14

## Summary

The project has a solid functional foundation, but this review identified **five
high-priority safety/security issues and one medium-priority API defect** to
address before production use. All findings below are **open as of this review**;
this document does not implement fixes.

| ID | Priority | Finding | Status |
| --- | --- | --- | --- |
| RE-01 | High | Historical run provenance can authorize deletion of a replacement VM | Fixed |
| RE-02 | High | Live restore boots before network isolation is applied | Fixed |
| RE-03 | High | Separate pending batches can receive duplicate target VMIDs | Fixed |
| RE-04 | High | Token-only deployments can accept forged operator sessions with the default signing key | Fixed |
| RE-05 | High | Worker advancement can finalize a plan while startup is still enqueueing | Fixed |
| RE-06 | Medium | Ad-hoc restore requests silently discard HA options | Fixed |

Remediation landed after this review (see git history / tests tagged RE-01…RE-06).
`ui.session_secret` is now **required** at startup for all deployments (including
token-only). Update existing configs that omitted it before upgrading.

## Scope and validation

- Reviewed checkout base: `47a076bde4ca441b58be0d91648014750d3145e3`.
- Focus: API/authentication, enqueue and VMID allocation, worker restore ordering,
  plan lifecycle, teardown ownership, and supporting tests/deployment configuration.
- Existing local changes in `scripts/README.md`, `scripts/add-ha.py`, and
  `scripts/tag-batch.py` were left untouched. This was not a focused review of
  those script changes.
- Baseline command: `.venv/bin/python -m pytest -q`.
- Baseline result: **230 passed, 1 warning**, in 0.72 seconds. The warning was a
  Starlette TestClient deprecation concerning its use of `httpx`.
- Additional reproduction probes used the existing FakeRedis/fake PVE helpers,
  Python mocks, and an in-process HTTP test client. They were executed as ad-hoc
  checks, not added to the committed test suite.
- No live PBS, PVE, or Redis systems were contacted by these checks. No real
  restore, guest deletion, or deployment operation was performed.
- This is a source-level review with mocked validation, not a live infrastructure,
  browser, dependency-vulnerability, or penetration-test assessment. Passing the
  existing suite does not establish that these lifecycle races are safe.

Source line references below refer to the reviewed checkout and may move as fixes
are made.

## RE-01 — Historical run provenance can delete a replacement VM

**Priority:** High  
**References:** [`plans.py:2331–2374`](../plans.py#L2331),
[`pve_client.py:778–819`](../pve_client.py#L778)

### Cause and impact

`teardown_plan_run` treats a job's historical `pve_upid`, `restore_started_at`, or
`managed_marked` field as sufficient provenance for its target VMID. It passes
`allow_run_provenance=True` to `destroy_owned_qemu_vm`, bypassing current ownership
and batch requirements.

A VMID identifies a slot, not a permanent guest identity. If the original restored
guest is removed and that VMID is reused, teardown of the old run can destroy the
replacement guest. This includes an unmarked foreign QEMU guest or a guest owned
by a different restore batch.

### Mocked reproduction

1. Seed an old completed run targeting VMID `200`, with a job hash containing an
   old `pve_upid` and target VMID `200`.
2. Present a replacement guest at VMID `200`, tagged only `prod`, with no
   restore-engine ownership marker.
3. Invoke `teardown_plan_run` using FakeRedis and the fake PVE client.
4. Observed: teardown status `completed`; replacement guest deleted in the fake
   client.

A separate helper-level check also allowed deletion of a managed guest tagged for
batch `bbbbbbbbbbbb` when teardown requested batch `aaaaaaaaaaaa` and historical
provenance was enabled.

### Recommended remediation and regression coverage

- Require ownership evidence tied to the **current guest**, not historical VMID
  assignment alone.
- Refuse a guest carrying a conflicting batch identity even when old job metadata
  exists.
- Handle unmarked leftovers through an explicit, narrowly scoped recovery procedure
  rather than an automatic provenance bypass.
- Check ownership before removing HA registration as well as before destruction.
- Add tests for VMID reuse by foreign guests, VMID reuse by a different batch,
  matching-current-batch deletion, and explicitly authorized leftover recovery.

## RE-02 — Live restore boots before network isolation is applied

**Priority:** High  
**References:** [`main.py:302–316`](../main.py#L302),
[`worker.py:228–238`](../worker.py#L228),
[`worker.py:381–406`](../worker.py#L381)

### Cause and impact

The API accepts `live_restore=true` with `network_mode=unlink` or `remap` as meeting
the isolation requirement. The worker, however, submits the live restore and waits
for restore completion before applying NIC isolation.

Live restore boots the guest during restoration. It can therefore initially use
the backup's production bridge and network configuration. Applying isolation
afterward does not eliminate the initial exposure or possible identity conflicts.

### Mocked reproduction

The API isolation helper accepted live restore with `network_mode=unlink`. A
mocked worker run recorded this order:

```text
submit-live-restore
wait-for-restore-completion
apply-network-isolation
```

The probe establishes application call order; actual guest boot/network traffic
was not tested against PVE.

### Recommended remediation and regression coverage

- Reject live restore when safety depends on NIC changes made after restore.
- Allow live restore only on infrastructure already isolated before boot, unless
  the operator explicitly accepts non-isolated operation.
- Apply a consistent policy to ad-hoc restores, plan runs, assurance, and schedules.
- Add tests proving that post-restore `unlink`/`remap` alone cannot authorize live
  boot on a shared network, while ordinary powered-off restore followed by
  isolation and explicit start remains supported.

## RE-03 — Pending batches can receive duplicate target VMIDs

**Priority:** High  
**References:** [`jobs.py:191–228`](../jobs.py#L191),
[`jobs.py:318–319`](../jobs.py#L318),
[`pve_client.py:1050–1056`](../pve_client.py#L1050)

### Cause and impact

Allocation checks VMIDs already present in PVE, but does not reserve IDs assigned
to pending jobs. Separate batches queued before the worker creates either guest
can receive the same target VMID. Simultaneous HTTP requests are not required;
successive requests while the queue is paused are sufficient.

Normal restores also set `force=1`, increasing the risk if a queued target slot
becomes occupied before submission. The observed collision is confirmed; actual
overwrite behavior was not exercised against PVE.

### Mocked reproduction

1. Mock the cluster's existing VMID set as empty.
2. Enqueue two batches with `vmid_start=5000` without executing either batch.
3. Observed: both batches were assigned `[5000]`; both job hashes had `force=1`.

### Recommended remediation and regression coverage

- Atomically reserve VMIDs per target cluster, including pending and restoring
  jobs. A scan without serialization is insufficient for concurrent requests.
- Define reservation release and restart-recovery behavior.
- Use `force=false` for normal restores so unexpected occupancy fails safely.
- Add tests for successive pending batches, concurrent allocations, independent
  clusters, reservation release, and a guest appearing after allocation but before
  restore submission.

## RE-04 — Token-only configurations can accept forged operator sessions

**Priority:** High  
**References:** [`main.py:56–86`](../main.py#L56),
[`main.py:1782–1783`](../main.py#L1782),
[`ui.py:63–82`](../ui.py#L63)

### Cause and impact

Session-secret validation is skipped when `ui.password` is unset, including when
API tokens are configured. Without an explicit secret, the application falls back
to the publicly known development signing key.

Cookie authentication remains enabled. A session asserting
`ui_authenticated=true`, signed with that known key, is accepted as an operator
before bearer-token permissions are checked. This bypasses token authentication
and viewer-only restrictions.

This finding is conditional: a deployment using a strong, private session secret
does not have this specific known-key bypass. Token-only operation with a missing
secret is the reproduced configuration; weak/default configured secrets also need
validation regardless of whether a UI password exists.

### Mocked reproduction

1. Configure a viewer API token, omit `ui.password` and `ui.session_secret`.
2. Confirm `validate_session_secret` accepts the configuration.
3. Build an in-process test app using the actual session-secret loader,
   SessionMiddleware, and `require_ui_session` dependency.
4. Observed: an unauthenticated protected POST returned `401`; a POST carrying a
   session signed with the development key returned `200`.

No production cookie or credential was used or generated for a live deployment.

### Recommended remediation and regression coverage

- Require a strong private signing secret whenever cookie sessions are enabled,
  independently of `ui.password`.
- Alternatively, explicitly disable cookie authentication for token-only mode.
- Reject the development placeholder in every session-enabled configuration.
- Add startup and HTTP regression tests for token-only configurations, missing or
  weak secrets, development-key cookies, and viewer-token permission enforcement.

## RE-05 — Worker advancement can finalize a run during startup

**Priority:** High  
**References:** [`plans.py:1610–1655`](../plans.py#L1610),
[`plans.py:1657–1674`](../plans.py#L1657),
[`plans.py:1708–1722`](../plans.py#L1708)

### Cause and impact

Startup publishes a `RUNNING` record with empty job lists before enqueueing the
first group and before persisting pending group rows. The worker does not share
the startup lock and interprets these empty lists as completed groups.

A worker tick in this window can mark the run `COMPLETED` and enter finalization
before its jobs have been recorded. Startup later saves its local copy as
`RUNNING` again. The interleaving can produce premature reports or teardown and
temporarily remove the running-plan guard.

### Mocked reproduction

1. Start a plan with a non-empty first group.
2. In the injected enqueue callback, call `advance_plan_runs` before returning job
   IDs, simulating a worker tick during startup.
3. Mock the report-finalization helper to prevent unrelated side effects.
4. Observed: the stored run became `COMPLETED` during enqueue; startup subsequently
   returned a `RUNNING` run.

The probe confirms premature state transition. It did not exercise actual
concurrent workers, live teardown, or a second plan start.

### Recommended remediation and regression coverage

- Introduce a non-advanceable `STARTING` state that still blocks another plan start,
  or atomically publish a fully initialized run.
- Do not treat incomplete initialization as successful completion.
- Protect lifecycle transitions from stale writes and ensure finalization is
  idempotent.
- Add deterministic interleaving tests for startup versus advancement, startup
  failures, a second plan start, and exactly-once report/teardown behavior.

## RE-06 — Ad-hoc restore requests silently discard HA options

**Priority:** Medium  
**References:** [`main.py:327–344`](../main.py#L327),
[`main.py:538–555`](../main.py#L538),
[`jobs.py:111–112`](../jobs.py#L111)

### Cause and impact

`RestoreSelectedRequest` does not declare `add_to_ha` or `ha_group`, and the
restore-selected endpoint does not forward them. Pydantic silently discards the
extra fields, even though the underlying enqueue function supports them.

An API caller can request HA registration without receiving a validation error,
while the job proceeds without that policy. This finding concerns ad-hoc
restore-selected requests, not plan-level HA handling.

### Mocked reproduction

Construct `RestoreSelectedRequest` with valid backup IDs plus `add_to_ha=true` and
`ha_group=ha1`. Observed: neither HA field appears in `model_dump()`. Source
inspection also confirms that the endpoint does not pass those options onward.

### Recommended remediation and regression coverage

- If ad-hoc HA is supported, add the fields to the request model and forward them
  to enqueueing.
- Otherwise reject unsupported fields rather than silently accepting them, and
  align the API documentation with supported behavior.
- Add an HTTP-level test checking the resulting job's `add_to_ha` and `ha_group`
  values, plus validation for unsupported options.

## Remediation order and completion criteria

1. Prioritize **RE-01, RE-02, and RE-03**: guest deletion, network exposure, and
   target identity allocation are restore-safety boundaries.
2. Address **RE-04 and RE-05**: authentication and plan lifecycle serialization.
3. Resolve **RE-06** and align the request contract with documentation.

For each finding, add the regression tests described above, record the fix commit,
and rerun the full offline suite. Validate PVE-specific restore/boot behavior in a
disposable, already-isolated lab only with explicit authorization. Do not close a
finding solely because the pre-existing suite still passes.

The existing module separation, explicit safety gates, and functional tests are
useful foundations. The main coverage gaps exposed here are **VMID reuse,
overlapping enqueue/worker execution, and isolation timing**.
