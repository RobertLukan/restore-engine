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
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
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
    qemu_vmids_in_use_cluster,
)
from progress_parse import safe_float, safe_int
from queue_control import collect_job_stats, drain_pending_jobs, set_queue_paused
from states import PlanRunStatus, PlanVerification, RestoreState
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
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Resolve guest tags for the given backup rows, caching per (immutable) volid.

    Returns ``(tags_by_backup_id, errors_by_backup_id)``. Failures are not cached
    and are reported so the UI does not show a false "(none)".
    """
    r = redis_client()
    result: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
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

        def fetch(row: dict[str, Any]) -> tuple[str, str, list[str], str]:
            try:
                text = extract_vm_config(proxmox, node, row["_volid"])
                return row["backup_id"], row["_volid"], parse_tags(text), ""
            except Exception as exc:
                return row["backup_id"], row["_volid"], [], str(exc)

        with ThreadPoolExecutor(max_workers=5) as pool:
            for backup_id, volid, tags, err in pool.map(fetch, pending):
                if err:
                    errors[backup_id] = err
                    continue
                r.set(tag_cache_key(cfg, volid), ";".join(tags))
                result[backup_id] = tags
    return result, errors


def _dedupe_nodes(nodes: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in nodes:
        name = str(raw).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _resolve_restore_nodes(
    *,
    proxmox_node: str | None,
    proxmox_nodes: list[str] | None,
    default_node: str,
) -> list[str]:
    """Prefer multi-node list; fall back to single node / config default."""
    multi = _dedupe_nodes(list(proxmox_nodes or []))
    if multi:
        return multi
    single = (proxmox_node or default_node or "").strip()
    if not single:
        raise HTTPException(
            status_code=400,
            detail="proxmox.default_node, proxmox_node, or proxmox_nodes is required",
        )
    return [single]


def _enqueue_restores(
    r: redis.Redis,
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    node: str = "",
    nodes: list[str] | None = None,
    target_storage: str = "",
    storage_by_node: dict[str, str] | None = None,
    vmid_start: int,
    live_restore: bool,
    bwlimit: int,
    restore_mode: str = "normal",
    plan_run_id: str = "",
    plan_group_index: int | None = None,
    power_on: bool = False,
    qga_wait_sec: int = 0,
    network_mode: str = "none",
    lab_bridge: str = "",
    overwrite: bool = False,
    http_check_url: str = "",
) -> dict[str, Any]:
    """API wrapper around jobs.enqueue_restores (maps errors to HTTP)."""
    try:
        return enqueue_restores(
            r,
            cfg,
            rows,
            node=node,
            nodes=nodes,
            target_storage=target_storage,
            storage_by_node=storage_by_node,
            vmid_start=vmid_start,
            live_restore=live_restore,
            bwlimit=bwlimit,
            restore_mode=restore_mode,
            plan_run_id=plan_run_id,
            plan_group_index=plan_group_index,
            power_on=power_on,
            qga_wait_sec=qga_wait_sec,
            network_mode=network_mode,
            lab_bridge=lab_bridge,
            overwrite=overwrite,
            http_check_url=http_check_url,
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


def _normalize_net_mode(raw: str | None) -> str:
    mode = (raw or "none").strip().lower()
    return mode if mode in {"none", "unlink", "remap"} else "none"


def _assert_overwrite_confirm(*, overwrite: bool, confirm_overwrite: bool, restore_mode: str) -> None:
    if not overwrite:
        return
    if (restore_mode or "").strip().lower() != "dr":
        raise HTTPException(status_code=400, detail="overwrite is only valid with restore_mode=dr")
    if not confirm_overwrite:
        raise HTTPException(
            status_code=400,
            detail=(
                "DR overwrite requires confirm_overwrite=true. "
                "Only guests previously restored by restore-engine can be reclaimed; "
                "foreign VMs/LXCs are never deleted."
            ),
        )


def _assert_power_on_isolation(
    *,
    power_on: bool,
    qga_wait_sec: int,
    live_restore: bool,
    network_mode: str,
    isolated: bool = False,
    allow_non_isolated: bool = False,
) -> None:
    wants_boot = bool(power_on) or int(qga_wait_sec or 0) > 0 or bool(live_restore)
    if not wants_boot or allow_non_isolated:
        return
    net = _normalize_net_mode(network_mode)
    if isolated or net in {"unlink", "remap"}:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "Power-on / live-restore / QGA require network isolation "
            "(network_mode=unlink|remap or an isolated location), "
            "or pass allow_non_isolated=true"
        ),
    )


class RestoreSelectedRequest(BaseModel):
    backup_ids: list[str] = Field(min_length=1)
    proxmox_node: str | None = None
    proxmox_nodes: list[str] = Field(default_factory=list)
    proxmox_storage: str | None = None
    proxmox_storage_by_node: dict[str, str] = Field(default_factory=dict)
    proxmox_vmid_start: int = Field(default=100, ge=100)
    live_restore: bool = False
    bwlimit: int = Field(default=0, ge=0)
    restore_mode: str = Field(default="normal")  # normal | dr
    power_on: bool = False
    qga_wait_sec: int = Field(default=0, ge=0, le=3600)
    network_mode: str = Field(default="none")
    lab_bridge: str = ""
    overwrite: bool = False
    http_check_url: str = ""
    confirm_overwrite: bool = False
    allow_non_isolated: bool = False


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
    restore_mode: str = "normal"
    power_on: bool = False
    qga_wait_sec: int = 0
    qga_ok: str = ""
    qga_waited_sec: str = ""
    network_mode: str = "none"
    lab_bridge: str = ""
    overwrite: bool = False
    http_check_url: str = ""
    http_check_ok: str = ""
    progress: int = 0
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    restore_started_at: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    speed_bps: int = 0
    eta_sec: int | None = None
    pve_status_text: str = ""
    pve_upid: str = ""
    archive: str = ""
    plan_run_id: str = ""
    plan_group_index: str = ""
    backup_size_bytes: int = 0
    network_bytes_done: int = 0
    network_speed_bps: int = 0
    nonzero_bytes_done: int = 0
    nonzero_speed_bps: int = 0
    wire_compression_ratio: float = 0.0
    wire_sample_chunks: int = 0
    disk_sparsity_ratio: float = 0.0

api = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(require_ui_session)])


@api.get("/backups")
def list_backups(offset: int = 0, limit: int | None = None) -> Any:
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
    if limit is None:
        return rows
    from job_hygiene import paginate

    return paginate(rows, offset=offset, limit=limit)


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
        "power_on": bool(px.get("power_on_default", False)),
        "qga_wait_sec": int(worker_cfg.get("qga_wait_sec_default", px.get("qga_wait_sec_default", 120)) or 0),
        "max_concurrent_restores": int(worker_cfg.get("max_concurrent_restores", 2) or 2),
        "require_verified_to_run": plans_module.require_verified_to_run(cfg),
    }
    # VMIDs are cluster-global; prefer cluster resource list over a single node.
    try:
        used = qemu_vmids_in_use_cluster(connect_proxmox(cfg))
        out["next_free_proxmox_vmid"] = max(max(used) + 1 if used else 100, 100)
    except Exception:
        pass
    if node:
        out["default_node"] = node or out["default_node"]
    return out


def _normalize_storage_by_node(raw: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in (raw or {}).items():
        node = str(key).strip()
        storage = str(val).strip()
        if node and storage:
            out[node] = storage
    return out


def _require_storage_selection(
    *,
    nodes: list[str],
    proxmox_storage: str | None,
    proxmox_storage_by_node: dict[str, str] | None,
) -> tuple[str, dict[str, str]]:
    by_node = _normalize_storage_by_node(proxmox_storage_by_node)
    default = (proxmox_storage or "").strip()
    missing = [n for n in nodes if n not in by_node and not default]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Select storage for node(s): {', '.join(missing)}",
        )
    if not by_node and not default:
        raise HTTPException(status_code=400, detail="proxmox_storage or proxmox_storage_by_node is required")
    return default, by_node


@api.post("/jobs/restore-selected")
def restore_selected(request: Request, body: RestoreSelectedRequest) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    px = cfg.get("proxmox") or {}
    nodes = _resolve_restore_nodes(
        proxmox_node=body.proxmox_node,
        proxmox_nodes=body.proxmox_nodes,
        default_node=str(px.get("default_node") or ""),
    )
    default_storage, storage_by_node = _require_storage_selection(
        nodes=nodes,
        proxmox_storage=body.proxmox_storage,
        proxmox_storage_by_node=body.proxmox_storage_by_node,
    )

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

    _assert_overwrite_confirm(
        overwrite=body.overwrite,
        confirm_overwrite=body.confirm_overwrite,
        restore_mode=body.restore_mode,
    )
    _assert_power_on_isolation(
        power_on=body.power_on,
        qga_wait_sec=body.qga_wait_sec,
        live_restore=body.live_restore,
        network_mode=body.network_mode,
        allow_non_isolated=body.allow_non_isolated,
    )

    result = _enqueue_restores(
        r,
        cfg,
        ordered,
        nodes=nodes,
        target_storage=default_storage,
        storage_by_node=storage_by_node,
        vmid_start=body.proxmox_vmid_start,
        live_restore=body.live_restore,
        bwlimit=body.bwlimit,
        restore_mode=body.restore_mode,
        power_on=body.power_on,
        qga_wait_sec=body.qga_wait_sec,
        network_mode=body.network_mode,
        lab_bridge=body.lab_bridge,
        overwrite=body.overwrite,
        http_check_url=body.http_check_url,
    )
    _audit_action(
        request,
        "restore.enqueue",
        {"enqueued": result.get("enqueued"), "mode": body.restore_mode, "overwrite": body.overwrite},
    )
    return result


def _audit_action(request: Request, action: str, detail: dict[str, Any] | None = None) -> None:
    try:
        import audit as audit_module

        cfg = load_config()
        actor = getattr(request.state, "auth_actor", None) or "ui"
        audit_module.append_audit(redis_client(), cfg, action=action, actor=str(actor), detail=detail or {})
    except Exception:
        pass


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
    tags_by_id, errors = _resolve_tags(cfg, rows, node, force=body.force)
    all_tags = sorted({t for tags in tags_by_id.values() for t in tags}, key=str.lower)
    return {"tags": tags_by_id, "all_tags": all_tags, "errors": errors}


class RestoreTagGroupRequest(BaseModel):
    tag: str = Field(min_length=1)
    at_or_before: str | None = None
    proxmox_node: str | None = None
    proxmox_nodes: list[str] = Field(default_factory=list)
    proxmox_storage: str | None = None
    proxmox_storage_by_node: dict[str, str] = Field(default_factory=dict)
    proxmox_vmid_start: int = Field(default=100, ge=100)
    live_restore: bool = False
    bwlimit: int = Field(default=0, ge=0)
    restore_mode: str = Field(default="normal")
    power_on: bool = False
    qga_wait_sec: int = Field(default=0, ge=0, le=3600)
    network_mode: str = Field(default="none")
    lab_bridge: str = ""
    overwrite: bool = False
    http_check_url: str = ""
    confirm_overwrite: bool = False
    allow_non_isolated: bool = False

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
    nodes = _resolve_restore_nodes(
        proxmox_node=body.proxmox_node,
        proxmox_nodes=body.proxmox_nodes,
        default_node=str(px.get("default_node") or ""),
    )
    # Tag resolution only needs any online node that can read extractconfig.
    tag_node = nodes[0]
    default_storage, storage_by_node = _require_storage_selection(
        nodes=nodes,
        proxmox_storage=body.proxmox_storage,
        proxmox_storage_by_node=body.proxmox_storage_by_node,
    )

    cutoff = normalize_cutoff(body.at_or_before)
    candidates = _latest_per_vmid(list_vm_backups(cfg), cutoff)
    if not candidates:
        return {"enqueued": 0, "job_ids": [], "proxmox_vmids_assigned": [], "matched_vmids": []}

    tags_by_id, _errors = _resolve_tags(cfg, candidates, tag_node)
    wanted = body.tag.strip().lower()
    selected = [
        row
        for row in candidates
        if wanted in {t.lower() for t in tags_by_id.get(row["backup_id"], [])}
    ]
    if not selected:
        return {"enqueued": 0, "job_ids": [], "proxmox_vmids_assigned": [], "matched_vmids": []}

    _assert_overwrite_confirm(
        overwrite=body.overwrite,
        confirm_overwrite=body.confirm_overwrite,
        restore_mode=body.restore_mode,
    )
    _assert_power_on_isolation(
        power_on=body.power_on,
        qga_wait_sec=body.qga_wait_sec,
        live_restore=body.live_restore,
        network_mode=body.network_mode,
        allow_non_isolated=body.allow_non_isolated,
    )

    selected.sort(key=lambda row: row["vmid"])
    result = _enqueue_restores(
        r,
        cfg,
        selected,
        nodes=nodes,
        target_storage=default_storage,
        storage_by_node=storage_by_node,
        vmid_start=body.proxmox_vmid_start,
        live_restore=body.live_restore,
        bwlimit=body.bwlimit,
        restore_mode=body.restore_mode,
        power_on=body.power_on,
        qga_wait_sec=body.qga_wait_sec,
        network_mode=body.network_mode,
        lab_bridge=body.lab_bridge,
        overwrite=body.overwrite,
        http_check_url=body.http_check_url,
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
    nodes: list[str] = Field(default_factory=list)
    storage: str = Field(min_length=1)
    storage_by_node: dict[str, str] = Field(default_factory=dict)
    vmid_start: int = Field(default=100, ge=100)
    bwlimit: int = Field(default=0, ge=0)
    live_restore: bool = False
    restore_mode: str = Field(default="normal")
    power_on: bool = False
    qga_wait_sec: int = Field(default=0, ge=0, le=3600)
    network_mode: str = Field(default="none")
    lab_bridge: str = ""
    isolated: bool = False
    http_check_url: str = ""


class PlanUpsert(BaseModel):
    name: str = Field(min_length=1)
    group_ids: list[str] = Field(min_length=1)
    location_id: str = Field(min_length=1)
    halt_on_error: bool = True
    enabled: bool = True
    schedule_enabled: bool = False
    schedule_interval_hours: int = Field(default=0, ge=0)
    schedule_drill: bool = True
    assurance_enabled: bool = False
    assurance_require_qga: bool = False
    assurance_require_http: bool = False
    assurance_max_rto_sec: int = Field(default=0, ge=0)


class PlanUpdate(BaseModel):
    """Partial plan update (Assurance tab can PATCH policy fields alone)."""

    name: str | None = Field(default=None, min_length=1)
    group_ids: list[str] | None = Field(default=None, min_length=1)
    location_id: str | None = Field(default=None, min_length=1)
    halt_on_error: bool | None = None
    enabled: bool | None = None
    schedule_enabled: bool | None = None
    schedule_interval_hours: int | None = Field(default=None, ge=0)
    schedule_drill: bool | None = None
    assurance_enabled: bool | None = None
    assurance_require_qga: bool | None = None
    assurance_require_http: bool | None = None
    assurance_max_rto_sec: int | None = Field(default=None, ge=0)


class PlanRunRequest(BaseModel):
    at_or_before: str | None = None
    location_id: str | None = None
    drill: bool = False
    auto_teardown: bool = False
    powered_off: bool | None = None
    power_on: bool = False
    qga_wait_sec: int = Field(default=0, ge=0, le=3600)
    allow_unverified: bool = False
    confirm_dr: bool = False
    overwrite: bool = False
    confirm_overwrite: bool = False
    allow_non_isolated: bool = False


class PlanTeardownRequest(BaseModel):
    force: bool = False


class PlanCheckRequest(BaseModel):
    at_or_before: str | None = None


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
def api_update_plan(plan_id: str, body: PlanUpdate) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    payload = body.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(status_code=400, detail="No plan fields to update")
    if "group_ids" in payload:
        for gid in payload["group_ids"] or []:
            if not plans_module.get_group(r, cfg, gid):
                raise HTTPException(status_code=400, detail=f"Unknown group_id: {gid}")
    if "location_id" in payload:
        if not plans_module.get_location(r, cfg, payload["location_id"]):
            raise HTTPException(status_code=400, detail=f"Unknown location_id: {payload['location_id']}")
    try:
        return plans_module.update_plan(r, cfg, plan_id, payload)
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


@api.post("/plans/{plan_id}/check")
def api_check_plan(plan_id: str, body: PlanCheckRequest | None = None) -> dict[str, Any]:
    """Run readiness checks and update plan verification / last_check."""
    cfg = load_config()
    r = redis_client()
    plan = plans_module.get_plan(r, cfg, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    body = body or PlanCheckRequest()
    cutoff = normalize_cutoff(body.at_or_before)
    try:
        updated, check = plans_module.run_plan_readiness(
            r,
            cfg,
            plan,
            cutoff=cutoff,
            resolve_tags_fn=lambda c, rows, node: _resolve_tags(c, rows, node),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Readiness check failed: {exc}") from exc
    return {"plan": updated, "check": check}


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
        tags_by_id, _errors = _resolve_tags(cfg, candidates, node)

    return [
        plans_module.resolve_group_rows(group, backups, cutoff=cutoff, tags_by_backup_id=tags_by_id)
        for group in groups
    ]


@api.post("/plans/{plan_id}/run")
def api_run_plan(plan_id: str, request: Request, body: PlanRunRequest) -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    plan = plans_module.get_plan(r, cfg, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not plan.get("enabled", True):
        raise HTTPException(status_code=400, detail="Plan is disabled")

    if plans_module.require_verified_to_run(cfg):
        if plan.get("verification") != PlanVerification.VERIFIED.value and not body.allow_unverified:
            raise HTTPException(
                status_code=400,
                detail="Plan is not VERIFIED; run Check first or pass allow_unverified=true",
            )

    location_id = (body.location_id or plan.get("location_id") or "").strip()
    location = plans_module.get_location(r, cfg, location_id)
    if not location:
        raise HTTPException(status_code=400, detail=f"Unknown location_id: {location_id}")

    restore_mode = str(location.get("restore_mode") or "normal").strip().lower()
    if restore_mode == "dr" and not body.confirm_dr:
        raise HTTPException(
            status_code=400,
            detail="DR location requires confirm_dr=true (keeps source VMIDs/MACs/UUIDs)",
        )
    _assert_overwrite_confirm(
        overwrite=body.overwrite,
        confirm_overwrite=body.confirm_overwrite,
        restore_mode=restore_mode,
    )
    net_mode = str(location.get("network_mode") or "none")
    drill = bool(body.drill)
    # Location power_on is a recovery default; drills ignore it unless the run opts in.
    wants_power = bool(body.power_on) or (
        (not drill) and bool(location.get("power_on"))
    )
    # Drill default is powered-off; isolation gate only when something will boot.
    live = bool(location.get("live_restore", False)) and not drill and (
        body.powered_off is not True
    )
    if wants_power or live or int(body.qga_wait_sec or 0) > 0:
        _assert_power_on_isolation(
            power_on=wants_power,
            qga_wait_sec=int(body.qga_wait_sec or location.get("qga_wait_sec") or 0),
            live_restore=live,
            network_mode=net_mode,
            isolated=bool(location.get("isolated")),
            allow_non_isolated=body.allow_non_isolated,
        )

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
            drill=drill,
            auto_teardown=bool(body.auto_teardown),
            powered_off=body.powered_off,
            power_on=bool(body.power_on),
            qga_wait_sec=int(body.qga_wait_sec or 0),
            overwrite=bool(body.overwrite),
        )
    except ValueError as exc:
        raise _http_value_error(exc) from exc
    except HTTPException:
        raise
    _audit_action(
        request,
        "plan.run",
        {"plan_id": plan_id, "run_id": run.get("id"), "drill": drill, "overwrite": body.overwrite},
    )
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


@api.post("/plan-runs/{run_id}/cancel")
def api_cancel_plan_run(run_id: str) -> dict[str, Any]:
    """Cancel an active plan run (pending jobs + stop advancing groups)."""
    cfg = load_config()
    r = redis_client()
    try:
        return plans_module.cancel_plan_run(r, cfg, run_id, job_key_fn=job_key)
    except ValueError as exc:
        raise _http_value_error(exc) from exc


@api.post("/plan-runs/{run_id}/teardown")
def api_teardown_plan_run(run_id: str, body: PlanTeardownRequest | None = None) -> dict[str, Any]:
    """Destroy QEMU VMs created by a finished plan run."""
    cfg = load_config()
    r = redis_client()
    run = plans_module.get_plan_run(r, cfg, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Plan run not found")
    body = body or PlanTeardownRequest()
    if run.get("status") == PlanRunStatus.RUNNING.value and not body.force:
        raise HTTPException(
            status_code=400,
            detail="Plan run is still RUNNING; cancel it first or pass force=true",
        )
    try:
        if run.get("status") == PlanRunStatus.RUNNING.value and body.force:
            plans_module.cancel_plan_run(r, cfg, run_id, job_key_fn=job_key)
            run = plans_module.get_plan_run(r, cfg, run_id) or run
        updated = plans_module.teardown_plan_run(r, cfg, run, job_key_fn=job_key)
    except ValueError as exc:
        raise _http_value_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Teardown failed: {exc}") from exc
    return plans_module.aggregate_plan_run(r, cfg, updated, job_key_fn=job_key)


@api.get("/reports")
def api_list_reports(
    plan_id: str | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    import reports as reports_module

    cfg = load_config()
    return reports_module.list_reports(
        redis_client(), cfg, plan_id=plan_id, kind=kind, limit=limit
    )


@api.get("/reports/{report_id}")
def api_get_report(report_id: str) -> dict[str, Any]:
    import reports as reports_module

    cfg = load_config()
    data = reports_module.get_report(redis_client(), cfg, report_id)
    if not data:
        raise HTTPException(status_code=404, detail="Report not found")
    return data


@api.get("/reports/{report_id}/download")
def api_download_report(report_id: str, format: str = "md") -> Response:
    import reports as reports_module

    cfg = load_config()
    data = reports_module.get_report(redis_client(), cfg, report_id)
    if not data:
        raise HTTPException(status_code=404, detail="Report not found")
    fmt = (format or "md").strip().lower()
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(data.get("title") or report_id))[:80]
    if fmt in {"html", "htm"}:
        body = data.get("html") or ""
        media = "text/html; charset=utf-8"
        filename = f"{safe_name or report_id}.html"
    else:
        body = data.get("markdown") or ""
        media = "text/markdown; charset=utf-8"
        filename = f"{safe_name or report_id}.md"
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class PlanAssureRequest(BaseModel):
    at_or_before: str | None = None
    allow_unverified: bool = False
    confirm_dr: bool = False
    allow_non_isolated: bool = False
    overwrite: bool = False
    confirm_overwrite: bool = False


@api.post("/plans/{plan_id}/assure")
def api_assure_plan(plan_id: str, request: Request, body: PlanAssureRequest | None = None) -> dict[str, Any]:
    """Start an assurance drill (powered-off by default; QGA/HTTP when policy requires)."""
    body = body or PlanAssureRequest()
    cfg = load_config()
    r = redis_client()
    plan = plans_module.get_plan(r, cfg, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not plan.get("enabled", True):
        raise HTTPException(status_code=400, detail="Plan is disabled")
    if not plan.get("assurance_enabled"):
        raise HTTPException(status_code=400, detail="Enable assurance on the plan first")

    if plans_module.require_verified_to_run(cfg):
        if plan.get("verification") != PlanVerification.VERIFIED.value and not body.allow_unverified:
            raise HTTPException(
                status_code=400,
                detail="Plan is not VERIFIED; run Check first or pass allow_unverified=true",
            )

    location = plans_module.get_location(r, cfg, str(plan.get("location_id") or ""))
    if not location:
        raise HTTPException(status_code=400, detail="Plan location not found")

    restore_mode = str(location.get("restore_mode") or "normal").strip().lower()
    if restore_mode == "dr" and not body.confirm_dr:
        raise HTTPException(
            status_code=400,
            detail="DR location requires confirm_dr=true for assurance runs",
        )
    _assert_overwrite_confirm(
        overwrite=body.overwrite,
        confirm_overwrite=body.confirm_overwrite,
        restore_mode=restore_mode,
    )

    need_qga = bool(plan.get("assurance_require_qga"))
    need_http = bool(plan.get("assurance_require_http"))
    if need_http and not str(location.get("http_check_url") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="assurance_require_http is set but location has no http_check_url",
        )

    qga_sec = 0
    power_on = False
    if need_qga or need_http:
        power_on = True
        try:
            qga_sec = max(0, int(location.get("qga_wait_sec") or 0))
        except (TypeError, ValueError):
            qga_sec = 0
        if qga_sec <= 0:
            worker_cfg = cfg.get("worker") or {}
            qga_sec = max(30, int(worker_cfg.get("qga_wait_sec_default", 120) or 120))

    net_mode = str(location.get("network_mode") or "none")
    if power_on:
        _assert_power_on_isolation(
            power_on=True,
            qga_wait_sec=qga_sec,
            live_restore=False,
            network_mode=net_mode,
            isolated=bool(location.get("isolated")),
            allow_non_isolated=body.allow_non_isolated,
        )

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
            drill=True,
            auto_teardown=True,
            powered_off=not power_on,
            power_on=power_on,
            qga_wait_sec=qga_sec,
            overwrite=bool(body.overwrite),
        )
    except ValueError as exc:
        raise _http_value_error(exc) from exc
    _audit_action(request, "plan.assure", {"plan_id": plan_id, "run_id": run.get("id")})
    return plans_module.aggregate_plan_run(r, cfg, run, job_key_fn=job_key)


@api.get("/assurance/dashboard")
def api_assurance_dashboard() -> dict[str, Any]:
    cfg = load_config()
    return plans_module.assurance_dashboard(redis_client(), cfg)


@api.get("/compliance/dashboard")
def api_compliance_dashboard() -> dict[str, Any]:
    import reports as reports_module

    cfg = load_config()
    r = redis_client()
    return reports_module.compliance_dashboard(
        r,
        cfg,
        list_plans_fn=plans_module.list_plans,
        list_plan_runs_fn=plans_module.list_plan_runs,
    )


def _hash_to_record(data: dict[str, str]) -> JobRecord:
    eta_raw = (data.get("eta_sec") or "").strip()
    eta_sec: int | None
    if eta_raw == "":
        eta_sec = None
    else:
        try:
            eta_sec = int(eta_raw)
        except ValueError:
            eta_sec = None
    return JobRecord(
        job_id=data["job_id"],
        state=data.get("state", ""),
        backup_id=data.get("backup_id", ""),
        vm_name=data.get("vm_name", ""),
        source_vmid=safe_int(data.get("source_vmid"), 0),
        source_label=data.get("source_label", ""),
        proxmox_vmid=safe_int(data.get("proxmox_vmid"), 0),
        proxmox_node=data.get("proxmox_node", ""),
        proxmox_storage=data.get("proxmox_storage", ""),
        live_restore=data.get("live_restore", "0") == "1",
        bwlimit=safe_int(data.get("bwlimit"), 0),
        restore_mode=(data.get("restore_mode") or "normal").strip().lower() or "normal",
        power_on=data.get("power_on", "0") == "1",
        qga_wait_sec=safe_int(data.get("qga_wait_sec"), 0),
        qga_ok=data.get("qga_ok", ""),
        qga_waited_sec=data.get("qga_waited_sec", ""),
        network_mode=(data.get("network_mode") or "none").strip().lower() or "none",
        lab_bridge=data.get("lab_bridge", ""),
        overwrite=data.get("overwrite", "0") == "1",
        http_check_url=data.get("http_check_url", ""),
        http_check_ok=data.get("http_check_ok", ""),
        progress=safe_int(data.get("progress"), 0),
        error=data.get("error", ""),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        restore_started_at=data.get("restore_started_at", ""),
        bytes_done=safe_int(data.get("bytes_done"), 0),
        bytes_total=safe_int(data.get("bytes_total"), 0),
        speed_bps=safe_int(data.get("speed_bps"), 0),
        eta_sec=eta_sec,
        pve_status_text=data.get("pve_status_text", ""),
        pve_upid=data.get("pve_upid", ""),
        archive=data.get("archive", ""),
        plan_run_id=data.get("plan_run_id", ""),
        plan_group_index=data.get("plan_group_index", ""),
        backup_size_bytes=safe_int(data.get("backup_size_bytes"), 0),
        network_bytes_done=safe_int(data.get("network_bytes_done"), 0),
        network_speed_bps=safe_int(data.get("network_speed_bps"), 0),
        nonzero_bytes_done=safe_int(data.get("nonzero_bytes_done"), 0),
        nonzero_speed_bps=safe_int(data.get("nonzero_speed_bps"), 0),
        wire_compression_ratio=safe_float(data.get("wire_compression_ratio"), 0.0),
        wire_sample_chunks=safe_int(data.get("wire_sample_chunks"), 0),
        disk_sparsity_ratio=safe_float(data.get("disk_sparsity_ratio"), 0.0),
    )


def _max_concurrent_from_cfg(cfg: dict[str, Any]) -> int:
    return max(1, int((cfg.get("worker") or {}).get("max_concurrent_restores", 2) or 2))


@api.get("/jobs/stats")
def jobs_stats() -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    return collect_job_stats(r, cfg, max_concurrent=_max_concurrent_from_cfg(cfg))


@api.post("/jobs/queue/pause")
def queue_pause() -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    set_queue_paused(r, cfg, True)
    return collect_job_stats(r, cfg, max_concurrent=_max_concurrent_from_cfg(cfg))


@api.post("/jobs/queue/resume")
def queue_resume() -> dict[str, Any]:
    cfg = load_config()
    r = redis_client()
    set_queue_paused(r, cfg, False)
    return collect_job_stats(r, cfg, max_concurrent=_max_concurrent_from_cfg(cfg))


@api.post("/jobs/queue/stop")
def queue_stop() -> dict[str, Any]:
    """Pause and cancel all PENDING jobs; in-flight restores continue."""
    cfg = load_config()
    r = redis_client()
    drained = drain_pending_jobs(r, cfg)
    stats = collect_job_stats(r, cfg, max_concurrent=_max_concurrent_from_cfg(cfg))
    stats["drained"] = drained
    return stats


@api.get("/jobs")
def list_jobs(state: str | None = None, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    from job_hygiene import collect_jobs, paginate

    cfg = load_config()
    r = redis_client()
    all_jobs = collect_jobs(r, cfg, state=state, record_fn=_hash_to_record)
    page = paginate(all_jobs, offset=offset, limit=limit)
    # Serialize JobRecord models for JSON.
    page["items"] = [j.model_dump() if hasattr(j, "model_dump") else j for j in page["items"]]
    return page


@api.get("/audit")
def list_audit_log(offset: int = 0, limit: int = 100) -> dict[str, Any]:
    import audit as audit_module

    cfg = load_config()
    r = redis_client()
    items = audit_module.list_audit(r, cfg, offset=offset, limit=limit)
    return {"items": items, "offset": offset, "limit": limit}


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
        missing: list[str] = []
        if not (cfg.get("pbs") or cfg.get("pbs_servers")):
            missing.append("pbs_servers")
        if not cfg.get("proxmox"):
            missing.append("proxmox")
        if not cfg.get("redis"):
            missing.append("redis")
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
