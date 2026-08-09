"""Enqueue restore jobs into Redis (shared by API and worker plan advancement)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import redis

from pve_client import (
    allocate_sequential_free_vmids,
    archive_path,
    assign_nodes_least_loaded,
    connect_proxmox,
    destroy_owned_qemu_vm,
    find_guest_resource,
    find_qemu_node,
    qemu_is_managed_by_tool,
    qemu_vmids_in_use_cluster,
    resolve_storage_for_node,
)
from states import RestoreState


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_key(cfg: dict[str, Any], job_id: str) -> str:
    return f"{cfg['redis']['job_key_prefix']}{job_id}"


def active_restore_counts_by_node(r: redis.Redis, cfg: dict[str, Any]) -> dict[str, int]:
    """Count PENDING/RESTORING jobs per target node (for load-balanced placement)."""
    prefix = cfg["redis"]["job_key_prefix"]
    suffix = cfg["redis"]["job_log_suffix"]
    counts: dict[str, int] = {}
    for key in r.scan_iter(f"{prefix}*", count=200):
        if key.endswith(suffix):
            continue
        data = r.hgetall(key) or {}
        state = data.get("state") or ""
        if state not in {RestoreState.PENDING.value, RestoreState.RESTORING.value}:
            continue
        node = (data.get("proxmox_node") or "").strip()
        if not node:
            continue
        counts[node] = counts.get(node, 0) + 1
    return counts


def enqueue_restores(
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
    """Allocate target VMIDs and enqueue one restore job per row.

    ``restore_mode``:
      - ``normal``: allocate free cluster VMIDs from ``vmid_start``; unique MAC/UUID.
      - ``dr``: use each backup's source VMID; keep MAC/UUID; fail if VMID is in use
        unless ``overwrite`` is True *and* the existing QEMU was provisioned by
        restore-engine (foreign VMs/LXCs are never destroyed).

    ``power_on`` starts the guest after restore (live-restore already boots early).
    ``qga_wait_sec`` > 0 waits for QEMU guest agent ping after the guest is up;
    timeout fails the job. QGA wait implies power-on.

    ``nodes`` (preferred) or ``node`` selects restore target(s). When multiple
    nodes are given, jobs are spread with least-loaded placement (important for
    live restore).

    Storage is resolved per assigned node via ``storage_by_node`` (preferred) with
    ``target_storage`` as fallback — so node-local ZFS mirrors can differ per host.

    Raises RuntimeError on Proxmox list / VMID allocation / DR conflicts.
    """
    mode = (restore_mode or "normal").strip().lower()
    if mode not in {"normal", "dr"}:
        raise RuntimeError(f"restore_mode must be 'normal' or 'dr', got {restore_mode!r}")

    do_power_on = bool(power_on) or bool(live_restore)
    try:
        qga_sec = max(0, int(qga_wait_sec or 0))
    except (TypeError, ValueError):
        qga_sec = 0
    if qga_sec > 0 and not do_power_on:
        do_power_on = True

    candidates = [n.strip() for n in (nodes or []) if str(n).strip()]
    if not candidates:
        single = (node or "").strip()
        if not single:
            raise RuntimeError("proxmox node (or nodes) is required")
        candidates = [single]

    by_node: dict[str, str] = {}
    for key, val in (storage_by_node or {}).items():
        n = str(key).strip()
        s = str(val).strip()
        if n and s:
            by_node[n] = s
    default_storage = (target_storage or "").strip()

    # Fail fast before allocating VMIDs if any candidate lacks storage.
    for cand in candidates:
        resolve_storage_for_node(cand, storage_by_node=by_node, default_storage=default_storage)

    proxmox = connect_proxmox(cfg)
    try:
        in_use = qemu_vmids_in_use_cluster(proxmox)
    except Exception as exc:
        raise RuntimeError(f"Cannot list guest VMIDs in Proxmox cluster: {exc}") from exc

    if mode == "dr":
        allocated_ids: list[int] = []
        seen: set[int] = set()
        for row in rows:
            try:
                src_vmid = int(row["vmid"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"DR restore requires a valid source VMID on each backup: {exc}") from exc
            if src_vmid in seen:
                raise RuntimeError(
                    f"DR restore batch has duplicate source VMID {src_vmid}; "
                    "select one snapshot per VM"
                )
            seen.add(src_vmid)
            if src_vmid in in_use:
                if not overwrite:
                    raise RuntimeError(
                        f"DR restore failed: VMID {src_vmid} already exists on the Proxmox cluster "
                        "(QEMU or LXC). Remove that guest manually in Proxmox, or use Normal restore "
                        "to assign a new VMID. Overwrite only reclaims guests previously restored "
                        "by restore-engine."
                    )
            allocated_ids.append(src_vmid)
        unique_flag = False
        force_flag = bool(overwrite)
    else:
        try:
            allocated_ids, _ = allocate_sequential_free_vmids(set(in_use), vmid_start, len(rows))
        except RuntimeError:
            raise
        unique_flag = True
        force_flag = True

    active = active_restore_counts_by_node(r, cfg)
    node_for_row = assign_nodes_least_loaded(candidates, len(rows), active_counts=active)
    storage_for_row = [
        resolve_storage_for_node(n, storage_by_node=by_node, default_storage=default_storage)
        for n in node_for_row
    ]

    if mode == "dr" and overwrite:
        for vmid in allocated_ids:
            if int(vmid) not in in_use:
                continue
            guest = find_guest_resource(proxmox, int(vmid))
            gtype = (guest or {}).get("type") or ""
            if gtype == "lxc":
                raise RuntimeError(
                    f"DR overwrite refused: VMID {vmid} is an LXC container. "
                    "restore-engine never deletes LXCs. Move/remove it in Proxmox or use Normal restore."
                )
            node = (guest or {}).get("node") or find_qemu_node(proxmox, int(vmid)) or (
                candidates[0] if candidates else ""
            )
            if not node:
                raise RuntimeError(f"Cannot overwrite VMID {vmid}: node unknown")
            if not qemu_is_managed_by_tool(proxmox, node, int(vmid)):
                raise RuntimeError(
                    f"DR overwrite refused: VMID {vmid} on {node} was not provisioned by "
                    "restore-engine. Foreign VMs are never deleted by this tool — remove it "
                    "manually in Proxmox (or leave the slot empty), then retry."
                )
            try:
                destroy_owned_qemu_vm(proxmox, node, int(vmid))
            except Exception as exc:
                raise RuntimeError(f"DR overwrite failed destroying managed VMID {vmid}: {exc}") from exc

    net_mode = (network_mode or "none").strip().lower() or "none"
    if net_mode not in {"none", "unlink", "remap"}:
        net_mode = "none"

    job_ids: list[str] = []
    for row, target_vmid, target_node, target_store in zip(
        rows, allocated_ids, node_for_row, storage_for_row, strict=True
    ):
        job_id = str(uuid.uuid4())
        now = utc_now_iso()
        archive = archive_path(row["pve_storage"], row["voltail"])
        try:
            backup_size = max(0, int(row.get("size_bytes") or 0))
        except (TypeError, ValueError):
            backup_size = 0
        mapping = {
            "job_id": job_id,
            "state": RestoreState.PENDING.value,
            "backup_id": row["backup_id"],
            "vm_name": row["name"],
            "source_vmid": str(row["vmid"]),
            "source_label": row.get("source_label", ""),
            "proxmox_vmid": str(target_vmid),
            "proxmox_node": target_node,
            "proxmox_storage": target_store,
            "live_restore": "1" if live_restore else "0",
            "powered_off": "0" if do_power_on else "1",
            "power_on": "1" if do_power_on else "0",
            "qga_wait_sec": str(qga_sec),
            "qga_ok": "",
            "qga_waited_sec": "",
            "network_mode": net_mode,
            "lab_bridge": str(lab_bridge or "").strip(),
            "overwrite": "1" if overwrite else "0",
            "http_check_url": str(http_check_url or "").strip(),
            "http_check_ok": "",
            "bwlimit": str(int(bwlimit or 0)),
            "restore_mode": mode,
            "unique": "1" if unique_flag else "0",
            "force": "1" if force_flag else "0",
            "archive": archive,
            "backup_size_bytes": str(backup_size),
            "progress": "0",
            "error": "",
            "created_at": now,
            "updated_at": now,
            "plan_run_id": plan_run_id or "",
            "plan_group_index": "" if plan_group_index is None else str(plan_group_index),
        }
        r.hset(job_key(cfg, job_id), mapping=mapping)
        r.rpush(cfg["redis"]["queue_key"], job_id)
        job_ids.append(job_id)

    return {
        "enqueued": len(job_ids),
        "job_ids": job_ids,
        "proxmox_vmids_assigned": allocated_ids,
        "proxmox_nodes_assigned": node_for_row,
        "proxmox_storages_assigned": storage_for_row,
        "load_balance_nodes": candidates,
        "storage_by_node": by_node or {n: default_storage for n in candidates if default_storage},
        "restore_mode": mode,
        "power_on": do_power_on,
        "qga_wait_sec": qga_sec,
    }
