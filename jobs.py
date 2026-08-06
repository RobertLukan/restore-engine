"""Enqueue restore jobs into Redis (shared by API and worker plan advancement)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import redis

from pve_client import allocate_sequential_free_vmids, archive_path, connect_proxmox, qemu_vmids_in_use_on_node
from states import RestoreState


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_key(cfg: dict[str, Any], job_id: str) -> str:
    return f"{cfg['redis']['job_key_prefix']}{job_id}"


def enqueue_restores(
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
    """Allocate sequential VMIDs and enqueue one restore job per row.

    Raises RuntimeError on Proxmox list / VMID allocation failures.
    """
    proxmox = connect_proxmox(cfg)
    try:
        in_use = qemu_vmids_in_use_on_node(proxmox, node)
    except Exception as exc:
        raise RuntimeError(f"Cannot list QEMU guests on node {node!r}: {exc}") from exc

    try:
        allocated_ids, _ = allocate_sequential_free_vmids(set(in_use), vmid_start, len(rows))
    except RuntimeError:
        raise

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
            "plan_run_id": plan_run_id or "",
            "plan_group_index": "" if plan_group_index is None else str(plan_group_index),
        }
        r.hset(job_key(cfg, job_id), mapping=mapping)
        r.rpush(cfg["redis"]["queue_key"], job_id)
        job_ids.append(job_id)

    return {"enqueued": len(job_ids), "job_ids": job_ids, "proxmox_vmids_assigned": allocated_ids}
