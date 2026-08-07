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


def _safe_qga_sec(raw: Any) -> int:
    try:
        return max(0, min(3600, int(raw or 0)))
    except (TypeError, ValueError):
        return 0


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
    raw_nodes = payload.get("nodes")
    nodes: list[str] = []
    if isinstance(raw_nodes, list):
        for item in raw_nodes:
            n = str(item).strip()
            if n and n not in nodes:
                nodes.append(n)
    if nodes:
        if node:
            nodes = [node] + [n for n in nodes if n != node]
        else:
            node = nodes[0]
    elif node:
        nodes = [node]
    else:
        raise ValueError("node (or nodes) is required")
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
    mode = str(payload.get("restore_mode") or "normal").strip().lower()
    if mode not in {"normal", "dr"}:
        raise ValueError("restore_mode must be 'normal' or 'dr'")
    storage_by_node: dict[str, str] = {}
    raw_sbn = payload.get("storage_by_node")
    if isinstance(raw_sbn, dict):
        for key, val in raw_sbn.items():
            n = str(key).strip()
            s = str(val).strip()
            if n and s:
                storage_by_node[n] = s
    for n in nodes:
        if n not in storage_by_node and storage:
            storage_by_node[n] = storage
    missing = [n for n in nodes if n not in storage_by_node]
    if missing:
        raise ValueError(f"storage required for node(s): {', '.join(missing)}")
    storage = storage_by_node.get(node) or storage
    if not storage:
        raise ValueError("storage is required")
    return {
        "id": location_id or str(uuid.uuid4()),
        "name": name,
        "node": node,
        "nodes": nodes,
        "storage": storage,
        "storage_by_node": storage_by_node,
        "vmid_start": vmid_start,
        "bwlimit": bwlimit,
        "live_restore": bool(payload.get("live_restore", False)),
        "restore_mode": mode,
        "power_on": bool(payload.get("power_on", False)),
        "qga_wait_sec": _safe_qga_sec(payload.get("qga_wait_sec")),
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
        "last_check": payload.get("last_check") if isinstance(payload.get("last_check"), dict) else {},
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
    if "last_check" in merged and isinstance(merged.get("last_check"), dict):
        data["last_check"] = merged["last_check"]
    elif isinstance(existing.get("last_check"), dict):
        data["last_check"] = existing["last_check"]
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


# --- Readiness checks ---


def _check_item(level: str, code: str, message: str, detail: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"level": level, "code": code, "message": message}
    if detail:
        item["detail"] = detail
    return item


def _default_cutoff(value: str | None) -> str:
    """Match main.normalize_cutoff empty → no upper bound."""
    v = (value or "").strip()
    if not v:
        return "9999-12-31T23:59:59Z"
    if len(v) == 10:
        return f"{v}T23:59:59Z"
    if len(v) == 16:
        return f"{v}:59Z"
    if not v.endswith("Z"):
        return f"{v}Z"
    return v


def _tag_cache_key(cfg: dict[str, Any], volid: str) -> str:
    prefix = _redis_cfg(cfg).get("tag_cache_prefix", "restore:tagcache:")
    return f"{prefix}{volid}"


def _resolve_tags_cached(
    r: redis.Redis,
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    node: str,
    proxmox: Any,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Resolve guest tags using Redis cache + PVE extractconfig (worker-safe)."""
    from pve_client import archive_path, extract_vm_config, parse_tags

    result: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for row in rows:
        try:
            volid = archive_path(row["pve_storage"], row["voltail"])
        except Exception as exc:
            errors[str(row.get("backup_id") or "")] = str(exc)
            continue
        cached = r.get(_tag_cache_key(cfg, volid))
        if cached is not None:
            result[row["backup_id"]] = [t for t in cached.split(";") if t]
            continue
        try:
            text = extract_vm_config(proxmox, node, volid)
            tags = parse_tags(text)
            r.set(_tag_cache_key(cfg, volid), ";".join(tags))
            result[row["backup_id"]] = tags
        except Exception as exc:
            errors[row["backup_id"]] = str(exc)
    return result, errors


def _parse_check_iso(ts: str) -> datetime | None:
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def plans_due_for_check(
    plans: list[dict[str, Any]],
    *,
    interval_sec: float,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return enabled plans whose last_check_at is missing or older than interval."""
    clock = now or datetime.now(timezone.utc)
    due: list[dict[str, Any]] = []
    for plan in plans:
        if not plan.get("enabled", True):
            continue
        last = _parse_check_iso(str(plan.get("last_check_at") or ""))
        if last is None:
            due.append(plan)
            continue
        age = (clock - last).total_seconds()
        if age >= float(interval_sec):
            due.append(plan)
    return due


def apply_check_result(
    r: redis.Redis,
    cfg: dict[str, Any],
    plan: dict[str, Any],
    check: dict[str, Any],
) -> dict[str, Any]:
    """Persist check result and verification on the plan."""
    ok = bool(check.get("ok"))
    updated = {
        **plan,
        "last_check": check,
        "last_check_at": str(check.get("checked_at") or utc_now_iso()),
        "verification": PlanVerification.VERIFIED.value if ok else PlanVerification.NOT_VERIFIED.value,
    }
    saved = _save_entity(
        r,
        cfg,
        key=plan_key(cfg, plan["id"]),
        index=plans_index(cfg),
        entity_id=plan["id"],
        data=updated,
    )
    try:
        import reports as reports_module

        rep = reports_module.save_check_report(r, cfg, plan=saved, check=check)
        saved = _save_entity(
            r,
            cfg,
            key=plan_key(cfg, plan["id"]),
            index=plans_index(cfg),
            entity_id=plan["id"],
            data={**saved, "last_check_report_id": rep["id"]},
        )
    except Exception:
        pass
    return saved


def run_plan_readiness(
    r: redis.Redis,
    cfg: dict[str, Any],
    plan: dict[str, Any],
    *,
    cutoff: str | None = None,
    list_backups_fn: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
    probe_pbs_fn: Callable[[dict[str, Any]], tuple[bool, list[dict[str, Any]]]] | None = None,
    test_pve_fn: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
    connect_pve_fn: Callable[[dict[str, Any]], Any] | None = None,
    list_nodes_fn: Callable[[Any], list[dict[str, Any]]] | None = None,
    list_storages_fn: Callable[[Any, str], list[dict[str, Any]]] | None = None,
    vmids_in_use_fn: Callable[[Any], set[int]] | None = None,
    resolve_tags_fn: Callable[..., tuple[dict[str, list[str]], dict[str, str]]] | None = None,
    persist: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run fail-closed readiness checks. Returns ``(plan, check)``.

    When ``persist`` is True (default), updates plan verification / last_check_*.
    """
    from pbs_client import list_vm_backups, probe_all_sources
    from pve_client import (
        allocate_sequential_free_vmids,
        connect_proxmox,
        list_cluster_nodes,
        list_node_storages,
        qemu_vmids_in_use_cluster,
        resolve_storage_for_node,
        test_proxmox_connection,
    )

    if list_backups_fn is None:
        list_backups_fn = list_vm_backups
    if probe_pbs_fn is None:
        probe_pbs_fn = probe_all_sources
    if test_pve_fn is None:
        test_pve_fn = test_proxmox_connection
    if connect_pve_fn is None:
        connect_pve_fn = connect_proxmox
    if list_nodes_fn is None:
        list_nodes_fn = list_cluster_nodes
    if list_storages_fn is None:
        list_storages_fn = list_node_storages
    if vmids_in_use_fn is None:
        vmids_in_use_fn = qemu_vmids_in_use_cluster

    checked_at = utc_now_iso()
    cutoff_norm = _default_cutoff(cutoff)
    items: list[dict[str, Any]] = []
    errors = 0

    def fail(code: str, message: str, detail: str | None = None) -> None:
        nonlocal errors
        errors += 1
        items.append(_check_item("error", code, message, detail))

    def ok_item(code: str, message: str, detail: str | None = None) -> None:
        items.append(_check_item("ok", code, message, detail))

    def warn(code: str, message: str, detail: str | None = None) -> None:
        items.append(_check_item("warn", code, message, detail))

    group_ids = list(plan.get("group_ids") or [])
    if not group_ids:
        fail("plan.groups", "Plan has no inventory groups")
    location_id = (plan.get("location_id") or "").strip()
    location = get_location(r, cfg, location_id) if location_id else None
    if not location:
        fail("plan.location", f"Location not found: {location_id or '(empty)'}")

    groups: list[dict[str, Any]] = []
    for gid in group_ids:
        group = get_group(r, cfg, gid)
        if not group:
            fail("plan.group_missing", f"Missing group in plan: {gid}")
        else:
            groups.append(group)

    # PBS connectivity
    try:
        pbs_ok, pbs_results = probe_pbs_fn(cfg)
    except Exception as exc:
        pbs_ok, pbs_results = False, [{"ok": False, "detail": str(exc)}]
    if not pbs_ok:
        detail = "; ".join(
            f"{r.get('label') or r.get('source_id') or '?'}: {r.get('detail')}"
            for r in (pbs_results or [])
            if not r.get("ok")
        )
        fail("pbs.connectivity", "One or more PBS sources unreachable", detail or None)
    else:
        ok_item("pbs.connectivity", f"{len(pbs_results or [])} PBS source(s) reachable")

    # PVE connectivity
    proxmox = None
    try:
        pve_ok, pve_msg = test_pve_fn(cfg)
    except Exception as exc:
        pve_ok, pve_msg = False, str(exc)
    if not pve_ok:
        fail("pve.connectivity", "Proxmox VE unreachable", pve_msg)
    else:
        ok_item("pve.connectivity", pve_msg or "Proxmox VE reachable")
        try:
            proxmox = connect_pve_fn(cfg)
        except Exception as exc:
            fail("pve.connect", "Failed to open Proxmox API session", str(exc))
            proxmox = None

    # Location nodes + storage
    member_count = 0
    group_rows: list[list[dict[str, Any]]] = []
    if location and proxmox is not None:
        try:
            nodes_info = list_nodes_fn(proxmox)
            known = {str(n.get("node") or n.get("name") or "").strip() for n in nodes_info}
            known.discard("")
        except Exception as exc:
            fail("pve.nodes", "Failed to list cluster nodes", str(exc))
            known = set()

        loc_nodes = list(location.get("nodes") or [])
        if not loc_nodes and location.get("node"):
            loc_nodes = [location["node"]]
        for node in loc_nodes:
            if known and node not in known:
                fail("pve.node_missing", f"Location node not in cluster: {node}")
            else:
                try:
                    storages = list_storages_fn(proxmox, node)
                    by_id = {s["id"]: s for s in storages}
                    try:
                        sid = resolve_storage_for_node(
                            node,
                            storage_by_node=dict(location.get("storage_by_node") or {}),
                            default_storage=str(location.get("storage") or ""),
                        )
                    except Exception as exc:
                        fail("pve.storage_map", f"No storage mapped for node {node}", str(exc))
                        continue
                    meta = by_id.get(sid)
                    if not meta:
                        fail("pve.storage_missing", f"Storage {sid!r} not found on node {node}")
                    elif not meta.get("usable_for_vm_disks"):
                        fail("pve.storage_unusable", f"Storage {sid!r} on {node} cannot hold VM disks")
                    elif not meta.get("enabled", True):
                        fail("pve.storage_disabled", f"Storage {sid!r} on {node} is disabled")
                    else:
                        ok_item("pve.storage", f"{node}: {sid} usable for VM disks")
                except Exception as exc:
                    fail("pve.storage_list", f"Failed listing storages on {node}", str(exc))

        # Resolve members
        try:
            backups = list_backups_fn(cfg)
        except Exception as exc:
            backups = []
            fail("pbs.backups", "Failed to list PBS backups", str(exc))

        tags_by_id: dict[str, list[str]] = {}
        need_tags = any(g.get("tags") for g in groups)
        if need_tags and backups:
            # Latest-per-vmid under cutoff as tag candidates (same as run path).
            best: dict[int, dict[str, Any]] = {}
            for row in backups:
                if row.get("timestamp", "") > cutoff_norm:
                    continue
                vmid = int(row["vmid"])
                cur = best.get(vmid)
                if cur is None or row["timestamp"] > cur["timestamp"]:
                    best[vmid] = row
            candidates = list(best.values())
            node0 = loc_nodes[0] if loc_nodes else str(location.get("node") or "")
            resolver = resolve_tags_fn
            if resolver is None:
                resolver = lambda c, rows, node: _resolve_tags_cached(r, c, rows, node, proxmox)
            if node0:
                try:
                    tags_by_id, tag_errors = resolver(cfg, candidates, node0)
                    if tag_errors:
                        warn(
                            "tags.resolve",
                            f"Tag resolve failed for {len(tag_errors)} backup(s)",
                            "; ".join(f"{k}: {v}" for k, v in list(tag_errors.items())[:5]),
                        )
                except Exception as exc:
                    fail("tags.resolve", "Failed to resolve guest tags for plan groups", str(exc))
            else:
                warn("tags.resolve", "Tag resolution skipped (no location node)")

        for group in groups:
            rows = resolve_group_rows(group, backups, cutoff=cutoff_norm, tags_by_backup_id=tags_by_id)
            group_rows.append(rows)
            if not rows:
                fail(
                    "group.empty",
                    f"Group {group.get('name') or group.get('id')} resolved to zero backups",
                )
            else:
                member_count += len(rows)
                ok_item(
                    "group.members",
                    f"Group {group.get('name') or group.get('id')}: {len(rows)} VM(s)",
                )

        if member_count > 0:
            mode = str(location.get("restore_mode") or "normal").strip().lower() or "normal"
            try:
                in_use = set(vmids_in_use_fn(proxmox))
            except Exception as exc:
                in_use = set()
                fail("pve.vmids", "Failed to list cluster VMIDs in use", str(exc))

            if mode == "dr":
                conflicts: list[int] = []
                seen_src: set[int] = set()
                for rows in group_rows:
                    for row in rows:
                        src = int(row["vmid"])
                        if src in seen_src:
                            fail("vmid.dr_duplicate", f"Duplicate source VMID in plan batch: {src}")
                        seen_src.add(src)
                        if src in in_use:
                            conflicts.append(src)
                if conflicts:
                    fail(
                        "vmid.dr_in_use",
                        "DR mode: source VMID(s) already exist on cluster",
                        ", ".join(str(v) for v in conflicts[:20]),
                    )
                else:
                    ok_item("vmid.dr", f"DR: {member_count} source VMID(s) free on cluster")
            else:
                try:
                    allocated, _ = allocate_sequential_free_vmids(
                        set(in_use),
                        int(location.get("vmid_start") or 100),
                        member_count,
                    )
                    ok_item(
                        "vmid.normal",
                        f"Normal: can allocate {len(allocated)} free VMID(s) from {location.get('vmid_start')}",
                    )
                except Exception as exc:
                    fail("vmid.normal", "Cannot allocate enough free VMIDs for plan members", str(exc))

    ok = errors == 0
    if ok and member_count == 0 and location and groups:
        # Groups existed but resolved nothing and we may have already recorded errors.
        if not any(i["code"] == "group.empty" for i in items):
            fail("plan.empty", "Plan resolved to zero backups")
    ok = errors == 0

    summary = (
        f"Readiness OK ({member_count} VM(s))"
        if ok
        else f"Readiness FAILED ({errors} error(s), {member_count} VM(s) resolved)"
    )
    check: dict[str, Any] = {
        "ok": ok,
        "checked_at": checked_at,
        "cutoff": cutoff_norm,
        "summary": summary,
        "member_count": member_count,
        "items": items,
    }
    if persist:
        plan = apply_check_result(r, cfg, plan, check)
    return plan, check


def tick_plan_readiness(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    interval_sec: float | None = None,
    now: datetime | None = None,
    on_error: Callable[[dict[str, Any], BaseException], None] | None = None,
) -> int:
    """Run readiness for enabled plans due for a check. Returns number checked."""
    worker_cfg = cfg.get("worker") or {}
    if interval_sec is None:
        try:
            interval_sec = float(worker_cfg.get("plan_check_interval_sec", 86400))
        except (TypeError, ValueError):
            interval_sec = 86400.0
    if interval_sec <= 0:
        return 0
    due = plans_due_for_check(list_plans(r, cfg), interval_sec=interval_sec, now=now)
    checked = 0
    for plan in due:
        try:
            run_plan_readiness(r, cfg, plan, cutoff=None, persist=True)
            checked += 1
        except Exception as exc:
            # Avoid retrying every worker loop second after a hard failure.
            try:
                apply_check_result(
                    r,
                    cfg,
                    plan,
                    {
                        "ok": False,
                        "checked_at": utc_now_iso(),
                        "cutoff": _default_cutoff(None),
                        "summary": f"Readiness check crashed: {exc}",
                        "member_count": 0,
                        "items": [
                            _check_item("error", "check.crash", "Readiness check crashed", str(exc))
                        ],
                    },
                )
            except Exception:
                pass
            if on_error is not None:
                on_error(plan, exc)
            else:
                raise
            checked += 1
    return checked


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


def require_verified_to_run(cfg: dict[str, Any]) -> bool:
    plans_cfg = cfg.get("plans") or {}
    worker_cfg = cfg.get("worker") or {}
    return bool(plans_cfg.get("require_verified_to_run", worker_cfg.get("require_verified_to_run", False)))


def _record_assigned_targets(run: dict[str, Any], result: dict[str, Any]) -> None:
    """Append VMIDs/nodes from an enqueue result onto the plan run for teardown."""
    targets = list(run.get("assigned_targets") or [])
    vmids = list(result.get("proxmox_vmids_assigned") or [])
    nodes = list(result.get("proxmox_nodes_assigned") or [])
    for i, vmid in enumerate(vmids):
        try:
            vid = int(vmid)
        except (TypeError, ValueError):
            continue
        node = ""
        if i < len(nodes):
            node = str(nodes[i] or "").strip()
        targets.append({"vmid": vid, "node": node})
    run["assigned_targets"] = targets
    assigned_ids = [int(t["vmid"]) for t in targets if t.get("vmid")]
    if assigned_ids:
        run["next_vmid"] = max(assigned_ids) + 1


def start_plan_run(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    plan: dict[str, Any],
    location: dict[str, Any],
    cutoff: str,
    group_rows: list[list[dict[str, Any]]],
    enqueue_fn: Callable[..., dict[str, Any]],
    drill: bool = False,
    auto_teardown: bool = False,
    powered_off: bool | None = None,
    power_on: bool = False,
    qga_wait_sec: int = 0,
) -> dict[str, Any]:
    """Create a plan run and enqueue the first non-empty group.

    Drill runs default to powered-off restores unless ``power_on`` (or location
    ``power_on``) is set — for isolated DR sites that can boot guests safely.
    ``qga_wait_sec`` waits for QEMU guest agent after power-on; timeout fails the job.
    ``auto_teardown`` destroys restored VMs when the run becomes terminal (drills).
    """
    if len(group_rows) != len(plan["group_ids"]):
        raise ValueError("group_rows length must match plan.group_ids")
    if not any(group_rows):
        raise ValueError("plan resolved to zero backups")

    run_id = str(uuid.uuid4())
    now = utc_now_iso()
    job_ids_by_group: list[list[str]] = [[] for _ in plan["group_ids"]]
    location_nodes = list(location.get("nodes") or [])
    if not location_nodes:
        location_nodes = [location["node"]]
    is_drill = bool(drill)

    do_power_on = bool(power_on) or bool(location.get("power_on", False))
    try:
        qga_sec = max(0, int(qga_wait_sec or 0))
    except (TypeError, ValueError):
        qga_sec = 0
    if qga_sec <= 0 and do_power_on:
        try:
            qga_sec = max(0, int(location.get("qga_wait_sec") or 0))
        except (TypeError, ValueError):
            qga_sec = 0
    if qga_sec > 0:
        do_power_on = True

    # Drills default powered-off; explicit power-on wins (isolated env).
    if powered_off is None:
        powered_off = is_drill and not do_power_on
    if do_power_on:
        powered_off = False
    live = bool(location.get("live_restore", False)) and not bool(powered_off)

    run = {
        "id": run_id,
        "plan_id": plan["id"],
        "plan_name": plan.get("name", ""),
        "cutoff": cutoff,
        "status": PlanRunStatus.RUNNING.value,
        "location_id": location["id"],
        "location_name": location.get("name", ""),
        "node": location["node"],
        "nodes": location_nodes,
        "storage": location["storage"],
        "storage_by_node": dict(location.get("storage_by_node") or {}),
        "vmid_start": location["vmid_start"],
        "bwlimit": location.get("bwlimit", 0),
        "live_restore": live,
        "powered_off": bool(powered_off),
        "power_on": do_power_on,
        "qga_wait_sec": qga_sec,
        "drill": is_drill,
        "auto_teardown": bool(auto_teardown) and is_drill,
        "restore_mode": str(location.get("restore_mode") or "normal").strip().lower() or "normal",
        "halt_on_error": bool(plan.get("halt_on_error", True)),
        "group_ids": list(plan["group_ids"]),
        "job_ids_by_group": job_ids_by_group,
        "assigned_targets": [],
        "teardown_status": "",
        "teardown_results": [],
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
        nodes=location_nodes,
        target_storage=location["storage"],
        storage_by_node=dict(location.get("storage_by_node") or {}),
        vmid_start=int(location["vmid_start"]),
        live_restore=live,
        bwlimit=int(location.get("bwlimit", 0) or 0),
        restore_mode=str(location.get("restore_mode") or "normal"),
        plan_run_id=run_id,
        plan_group_index=start_idx,
        power_on=do_power_on,
        qga_wait_sec=qga_sec,
    )
    job_ids_by_group[start_idx] = list(result.get("job_ids") or [])
    run["job_ids_by_group"] = job_ids_by_group
    _record_assigned_targets(run, result)
    if not run.get("next_vmid"):
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
                "size_bytes": int(row.get("size_bytes") or 0),
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
            _finalize_plan_run_report(r, cfg, run, job_key_fn=job_key_fn)
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
            run_nodes = list(run.get("nodes") or [])
            if not run_nodes and run.get("node"):
                run_nodes = [run["node"]]
            result = enqueue_fn(
                r,
                cfg,
                rows,
                node=run.get("node") or (run_nodes[0] if run_nodes else ""),
                nodes=run_nodes,
                target_storage=run["storage"],
                storage_by_node=dict(run.get("storage_by_node") or {}),
                vmid_start=vmid_start,
                live_restore=bool(run.get("live_restore", False)),
                bwlimit=int(run.get("bwlimit", 0) or 0),
                restore_mode=str(run.get("restore_mode") or "normal"),
                plan_run_id=run_id,
                plan_group_index=idx,
                power_on=bool(run.get("power_on", False)),
                qga_wait_sec=int(run.get("qga_wait_sec") or 0),
            )
            job_ids_by_group[idx] = list(result.get("job_ids") or [])
            run["job_ids_by_group"] = job_ids_by_group
            _record_assigned_targets(run, result)
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
            _finalize_plan_run_report(r, cfg, run, job_key_fn=job_key_fn)
            touched += 1
            continue
        if cancelled and not any(s == RestoreState.COMPLETED.value for s in states) and all(
            s in {RestoreState.CANCELLED.value, RestoreState.FAILED.value} for s in states
        ):
            run["status"] = PlanRunStatus.CANCELLED.value
            run["finished_at"] = utc_now_iso()
            save_plan_run(r, cfg, run)
            _finalize_plan_run_report(r, cfg, run, job_key_fn=job_key_fn)
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
            _finalize_plan_run_report(r, cfg, run, job_key_fn=job_key_fn)
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
                    "archive": data.get("archive", ""),
                    "backup_id": data.get("backup_id", ""),
                    "power_on": data.get("power_on", "0") == "1",
                    "qga_ok": data.get("qga_ok", ""),
                    "qga_waited_sec": data.get("qga_waited_sec", ""),
                    "restore_started_at": data.get("restore_started_at", ""),
                    "updated_at": data.get("updated_at", ""),
                }
            )
    out = {**run}
    # Do not dump full pending backup rows in list responses by default.
    out.pop("pending_group_rows", None)
    out["jobs"] = jobs
    out["job_count"] = len(jobs)
    out["completed_jobs"] = sum(1 for j in jobs if j["state"] == RestoreState.COMPLETED.value)
    out["failed_jobs"] = sum(1 for j in jobs if j["state"] == RestoreState.FAILED.value)
    try:
        import reports as reports_module

        rto = reports_module.wall_clock_rto_sec(
            str(run.get("started_at") or ""), str(run.get("finished_at") or "")
        )
        out["rto_sec"] = rto
        out["rto"] = reports_module.format_duration(rto)
    except Exception:
        out["rto_sec"] = None
        out["rto"] = "—"
    return out


def _finalize_plan_run_report(
    r: redis.Redis,
    cfg: dict[str, Any],
    run: dict[str, Any],
    *,
    job_key_fn: Callable | None = None,
) -> dict[str, Any]:
    """Generate and attach a compliance report when a plan run becomes terminal."""
    if run.get("report_id"):
        if run.get("auto_teardown") and not run.get("teardown_status"):
            try:
                teardown_plan_run(r, cfg, run, job_key_fn=job_key_fn)
            except Exception:
                pass
        return run
    try:
        import reports as reports_module

        aggregated = aggregate_plan_run(r, cfg, run, job_key_fn=job_key_fn)
        plan = get_plan(r, cfg, str(run.get("plan_id") or ""))
        rep = reports_module.save_run_report(r, cfg, plan=plan, run=aggregated)
        run = {**run, "report_id": rep["id"], "rto_sec": (rep.get("meta") or {}).get("rto_sec")}
        save_plan_run(r, cfg, run)
        if plan:
            _save_entity(
                r,
                cfg,
                key=plan_key(cfg, plan["id"]),
                index=plans_index(cfg),
                entity_id=plan["id"],
                data={**plan, "last_run_report_id": rep["id"]},
            )
    except Exception:
        pass
    if run.get("auto_teardown") and not run.get("teardown_status"):
        try:
            teardown_plan_run(r, cfg, run, job_key_fn=job_key_fn)
        except Exception:
            pass
    return run


def collect_plan_run_targets(
    r: redis.Redis,
    cfg: dict[str, Any],
    run: dict[str, Any],
    *,
    job_key_fn: Callable | None = None,
) -> list[dict[str, Any]]:
    """Unique (vmid, node) targets restored by this plan run."""
    from jobs import job_key as default_job_key

    if job_key_fn is None:
        job_key_fn = default_job_key

    by_vmid: dict[int, str] = {}
    for t in run.get("assigned_targets") or []:
        try:
            vid = int(t.get("vmid"))
        except (TypeError, ValueError, AttributeError):
            continue
        if vid <= 0:
            continue
        node = str((t or {}).get("node") or "").strip()
        if vid not in by_vmid or (node and not by_vmid[vid]):
            by_vmid[vid] = node

    for group_ids in run.get("job_ids_by_group") or []:
        for job_id in group_ids or []:
            data = r.hgetall(job_key_fn(cfg, job_id)) or {}
            try:
                vid = int(data.get("proxmox_vmid") or 0)
            except (TypeError, ValueError):
                continue
            if vid <= 0:
                continue
            node = str(data.get("proxmox_node") or "").strip()
            if vid not in by_vmid or (node and not by_vmid[vid]):
                by_vmid[vid] = node

    return [{"vmid": vid, "node": node} for vid, node in sorted(by_vmid.items())]


def teardown_plan_run(
    r: redis.Redis,
    cfg: dict[str, Any],
    run: dict[str, Any] | str,
    *,
    job_key_fn: Callable | None = None,
    destroy_fn: Callable[..., None] | None = None,
    find_node_fn: Callable[..., str | None] | None = None,
    connect_fn: Callable | None = None,
) -> dict[str, Any]:
    """Destroy QEMU VMs created by a plan run. Idempotent when already torn down."""
    from jobs import job_key as default_job_key
    from pve_client import connect_proxmox, destroy_qemu_vm, find_qemu_node

    if job_key_fn is None:
        job_key_fn = default_job_key
    if isinstance(run, str):
        loaded = get_plan_run(r, cfg, run)
        if not loaded:
            raise ValueError(f"Plan run not found: {run}")
        run = loaded

    if run.get("teardown_status") == "completed":
        return run

    if run.get("status") == PlanRunStatus.RUNNING.value:
        raise ValueError("Cannot teardown a RUNNING plan run; cancel or wait for it to finish")

    targets = collect_plan_run_targets(r, cfg, run, job_key_fn=job_key_fn)
    results: list[dict[str, Any]] = []
    proxmox = None
    if targets:
        connect = connect_fn or connect_proxmox
        proxmox = connect(cfg)

    destroy = destroy_fn or destroy_qemu_vm
    find_node = find_node_fn or find_qemu_node

    for t in targets:
        vmid = int(t["vmid"])
        node = str(t.get("node") or "").strip()
        entry: dict[str, Any] = {"vmid": vmid, "node": node, "ok": False, "error": ""}
        try:
            if not node and proxmox is not None:
                node = find_node(proxmox, vmid) or ""
                entry["node"] = node
            if not node:
                entry["error"] = "node unknown"
                results.append(entry)
                continue
            destroy(proxmox, node, vmid)
            entry["ok"] = True
        except Exception as exc:
            # Missing VM is success for teardown (already gone).
            msg = str(exc)
            lower = msg.lower()
            if "does not exist" in lower or "no such" in lower or "not found" in lower:
                entry["ok"] = True
                entry["error"] = "already gone"
            else:
                entry["error"] = msg
        results.append(entry)

    ok_count = sum(1 for x in results if x.get("ok"))
    if not results:
        status = "completed"
    elif ok_count == len(results):
        status = "completed"
    elif ok_count == 0:
        status = "failed"
    else:
        status = "partial"

    run = {
        **run,
        "teardown_status": status,
        "teardown_results": results,
        "teardown_at": utc_now_iso(),
    }
    save_plan_run(r, cfg, run)
    return run


def cancel_plan_run(
    r: redis.Redis,
    cfg: dict[str, Any],
    run_id: str,
    *,
    job_key_fn: Callable | None = None,
) -> dict[str, Any]:
    """Stop advancing a plan run: cancel PENDING jobs, request cancel on RESTORING."""
    from jobs import job_key as default_job_key

    if job_key_fn is None:
        job_key_fn = default_job_key

    run = get_plan_run(r, cfg, run_id)
    if not run:
        raise ValueError(f"Plan run not found: {run_id}")
    if run.get("status") != PlanRunStatus.RUNNING.value:
        return aggregate_plan_run(r, cfg, run, job_key_fn=job_key_fn)

    queue_key = cfg["redis"]["queue_key"]
    cancelled_pending = 0
    cancel_requested = 0
    for group_ids in run.get("job_ids_by_group") or []:
        for job_id in group_ids or []:
            key = job_key_fn(cfg, job_id)
            data = r.hgetall(key) or {}
            if not data:
                continue
            state = data.get("state") or RestoreState.PENDING.value
            if state == RestoreState.PENDING.value:
                pipe = r.pipeline(transaction=True)
                pipe.hset(
                    key,
                    mapping={
                        "state": RestoreState.CANCELLED.value,
                        "updated_at": utc_now_iso(),
                        "error": "Cancelled with plan run",
                        "cancel_requested": "1",
                        "eta_sec": "",
                        "speed_bps": "",
                    },
                )
                pipe.lrem(queue_key, 0, job_id)
                pipe.execute()
                cancelled_pending += 1
            elif state == RestoreState.RESTORING.value:
                r.hset(key, mapping={"cancel_requested": "1", "updated_at": utc_now_iso()})
                cancel_requested += 1

    run["pending_group_rows"] = []
    run["status"] = PlanRunStatus.CANCELLED.value
    run["finished_at"] = utc_now_iso()
    run["error"] = run.get("error") or "Cancelled by operator"
    run["cancel_stats"] = {
        "cancelled_pending": cancelled_pending,
        "cancel_requested_restoring": cancel_requested,
    }
    save_plan_run(r, cfg, run)
    _finalize_plan_run_report(r, cfg, run, job_key_fn=job_key_fn)
    return aggregate_plan_run(r, cfg, get_plan_run(r, cfg, run_id) or run, job_key_fn=job_key_fn)
