"""FastAPI API for PBS -> Proxmox VE restore orchestration."""

from __future__ import annotations

import json
import os
import uuid
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
from pbs_client import list_vm_backups
from pve_client import (
    allocate_sequential_free_vmids,
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


def job_key(cfg: dict[str, Any], job_id: str) -> str:
    return f"{cfg['redis']['job_key_prefix']}{job_id}"


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
) -> dict[str, Any]:
    """Allocate sequential VMIDs and enqueue one restore job per row."""
    proxmox = connect_proxmox(cfg)
    try:
        in_use = qemu_vmids_in_use_on_node(proxmox, node)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Cannot list QEMU guests on node {node!r}: {exc}") from exc

    try:
        allocated_ids, _ = allocate_sequential_free_vmids(set(in_use), vmid_start, len(rows))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_ids: list[str] = []
    for row, target_vmid in zip(rows, allocated_ids, strict=True):
        job_id = str(uuid.uuid4())
        now = utc_now_iso()
        archive = archive_path(row["pve_storage"], row["voltail"])
        mapping = {
            "job_id": job_id,
            "state": RestoreState.PENDING.value,
            "backup_id": row["backup_id"],
            "vm_name": row["name"],
            "source_vmid": str(row["vmid"]),
            "source_label": row.get("source_label", ""),
            "proxmox_vmid": str(target_vmid),
            "proxmox_node": node,
            "proxmox_storage": target_storage,
            "live_restore": "1" if live_restore else "0",
            "bwlimit": str(int(bwlimit or 0)),
            "archive": archive,
            "progress": "0",
            "error": "",
            "created_at": now,
            "updated_at": now,
        }
        r.hset(job_key(cfg, job_id), mapping=mapping)
        r.rpush(cfg["redis"]["queue_key"], job_id)
        job_ids.append(job_id)

    return {"enqueued": len(job_ids), "job_ids": job_ids, "proxmox_vmids_assigned": allocated_ids}


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
