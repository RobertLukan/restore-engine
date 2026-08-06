"""Redis-backed recovery plans: inventory groups, locations, plans, and plan runs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import redis

from states import PlanRunStatus, PlanVerification, RestoreState

TERMINAL_JOB_STATES = {
    RestoreState.COMPLETED.value,
    RestoreState.FAILED.value,
    RestoreState.CANCELLED.value,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redis_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("redis") or {}


def group_key(cfg: dict[str, Any], group_id: str) -> str:
    prefix = _redis_cfg(cfg).get("group_key_prefix", "restore:group:")
    return f"{prefix}{group_id}"


def location_key(cfg: dict[str, Any], location_id: str) -> str:
    prefix = _redis_cfg(cfg).get("location_key_prefix", "restore:location:")
    return f"{prefix}{location_id}"


def plan_key(cfg: dict[str, Any], plan_id: str) -> str:
    prefix = _redis_cfg(cfg).get("plan_key_prefix", "restore:plan:")
    return f"{prefix}{plan_id}"


def plan_run_key(cfg: dict[str, Any], run_id: str) -> str:
    prefix = _redis_cfg(cfg).get("plan_run_key_prefix", "restore:planrun:")
    return f"{prefix}{run_id}"


def groups_index(cfg: dict[str, Any]) -> str:
    return _redis_cfg(cfg).get("groups_index", "restore:groups")


def locations_index(cfg: dict[str, Any]) -> str:
    return _redis_cfg(cfg).get("locations_index", "restore:locations")


def plans_index(cfg: dict[str, Any]) -> str:
    return _redis_cfg(cfg).get("plans_index", "restore:plans")


def plan_runs_index(cfg: dict[str, Any]) -> str:
    return _redis_cfg(cfg).get("plan_runs_index", "restore:planruns")


def active_plan_runs_key(cfg: dict[str, Any]) -> str:
    return _redis_cfg(cfg).get("active_plan_runs_key", "restore:planruns:active")


def _dump(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def _load(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return json.loads(raw)


def _save_entity(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    key: str,
    index: str,
    entity_id: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    data = {**data, "id": entity_id, "updated_at": utc_now_iso()}
    if "created_at" not in data or not data["created_at"]:
        data["created_at"] = data["updated_at"]
    pipe = r.pipeline(transaction=True)
    pipe.set(key, _dump(data))
    pipe.sadd(index, entity_id)
    pipe.execute()
    return data


def _delete_entity(r: redis.Redis, *, key: str, index: str, entity_id: str) -> bool:
    pipe = r.pipeline(transaction=True)
    pipe.delete(key)
    pipe.srem(index, entity_id)
    deleted, _ = pipe.execute()
    return bool(deleted)


def _list_entities(r: redis.Redis, cfg: dict[str, Any], *, index: str, key_fn: Callable[[dict[str, Any], str], str]) -> list[dict[str, Any]]:
    ids = sorted(r.smembers(index) or [])
    out: list[dict[str, Any]] = []
    for entity_id in ids:
        data = _load(r.get(key_fn(cfg, entity_id)))
        if data:
            out.append(data)
    return out


# --- Inventory groups ---


def normalize_group(payload: dict[str, Any], *, group_id: str | None = None) -> dict[str, Any]:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    tags = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()]
    source_ids = [str(s).strip() for s in (payload.get("source_ids") or []) if str(s).strip()]
    vmids_raw = payload.get("vmids") or []
    vmids: list[int] = []
    for v in vmids_raw:
        try:
            vmids.append(int(v))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid vmid: {v!r}") from exc
    if not tags and not vmids:
        raise ValueError("group needs at least one tag or one vmid")
    return {
        "id": group_id or str(uuid.uuid4()),
        "name": name,
        "tags": tags,
        "source_ids": source_ids,
        "vmids": vmids,
        "mode": "latest_per_vmid",
        "created_at": payload.get("created_at") or "",
        "updated_at": payload.get("updated_at") or "",
    }


def create_group(r: redis.Redis, cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    data = normalize_group(payload)
    return _save_entity(r, cfg, key=group_key(cfg, data["id"]), index=groups_index(cfg), entity_id=data["id"], data=data)


def update_group(r: redis.Redis, cfg: dict[str, Any], group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = get_group(r, cfg, group_id)
    if not existing:
        raise KeyError(group_id)
    merged = {**existing, **payload, "id": group_id}
    data = normalize_group(merged, group_id=group_id)
    data["created_at"] = existing.get("created_at") or ""
    return _save_entity(r, cfg, key=group_key(cfg, group_id), index=groups_index(cfg), entity_id=group_id, data=data)


def get_group(r: redis.Redis, cfg: dict[str, Any], group_id: str) -> dict[str, Any] | None:
    return _load(r.get(group_key(cfg, group_id)))


def list_groups(r: redis.Redis, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return _list_entities(r, cfg, index=groups_index(cfg), key_fn=group_key)


def delete_group(r: redis.Redis, cfg: dict[str, Any], group_id: str) -> bool:
    return _delete_entity(r, key=group_key(cfg, group_id), index=groups_index(cfg), entity_id=group_id)


# --- Recovery locations ---


def normalize_location(payload: dict[str, Any], *, location_id: str | None = None) -> dict[str, Any]:
    name = (payload.get("name") or "").strip()
    node = (payload.get("node") or "").strip()
    storage = (payload.get("storage") or "").strip()
    if not name:
        raise ValueError("name is required")
    if not node:
        raise ValueError("node is required")
    if not storage:
        raise ValueError("storage is required")
    try:
        vmid_start = int(payload.get("vmid_start", 100))
    except (TypeError, ValueError) as exc:
        raise ValueError("vmid_start must be an integer >= 100") from exc
    if vmid_start < 100:
        raise ValueError("vmid_start must be >= 100")
    try:
        bwlimit = int(payload.get("bwlimit", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("bwlimit must be an integer >= 0") from exc
    if bwlimit < 0:
        raise ValueError("bwlimit must be >= 0")
    return {
        "id": location_id or str(uuid.uuid4()),
        "name": name,
        "node": node,
        "storage": storage,
        "vmid_start": vmid_start,
        "bwlimit": bwlimit,
        "live_restore": bool(payload.get("live_restore", False)),
        "created_at": payload.get("created_at") or "",
        "updated_at": payload.get("updated_at") or "",
    }


def create_location(r: redis.Redis, cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    data = normalize_location(payload)
    return _save_entity(
        r, cfg, key=location_key(cfg, data["id"]), index=locations_index(cfg), entity_id=data["id"], data=data
    )


def update_location(r: redis.Redis, cfg: dict[str, Any], location_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = get_location(r, cfg, location_id)
    if not existing:
        raise KeyError(location_id)
    merged = {**existing, **payload, "id": location_id}
    data = normalize_location(merged, location_id=location_id)
    data["created_at"] = existing.get("created_at") or ""
    return _save_entity(
        r, cfg, key=location_key(cfg, location_id), index=locations_index(cfg), entity_id=location_id, data=data
    )


def get_location(r: redis.Redis, cfg: dict[str, Any], location_id: str) -> dict[str, Any] | None:
    return _load(r.get(location_key(cfg, location_id)))


def list_locations(r: redis.Redis, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return _list_entities(r, cfg, index=locations_index(cfg), key_fn=location_key)


def delete_location(r: redis.Redis, cfg: dict[str, Any], location_id: str) -> bool:
    return _delete_entity(r, key=location_key(cfg, location_id), index=locations_index(cfg), entity_id=location_id)


# --- Recovery plans ---


def normalize_plan(payload: dict[str, Any], *, plan_id: str | None = None) -> dict[str, Any]:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    group_ids = [str(g).strip() for g in (payload.get("group_ids") or []) if str(g).strip()]
    if not group_ids:
        raise ValueError("plan needs at least one group_id")
    location_id = (payload.get("location_id") or "").strip()
    if not location_id:
        raise ValueError("location_id is required")
    verification = (payload.get("verification") or PlanVerification.NOT_VERIFIED.value).strip()
    if verification not in {v.value for v in PlanVerification}:
        raise ValueError(f"invalid verification: {verification}")
    return {
        "id": plan_id or str(uuid.uuid4()),
        "name": name,
        "group_ids": group_ids,
        "location_id": location_id,
        "halt_on_error": bool(payload.get("halt_on_error", True)),
        "enabled": bool(payload.get("enabled", True)),
        "verification": verification,
        "last_check_at": payload.get("last_check_at") or "",
        "last_run_at": payload.get("last_run_at") or "",
        "created_at": payload.get("created_at") or "",
        "updated_at": payload.get("updated_at") or "",
    }


def create_plan(r: redis.Redis, cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    data = normalize_plan(payload)
    return _save_entity(r, cfg, key=plan_key(cfg, data["id"]), index=plans_index(cfg), entity_id=data["id"], data=data)


def update_plan(r: redis.Redis, cfg: dict[str, Any], plan_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = get_plan(r, cfg, plan_id)
    if not existing:
        raise KeyError(plan_id)
    # Changing membership invalidates verification.
    membership_changed = False
    if "group_ids" in payload and payload["group_ids"] is not None:
        new_ids = [str(g).strip() for g in payload["group_ids"] if str(g).strip()]
        if new_ids != existing.get("group_ids"):
            membership_changed = True
    if "location_id" in payload and (payload.get("location_id") or "").strip() != existing.get("location_id"):
        membership_changed = True
    merged = {**existing, **payload, "id": plan_id}
    if membership_changed and "verification" not in payload:
        merged["verification"] = PlanVerification.NEEDS_VERIFIED.value
    data = normalize_plan(merged, plan_id=plan_id)
    data["created_at"] = existing.get("created_at") or ""
    data["last_check_at"] = merged.get("last_check_at") or existing.get("last_check_at") or ""
    data["last_run_at"] = merged.get("last_run_at") or existing.get("last_run_at") or ""
    return _save_entity(r, cfg, key=plan_key(cfg, plan_id), index=plans_index(cfg), entity_id=plan_id, data=data)


def get_plan(r: redis.Redis, cfg: dict[str, Any], plan_id: str) -> dict[str, Any] | None:
    return _load(r.get(plan_key(cfg, plan_id)))


def list_plans(r: redis.Redis, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return _list_entities(r, cfg, index=plans_index(cfg), key_fn=plan_key)


def delete_plan(r: redis.Redis, cfg: dict[str, Any], plan_id: str) -> bool:
    return _delete_entity(r, key=plan_key(cfg, plan_id), index=plans_index(cfg), entity_id=plan_id)


# --- Member resolution ---


def resolve_group_rows(
    group: dict[str, Any],
    backups: list[dict[str, Any]],
    *,
    cutoff: str,
    tags_by_backup_id: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Pick latest-per-vmid rows for a group at or before cutoff.

    Matching rules:
    - ``vmids`` only: those VMIDs (latest snapshot ≤ cutoff).
    - ``tags`` only: VMs whose guest tags contain *all* listed tags (AND).
    - both: intersection (listed VMIDs that also carry all tags).
    - optional ``source_ids`` filters by backup source id/label (case-insensitive).
    """
    source_filter = {s.lower() for s in (group.get("source_ids") or [])}
    candidates = [row for row in backups if row.get("timestamp", "") <= cutoff]
    if source_filter:
        candidates = [
            row
            for row in candidates
            if (row.get("source_id") or row.get("server_id") or "").lower() in source_filter
            or (row.get("source_label") or "").lower() in source_filter
        ]

    best: dict[int, dict[str, Any]] = {}
    for row in candidates:
        vmid = int(row["vmid"])
        current = best.get(vmid)
        if current is None or row["timestamp"] > current["timestamp"]:
            best[vmid] = row

    explicit_vmids = {int(v) for v in (group.get("vmids") or [])}
    wanted_tags = {t.lower() for t in (group.get("tags") or []) if str(t).strip()}
    tags_by_backup_id = tags_by_backup_id or {}

    if explicit_vmids and not wanted_tags:
        selected = [best[v] for v in sorted(explicit_vmids) if v in best]
    elif wanted_tags and not explicit_vmids:
        selected = []
        for vmid in sorted(best):
            row = best[vmid]
            row_tags = {t.lower() for t in tags_by_backup_id.get(row["backup_id"], [])}
            if wanted_tags.issubset(row_tags):
                selected.append(row)
    else:
        selected = []
        for vmid in sorted(explicit_vmids):
            row = best.get(vmid)
            if not row:
                continue
            row_tags = {t.lower() for t in tags_by_backup_id.get(row["backup_id"], [])}
            if wanted_tags.issubset(row_tags):
                selected.append(row)

    return selected


