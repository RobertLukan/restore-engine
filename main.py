"""FastAPI API for PBS -> Proxmox VE restore orchestration."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis
import yaml
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

import ui as ui_module
import plans as plans_module
from jobs import enqueue_restores, job_key
from pbs_client import list_vm_backups
from pve_client import (
    archive_path,
    connect_proxmox,
    extract_vm_config,
    parse_tags,
    qemu_vmids_in_use_on_node,
)
from states import RestoreState
from ui import require_ui_session, router as ui_router

_cfg_path_override = (os.environ.get("RESTORE_ENGINE_CONFIG") or "").strip()
CONFIG_PATH = (
    Path(_cfg_path_override).expanduser()
    if _cfg_path_override
    else Path(__file__).resolve().parent / "config.yaml"
)
STATIC_DIR = Path(__file__).resolve().parent / "static"

ui_module.CONFIG_PATH = CONFIG_PATH


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def redis_client() -> redis.Redis:
    cfg = load_config()
    return redis.from_url(cfg["redis"]["url"], decode_responses=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tag_cache_key(cfg: dict[str, Any], volid: str) -> str:
    prefix = cfg["redis"].get("tag_cache_prefix", "restore:tagcache:")
    return f"{prefix}{volid}"


def normalize_cutoff(value: str | None) -> str:
    """Normalize a date / datetime-local string into a comparable ISO ``...Z`` string.

    Snapshot timestamps are fixed-format ``YYYY-MM-DDTHH:MM:SSZ`` so lexicographic
    comparison is valid. Empty means "no upper bound".
    """
    v = (value or "").strip()
    if not v:
        return "9999-12-31T23:59:59Z"
    if len(v) == 10:  # YYYY-MM-DD -> end of that day
        return f"{v}T23:59:59Z"
    if len(v) == 16:  # datetime-local "YYYY-MM-DDTHH:MM"
        return f"{v}:59Z"
    if not v.endswith("Z"):
        return f"{v}Z"
    return v


def _resolve_tags(
    cfg: dict[str, Any], rows: list[dict[str, Any]], node: str, *, force: bool = False
) -> dict[str, list[str]]:
    """Resolve guest tags for the given backup rows, caching per (immutable) volid."""
    r = redis_client()
    result: dict[str, list[str]] = {}
    pending: list[dict[str, Any]] = []
    for row in rows:
        volid = archive_path(row["pve_storage"], row["voltail"])
        row["_volid"] = volid
        if not force:
            cached = r.get(tag_cache_key(cfg, volid))
            if cached is not None:
                result[row["backup_id"]] = [t for t in cached.split(";") if t]
                continue
        pending.append(row)

    if pending:
        proxmox = connect_proxmox(cfg)

        def fetch(row: dict[str, Any]) -> tuple[str, str, list[str], bool]:
            try:
                text = extract_vm_config(proxmox, node, row["_volid"])
                return row["backup_id"], row["_volid"], parse_tags(text), True
            except Exception:
                return row["backup_id"], row["_volid"], [], False

        with ThreadPoolExecutor(max_workers=5) as pool:
            for backup_id, volid, tags, ok in pool.map(fetch, pending):
                if ok:
                    r.set(tag_cache_key(cfg, volid), ";".join(tags))
                result[backup_id] = tags
    return result


def _enqueue_restores(
    r: redis.Redis,
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    node: str,
    target_storage: str,
    vmid_start: int,
    live_restore: bool,
    bwlimit: int,
    plan_run_id: str = "",
    plan_group_index: int | None = None,
) -> dict[str, Any]:
    """API wrapper around jobs.enqueue_restores (maps errors to HTTP)."""
    try:
        return enqueue_restores(
            r,
            cfg,
            rows,
            node=node,
            target_storage=target_storage,
            vmid_start=vmid_start,
            live_restore=live_restore,
            bwlimit=bwlimit,
            plan_run_id=plan_run_id,
            plan_group_index=plan_group_index,
        )
    except RuntimeError as exc:
        msg = str(exc)
        code = 503 if msg.startswith("Cannot list QEMU") else 400
        raise HTTPException(status_code=code, detail=msg) from exc


class BackupItem(BaseModel):
    backup_id: str
    vmid: int
    name: str
    timestamp: str
    datastore: str


class RestoreSelectedRequest(BaseModel):
    backup_ids: list[str] = Field(min_length=1)
    proxmox_node: str | None = None
    proxmox_storage: str
    proxmox_vmid_start: int = Field(ge=100)
    live_restore: bool = False
    bwlimit: int = Field(default=0, ge=0)


class JobRecord(BaseModel):
    job_id: str
    state: str
    backup_id: str
    vm_name: str
    source_vmid: int
    source_label: str = ""
    proxmox_vmid: int
    proxmox_node: str
    proxmox_storage: str
    live_restore: bool
    bwlimit: int = 0
    progress: int = 0
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


api = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(require_ui_session)])


@api.get("/backups")
def list_backups() -> list[dict[str, Any]]:
    cfg = load_config()
    rows = list_vm_backups(cfg)
    # Attach tags only if already cached (no PVE calls here; resolve on demand).
    try:
        r = redis_client()
        for row in rows:
            cached = r.get(tag_cache_key(cfg, archive_path(row["pve_storage"], row["voltail"])))
            row["tags"] = [t for t in cached.split(";") if t] if cached is not None else None
    except Exception:
        for row in rows:
            row.setdefault("tags", None)
    return rows


@api.get("/restore-defaults")
def restore_defaults(proxmox_node: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    px = cfg.get("proxmox") or {}
    node = (proxmox_node or px.get("default_node") or "").strip()
    worker_cfg = cfg.get("worker") or {}
    out: dict[str, Any] = {
        "storage": px.get("storage", "local-lvm"),
        "default_node": px.get("default_node", ""),
        "next_free_proxmox_vmid": 100,
        "bwlimit": int(px.get("restore_bwlimit", 0) or 0),
        "live_restore": bool(px.get("live_restore_default", False)),
        "max_concurrent_restores": int(worker_cfg.get("max_concurrent_restores", 2) or 2),
    }
    if node:
        try:
            used = qemu_vmids_in_use_on_node(connect_proxmox(cfg), node)
            out["next_free_proxmox_vmid"] = max(max(used) + 1 if used else 100, 100)
        except Exception:
            pass
    return out


@api.post("/jobs/restore-selected")
def restore_selected(body: RestoreSelectedRequest) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    px = cfg.get("proxmox") or {}
    node = (body.proxmox_node or px.get("default_node") or "").strip()
    if not node:
        raise HTTPException(status_code=400, detail="proxmox.default_node or proxmox_node is required")

    target_storage = body.proxmox_storage.strip()
    if not target_storage:
        raise HTTPException(status_code=400, detail="proxmox_storage is required")

    backups = {row["backup_id"]: row for row in list_vm_backups(cfg)}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in body.backup_ids:
        bid = raw.strip()
        if not bid or bid in seen:
            continue
        seen.add(bid)
        row = backups.get(bid)
        if not row:
            raise HTTPException(status_code=400, detail=f"Backup not found: {bid}")
        ordered.append(row)
    if not ordered:
        raise HTTPException(status_code=400, detail="No valid backup IDs after deduplication")

    for row in ordered:
        if not (row.get("pve_storage") or "").strip():
            raise HTTPException(
                status_code=400,
                detail=f"Backup {row['backup_id']} has no PVE storage mapping for its PBS source",
            )

    return _enqueue_restores(
        r,
        cfg,
        ordered,
        node=node,
        target_storage=target_storage,
        vmid_start=body.proxmox_vmid_start,
        live_restore=body.live_restore,
        bwlimit=body.bwlimit,
    )


class ResolveTagsRequest(BaseModel):
    backup_ids: list[str] | None = None
    proxmox_node: str | None = None
    force: bool = False


@api.post("/backups/resolve-tags")
def resolve_backup_tags(body: ResolveTagsRequest) -> dict[str, Any]:
    cfg = load_config()
    px = cfg.get("proxmox") or {}
    node = (body.proxmox_node or px.get("default_node") or "").strip()
    if not node:
        raise HTTPException(status_code=400, detail="proxmox.default_node or proxmox_node is required")
    rows = list_vm_backups(cfg)
    if body.backup_ids:
        wanted = {b.strip() for b in body.backup_ids if b.strip()}
        rows = [row for row in rows if row["backup_id"] in wanted]
    tags_by_id = _resolve_tags(cfg, rows, node, force=body.force)
    all_tags = sorted({t for tags in tags_by_id.values() for t in tags}, key=str.lower)
    return {"tags": tags_by_id, "all_tags": all_tags}


class RestoreTagGroupRequest(BaseModel):
    tag: str = Field(min_length=1)
    at_or_before: str | None = None
    proxmox_node: str | None = None
    proxmox_storage: str
    proxmox_vmid_start: int = Field(ge=100)
    live_restore: bool = False
    bwlimit: int = Field(default=0, ge=0)


def _latest_per_vmid(rows: list[dict[str, Any]], cutoff: str) -> list[dict[str, Any]]:
    """Pick the newest snapshot per VMID whose timestamp is <= cutoff."""
    best: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row["timestamp"] > cutoff:
            continue
        current = best.get(row["vmid"])
        if current is None or row["timestamp"] > current["timestamp"]:
            best[row["vmid"]] = row
    return list(best.values())


@api.post("/jobs/restore-tag-group")
def restore_tag_group(body: RestoreTagGroupRequest) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    px = cfg.get("proxmox") or {}
    node = (body.proxmox_node or px.get("default_node") or "").strip()
    if not node:
        raise HTTPException(status_code=400, detail="proxmox.default_node or proxmox_node is required")
    target_storage = body.proxmox_storage.strip()
    if not target_storage:
        raise HTTPException(status_code=400, detail="proxmox_storage is required")

    cutoff = normalize_cutoff(body.at_or_before)
    candidates = _latest_per_vmid(list_vm_backups(cfg), cutoff)
    if not candidates:
        return {"enqueued": 0, "job_ids": [], "proxmox_vmids_assigned": [], "matched_vmids": []}

    tags_by_id = _resolve_tags(cfg, candidates, node)
    wanted = body.tag.strip().lower()
    selected = [
        row
        for row in candidates
        if wanted in {t.lower() for t in tags_by_id.get(row["backup_id"], [])}
    ]
    if not selected:
        return {"enqueued": 0, "job_ids": [], "proxmox_vmids_assigned": [], "matched_vmids": []}

    selected.sort(key=lambda row: row["vmid"])
    result = _enqueue_restores(
        r,
        cfg,
        selected,
        node=node,
        target_storage=target_storage,
        vmid_start=body.proxmox_vmid_start,
        live_restore=body.live_restore,
        bwlimit=body.bwlimit,
    )
    result["matched_vmids"] = [row["vmid"] for row in selected]
    return result


# --- Recovery orchestration (groups / locations / plans) ---


class GroupUpsert(BaseModel):
    name: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    vmids: list[int] = Field(default_factory=list)


class LocationUpsert(BaseModel):
    name: str = Field(min_length=1)
    node: str = Field(min_length=1)
    storage: str = Field(min_length=1)
    vmid_start: int = Field(default=100, ge=100)
    bwlimit: int = Field(default=0, ge=0)
    live_restore: bool = False


class PlanUpsert(BaseModel):
    name: str = Field(min_length=1)
    group_ids: list[str] = Field(min_length=1)
    location_id: str = Field(min_length=1)
    halt_on_error: bool = True
    enabled: bool = True


class PlanRunRequest(BaseModel):
    at_or_before: str | None = None
    location_id: str | None = None


def _http_value_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@api.get("/groups")
def api_list_groups() -> list[dict[str, Any]]:
    cfg = load_config()
    return plans_module.list_groups(redis_client(), cfg)


@api.post("/groups")
def api_create_group(body: GroupUpsert) -> dict[str, Any]:
    cfg = load_config()
    try:
        return plans_module.create_group(redis_client(), cfg, body.model_dump())
    except ValueError as exc:
        raise _http_value_error(exc) from exc


@api.get("/groups/{group_id}")
def api_get_group(group_id: str) -> dict[str, Any]:
    cfg = load_config()
    data = plans_module.get_group(redis_client(), cfg, group_id)
    if not data:
        raise HTTPException(status_code=404, detail="Group not found")
    return data


@api.put("/groups/{group_id}")
def api_update_group(group_id: str, body: GroupUpsert) -> dict[str, Any]:
    cfg = load_config()
    try:
        return plans_module.update_group(redis_client(), cfg, group_id, body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Group not found") from exc
    except ValueError as exc:
        raise _http_value_error(exc) from exc


@api.delete("/groups/{group_id}")
def api_delete_group(group_id: str) -> dict[str, str]:
    cfg = load_config()
    if not plans_module.delete_group(redis_client(), cfg, group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    return {"status": "deleted"}


@api.get("/locations")
def api_list_locations() -> list[dict[str, Any]]:
    cfg = load_config()
    return plans_module.list_locations(redis_client(), cfg)


@api.post("/locations")
def api_create_location(body: LocationUpsert) -> dict[str, Any]:
    cfg = load_config()
    try:
        return plans_module.create_location(redis_client(), cfg, body.model_dump())
    except ValueError as exc:
        raise _http_value_error(exc) from exc


@api.get("/locations/{location_id}")
def api_get_location(location_id: str) -> dict[str, Any]:
    cfg = load_config()
    data = plans_module.get_location(redis_client(), cfg, location_id)
    if not data:
        raise HTTPException(status_code=404, detail="Location not found")
    return data


@api.put("/locations/{location_id}")
def api_update_location(location_id: str, body: LocationUpsert) -> dict[str, Any]:
    cfg = load_config()
    try:
        return plans_module.update_location(redis_client(), cfg, location_id, body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Location not found") from exc
    except ValueError as exc:
        raise _http_value_error(exc) from exc


@api.delete("/locations/{location_id}")
def api_delete_location(location_id: str) -> dict[str, str]:
    cfg = load_config()
    if not plans_module.delete_location(redis_client(), cfg, location_id):
        raise HTTPException(status_code=404, detail="Location not found")
    return {"status": "deleted"}


@api.get("/plans")
def api_list_plans() -> list[dict[str, Any]]:
    cfg = load_config()
    return plans_module.list_plans(redis_client(), cfg)


@api.post("/plans")
def api_create_plan(body: PlanUpsert) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    for gid in body.group_ids:
        if not plans_module.get_group(r, cfg, gid):
            raise HTTPException(status_code=400, detail=f"Unknown group_id: {gid}")
    if not plans_module.get_location(r, cfg, body.location_id):
        raise HTTPException(status_code=400, detail=f"Unknown location_id: {body.location_id}")
    try:
        return plans_module.create_plan(r, cfg, body.model_dump())
    except ValueError as exc:
        raise _http_value_error(exc) from exc


@api.get("/plans/{plan_id}")
def api_get_plan(plan_id: str) -> dict[str, Any]:
    cfg = load_config()
    data = plans_module.get_plan(redis_client(), cfg, plan_id)
    if not data:
        raise HTTPException(status_code=404, detail="Plan not found")
    return data


@api.put("/plans/{plan_id}")
def api_update_plan(plan_id: str, body: PlanUpsert) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    for gid in body.group_ids:
        if not plans_module.get_group(r, cfg, gid):
            raise HTTPException(status_code=400, detail=f"Unknown group_id: {gid}")
    if not plans_module.get_location(r, cfg, body.location_id):
        raise HTTPException(status_code=400, detail=f"Unknown location_id: {body.location_id}")
    try:
        return plans_module.update_plan(r, cfg, plan_id, body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Plan not found") from exc
    except ValueError as exc:
        raise _http_value_error(exc) from exc


@api.delete("/plans/{plan_id}")
def api_delete_plan(plan_id: str) -> dict[str, str]:
    cfg = load_config()
    if not plans_module.delete_plan(redis_client(), cfg, plan_id):
        raise HTTPException(status_code=404, detail="Plan not found")
    return {"status": "deleted"}


def _resolve_plan_group_rows(
    cfg: dict[str, Any],
    plan: dict[str, Any],
    *,
    cutoff: str,
    node: str,
) -> list[list[dict[str, Any]]]:
    r = redis_client()
    backups = list_vm_backups(cfg)
    groups: list[dict[str, Any]] = []
    for gid in plan["group_ids"]:
        group = plans_module.get_group(r, cfg, gid)
        if not group:
            raise HTTPException(status_code=400, detail=f"Missing group in plan: {gid}")
        groups.append(group)

    need_tags = any(g.get("tags") for g in groups)
    tags_by_id: dict[str, list[str]] = {}
    if need_tags:
        # Resolve tags only for latest-per-vmid candidates under the cutoff.
        candidates = _latest_per_vmid(backups, cutoff)
        tags_by_id = _resolve_tags(cfg, candidates, node)

    return [
        plans_module.resolve_group_rows(group, backups, cutoff=cutoff, tags_by_backup_id=tags_by_id)
        for group in groups
    ]


@api.post("/plans/{plan_id}/run")
def api_run_plan(plan_id: str, body: PlanRunRequest) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    plan = plans_module.get_plan(r, cfg, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not plan.get("enabled", True):
        raise HTTPException(status_code=400, detail="Plan is disabled")

    location_id = (body.location_id or plan.get("location_id") or "").strip()
    location = plans_module.get_location(r, cfg, location_id)
    if not location:
        raise HTTPException(status_code=400, detail=f"Unknown location_id: {location_id}")

    cutoff = normalize_cutoff(body.at_or_before)
    group_rows = _resolve_plan_group_rows(cfg, plan, cutoff=cutoff, node=location["node"])
    if not any(group_rows):
        raise HTTPException(status_code=400, detail="Plan resolved to zero backups at this point in time")

    try:
        run = plans_module.start_plan_run(
            r,
            cfg,
            plan=plan,
            location=location,
            cutoff=cutoff,
            group_rows=group_rows,
            enqueue_fn=_enqueue_restores,
        )
    except ValueError as exc:
        raise _http_value_error(exc) from exc
    except HTTPException:
        raise
    return plans_module.aggregate_plan_run(r, cfg, run, job_key_fn=job_key)


@api.get("/plan-runs")
def api_list_plan_runs(plan_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    cfg = load_config()
    r = redis_client()
    runs = plans_module.list_plan_runs(r, cfg, plan_id=plan_id, limit=limit)
    return [plans_module.aggregate_plan_run(r, cfg, run, job_key_fn=job_key) for run in runs]


@api.get("/plan-runs/{run_id}")
def api_get_plan_run(run_id: str) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    run = plans_module.get_plan_run(r, cfg, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Plan run not found")
    return plans_module.aggregate_plan_run(r, cfg, run, job_key_fn=job_key)


def _hash_to_record(data: dict[str, str]) -> JobRecord:
    return JobRecord(
        job_id=data["job_id"],
        state=data.get("state", ""),
        backup_id=data.get("backup_id", ""),
        vm_name=data.get("vm_name", ""),
        source_vmid=int(data.get("source_vmid") or 0),
        source_label=data.get("source_label", ""),
        proxmox_vmid=int(data.get("proxmox_vmid") or 0),
        proxmox_node=data.get("proxmox_node", ""),
        proxmox_storage=data.get("proxmox_storage", ""),
        live_restore=data.get("live_restore", "0") == "1",
        bwlimit=int(data.get("bwlimit") or 0),
        progress=int(data.get("progress") or 0),
        error=data.get("error", ""),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
    )


@api.get("/jobs", response_model=list[JobRecord])
def list_jobs(state: str | None = None) -> list[JobRecord]:
    cfg = load_config()
    r = redis_client()
    prefix = cfg["redis"]["job_key_prefix"]
    out: list[JobRecord] = []
    for key in r.scan_iter(f"{prefix}*", count=100):
        if key.endswith(cfg["redis"]["job_log_suffix"]):
            continue
        data = r.hgetall(key)
        if not data:
            continue
        rec = _hash_to_record(data)
        if state is None or rec.state == state:
            out.append(rec)
    return sorted(out, key=lambda job: job.created_at or "")


@api.get("/jobs/{job_id}", response_model=JobRecord)
def get_job(job_id: str) -> JobRecord:
    cfg = load_config()
    r = redis_client()
    data = r.hgetall(job_key(cfg, job_id))
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    return _hash_to_record(data)


@api.post("/jobs/{job_id}/stop", response_model=JobRecord)
def stop_job(job_id: str) -> JobRecord:
    cfg = load_config()
    r = redis_client()
    key = job_key(cfg, job_id)
    data = r.hgetall(key)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    state = data.get("state", RestoreState.PENDING.value)
    if state in {RestoreState.COMPLETED.value, RestoreState.FAILED.value, RestoreState.CANCELLED.value}:
        return _hash_to_record(data)
    pipe = r.pipeline(transaction=True)
    pipe.hset(key, "cancel_requested", "1")
    pipe.lrem(cfg["redis"]["queue_key"], 0, job_id)
    pipe.execute()
    data = r.hgetall(key)
    if data.get("state") == RestoreState.PENDING.value:
        r.hset(
            key,
            mapping={
                "state": RestoreState.CANCELLED.value,
                "updated_at": utc_now_iso(),
            },
        )
    return _hash_to_record(r.hgetall(key))


@api.get("/jobs/{job_id}/log")
def get_job_log(job_id: str, limit: int = 200) -> list[dict[str, Any]]:
    cfg = load_config()
    r = redis_client()
    log_key = f"{job_key(cfg, job_id)}{cfg['redis']['job_log_suffix']}"
    lines = r.lrange(log_key, -limit, -1)
    parsed: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append({"raw": line})
    return parsed


app = FastAPI(title="Restore Engine")
cfg_boot = load_config()
session_secret = (cfg_boot.get("ui") or {}).get("session_secret") or "dev-insecure-session-secret-change-me"
app.add_middleware(SessionMiddleware, secret_key=session_secret)
app.include_router(ui_router)
app.include_router(api)


@app.get("/health")
def health() -> JSONResponse:
    config_ok = False
    config_detail = ""
    cfg: dict[str, Any] = {}
    try:
        cfg = load_config()
        missing = [section for section in ("pbs", "proxmox", "redis") if not cfg.get(section)]
        if missing:
            config_detail = f"missing config sections: {', '.join(missing)}"
        else:
            config_ok = True
            config_detail = "loaded"
    except Exception as exc:
        config_detail = f"config load failed: {exc}"

    pbs = ui_module.health_pbs_component(cfg) if config_ok else {"ok": False, "detail": "config not loaded"}
    pve = ui_module.health_proxmox_component(cfg) if config_ok else {"ok": False, "detail": "config not loaded"}

    redis_ok = False
    redis_detail = ""
    try:
        redis_client().ping()
        redis_ok = True
        redis_detail = "reachable"
    except Exception as exc:
        redis_detail = str(exc)

    overall = config_ok and pbs["ok"] and pve["ok"] and redis_ok
    body = {
        "ok": overall,
        "status": "ok" if overall else "degraded",
        "components": {
            "config": {"ok": config_ok, "detail": config_detail},
            "pbs": pbs,
            "proxmox": pve,
            "redis": {"ok": redis_ok, "detail": redis_detail},
        },
    }
    return JSONResponse(status_code=200 if overall else 503, content=body)


@app.get("/version")
def version() -> dict[str, str]:
    return {"name": "restore-engine", "version": "0.1.0"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