# --- Plan runs ---


def get_plan_run(r: redis.Redis, cfg: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    return _load(r.get(plan_run_key(cfg, run_id)))


def list_plan_runs(r: redis.Redis, cfg: dict[str, Any], *, plan_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    runs = _list_entities(r, cfg, index=plan_runs_index(cfg), key_fn=plan_run_key)
    if plan_id:
        runs = [run for run in runs if run.get("plan_id") == plan_id]
    runs.sort(key=lambda run: run.get("started_at") or "", reverse=True)
    return runs[: max(1, limit)]


def save_plan_run(r: redis.Redis, cfg: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    run_id = run["id"]
    run = {**run, "updated_at": utc_now_iso()}
    pipe = r.pipeline(transaction=True)
    pipe.set(plan_run_key(cfg, run_id), _dump(run))
    pipe.sadd(plan_runs_index(cfg), run_id)
    if run.get("status") == PlanRunStatus.RUNNING.value:
        pipe.sadd(active_plan_runs_key(cfg), run_id)
    else:
        pipe.srem(active_plan_runs_key(cfg), run_id)
    pipe.execute()
    return run


def start_plan_run(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    plan: dict[str, Any],
    location: dict[str, Any],
    cutoff: str,
    group_rows: list[list[dict[str, Any]]],
    enqueue_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Create a plan run and enqueue the first non-empty group."""
    if len(group_rows) != len(plan["group_ids"]):
        raise ValueError("group_rows length must match plan.group_ids")
    if not any(group_rows):
        raise ValueError("plan resolved to zero backups")

    run_id = str(uuid.uuid4())
    now = utc_now_iso()
    job_ids_by_group: list[list[str]] = [[] for _ in plan["group_ids"]]
    run = {
        "id": run_id,
        "plan_id": plan["id"],
        "plan_name": plan.get("name", ""),
        "cutoff": cutoff,
        "status": PlanRunStatus.RUNNING.value,
        "location_id": location["id"],
        "location_name": location.get("name", ""),
        "node": location["node"],
        "storage": location["storage"],
        "vmid_start": location["vmid_start"],
        "bwlimit": location.get("bwlimit", 0),
        "live_restore": bool(location.get("live_restore", False)),
        "halt_on_error": bool(plan.get("halt_on_error", True)),
        "group_ids": list(plan["group_ids"]),
        "job_ids_by_group": job_ids_by_group,
        "current_group_index": 0,
        "started_at": now,
        "finished_at": "",
        "error": "",
        "created_at": now,
        "updated_at": now,
    }

    # Find first non-empty group and enqueue.
    start_idx = next((i for i, rows in enumerate(group_rows) if rows), None)
    if start_idx is None:
        raise ValueError("plan resolved to zero backups")
    run["current_group_index"] = start_idx

    # Persist empty run shell first so jobs can reference it.
    save_plan_run(r, cfg, run)

    result = enqueue_fn(
        r,
        cfg,
        group_rows[start_idx],
        node=location["node"],
        target_storage=location["storage"],
        vmid_start=int(location["vmid_start"]),
        live_restore=bool(location.get("live_restore", False)),
        bwlimit=int(location.get("bwlimit", 0) or 0),
        plan_run_id=run_id,
        plan_group_index=start_idx,
    )
    job_ids_by_group[start_idx] = list(result.get("job_ids") or [])
    run["job_ids_by_group"] = job_ids_by_group
    # Advance vmid_start for subsequent groups based on assigned IDs.
    assigned = result.get("proxmox_vmids_assigned") or []
    if assigned:
        run["next_vmid"] = max(int(v) for v in assigned) + 1
    else:
        run["next_vmid"] = int(location["vmid_start"])
    save_plan_run(r, cfg, run)

    # Stash unresolved later groups as JSON on the run for the worker tick.
    run["pending_group_rows"] = [
        [
            {
                "backup_id": row["backup_id"],
                "vmid": row["vmid"],
                "name": row["name"],
                "timestamp": row["timestamp"],
                "pve_storage": row["pve_storage"],
                "voltail": row["voltail"],
                "source_label": row.get("source_label", ""),
            }
            for row in rows
        ]
        for rows in group_rows
    ]
    save_plan_run(r, cfg, run)

    plan_update = {
        **plan,
        "last_run_at": now,
        "verification": PlanVerification.NEEDS_VERIFIED.value
        if plan.get("verification") == PlanVerification.VERIFIED.value
        else plan.get("verification", PlanVerification.NOT_VERIFIED.value),
    }
    _save_entity(r, cfg, key=plan_key(cfg, plan["id"]), index=plans_index(cfg), entity_id=plan["id"], data=plan_update)
    return run


def advance_plan_runs(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    enqueue_fn: Callable[..., dict[str, Any]],
    job_key_fn: Callable[[dict[str, Any], str], str] | None = None,
) -> int:
    """Advance active plan runs: enqueue next group or finish. Returns runs touched."""
    from jobs import job_key as default_job_key

    if job_key_fn is None:
        job_key_fn = default_job_key

    touched = 0
    active_ids = list(r.smembers(active_plan_runs_key(cfg)) or [])
    for run_id in active_ids:
        run = get_plan_run(r, cfg, run_id)
        if not run or run.get("status") != PlanRunStatus.RUNNING.value:
            r.srem(active_plan_runs_key(cfg), run_id)
            continue

        idx = int(run.get("current_group_index") or 0)
        job_ids_by_group = run.get("job_ids_by_group") or []
        while idx < len(job_ids_by_group) and not job_ids_by_group[idx]:
            # Skip empty groups already recorded.
            pending = run.get("pending_group_rows") or []
            if idx < len(pending) and pending[idx]:
                break
            idx += 1
        if idx >= len(job_ids_by_group):
            run["status"] = PlanRunStatus.COMPLETED.value
            run["finished_at"] = utc_now_iso()
            run["current_group_index"] = idx
            save_plan_run(r, cfg, run)
            touched += 1
            continue

        current_jobs = list(job_ids_by_group[idx] or [])
        if not current_jobs:
            # Need to enqueue this group from pending rows.
            pending = run.get("pending_group_rows") or []
            rows = pending[idx] if idx < len(pending) else []
            if not rows:
                idx += 1
                run["current_group_index"] = idx
                save_plan_run(r, cfg, run)
                touched += 1
                continue
            vmid_start = int(run.get("next_vmid") or run.get("vmid_start") or 100)
            result = enqueue_fn(
                r,
                cfg,
                rows,
                node=run["node"],
                target_storage=run["storage"],
                vmid_start=vmid_start,
                live_restore=bool(run.get("live_restore", False)),
                bwlimit=int(run.get("bwlimit", 0) or 0),
                plan_run_id=run_id,
                plan_group_index=idx,
            )
            job_ids_by_group[idx] = list(result.get("job_ids") or [])
            run["job_ids_by_group"] = job_ids_by_group
            assigned = result.get("proxmox_vmids_assigned") or []
            if assigned:
                run["next_vmid"] = max(int(v) for v in assigned) + 1
            run["current_group_index"] = idx
            save_plan_run(r, cfg, run)
            touched += 1
            continue

        states = []
        for job_id in current_jobs:
            data = r.hgetall(job_key_fn(cfg, job_id))
            states.append((data or {}).get("state") or RestoreState.PENDING.value)

        if not all(s in TERMINAL_JOB_STATES for s in states):
            continue

        failed = [s for s in states if s == RestoreState.FAILED.value]
        cancelled = [s for s in states if s == RestoreState.CANCELLED.value]
        if failed and run.get("halt_on_error", True):
            run["status"] = PlanRunStatus.FAILED.value
            run["error"] = f"{len(failed)} job(s) failed in group index {idx}"
            run["finished_at"] = utc_now_iso()
            save_plan_run(r, cfg, run)
            touched += 1
            continue
        if cancelled and not any(s == RestoreState.COMPLETED.value for s in states) and all(
            s in {RestoreState.CANCELLED.value, RestoreState.FAILED.value} for s in states
        ):
            run["status"] = PlanRunStatus.CANCELLED.value
            run["finished_at"] = utc_now_iso()
            save_plan_run(r, cfg, run)
            touched += 1
            continue

        # Advance to next group.
        next_idx = idx + 1
        while next_idx < len(job_ids_by_group):
            pending = run.get("pending_group_rows") or []
            has_pending = next_idx < len(pending) and bool(pending[next_idx])
            has_jobs = bool(job_ids_by_group[next_idx])
            if has_pending or has_jobs:
                break
            next_idx += 1

        if next_idx >= len(job_ids_by_group):
            run["status"] = PlanRunStatus.COMPLETED.value
            run["finished_at"] = utc_now_iso()
            run["current_group_index"] = next_idx
            save_plan_run(r, cfg, run)
            touched += 1
            continue

        run["current_group_index"] = next_idx
        save_plan_run(r, cfg, run)
        touched += 1

    return touched


def aggregate_plan_run(
    r: redis.Redis, cfg: dict[str, Any], run: dict[str, Any], *, job_key_fn: Callable | None = None
) -> dict[str, Any]:
    """Attach per-job summaries for UI/API."""
    from jobs import job_key as default_job_key

    if job_key_fn is None:
        job_key_fn = default_job_key

    jobs: list[dict[str, Any]] = []
    for group_index, job_ids in enumerate(run.get("job_ids_by_group") or []):
        for job_id in job_ids:
            data = r.hgetall(job_key_fn(cfg, job_id)) or {}
            jobs.append(
                {
                    "job_id": job_id,
                    "group_index": group_index,
                    "state": data.get("state", ""),
                    "vm_name": data.get("vm_name", ""),
                    "source_vmid": int(data.get("source_vmid") or 0),
                    "proxmox_vmid": int(data.get("proxmox_vmid") or 0),
                    "progress": int(data.get("progress") or 0),
                    "error": data.get("error", ""),
                }
            )
    out = {**run}
    # Do not dump full pending backup rows in list responses by default.
    out.pop("pending_group_rows", None)
    out["jobs"] = jobs
    out["job_count"] = len(jobs)
    out["completed_jobs"] = sum(1 for j in jobs if j["state"] == RestoreState.COMPLETED.value)
    out["failed_jobs"] = sum(1 for j in jobs if j["state"] == RestoreState.FAILED.value)
    return out
