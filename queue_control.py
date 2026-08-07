"""Queue pause/resume/stop and job aggregate stats."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import redis

from jobs import job_key
from states import RestoreState


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def queue_paused_key(cfg: dict[str, Any]) -> str:
    redis_cfg = cfg.get("redis") or {}
    explicit = str(redis_cfg.get("queue_paused_key") or "").strip()
    if explicit:
        return explicit
    queue_key = str(redis_cfg.get("queue_key") or "restore:queue").strip()
    return f"{queue_key}:paused"


def is_queue_paused(r: redis.Redis, cfg: dict[str, Any]) -> bool:
    return (r.get(queue_paused_key(cfg)) or "") == "1"


def set_queue_paused(r: redis.Redis, cfg: dict[str, Any], paused: bool) -> None:
    key = queue_paused_key(cfg)
    if paused:
        r.set(key, "1")
    else:
        r.delete(key)


def collect_job_stats(r: redis.Redis, cfg: dict[str, Any], *, max_concurrent: int) -> dict[str, Any]:
    """Scan job hashes for state counts and aggregate in-flight metrics."""
    prefix = cfg["redis"]["job_key_prefix"]
    suffix = cfg["redis"]["job_log_suffix"]
    counts = {
        "active": 0,
        "pending": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
    }
    speed_sum = 0.0
    eta_values: list[int] = []
    for key in r.scan_iter(f"{prefix}*", count=200):
        if key.endswith(suffix):
            continue
        data = r.hgetall(key) or {}
        state = data.get("state") or ""
        if state == RestoreState.RESTORING.value:
            counts["active"] += 1
            try:
                speed_sum += float(data.get("speed_bps") or 0)
            except (TypeError, ValueError):
                pass
            try:
                eta = int(data.get("eta_sec") or 0)
                if eta > 0:
                    eta_values.append(eta)
            except (TypeError, ValueError):
                pass
        elif state == RestoreState.PENDING.value:
            counts["pending"] += 1
        elif state == RestoreState.COMPLETED.value:
            counts["completed"] += 1
        elif state == RestoreState.FAILED.value:
            counts["failed"] += 1
        elif state == RestoreState.CANCELLED.value:
            counts["cancelled"] += 1

    queue_len = int(r.llen(cfg["redis"]["queue_key"]) or 0)
    worst_eta = max(eta_values) if eta_values else None
    median_eta = None
    if eta_values:
        ordered = sorted(eta_values)
        median_eta = ordered[len(ordered) // 2]

    return {
        "active": counts["active"],
        "pending": counts["pending"],
        "completed": counts["completed"],
        "failed": counts["failed"],
        "cancelled": counts["cancelled"],
        "queue_length": queue_len,
        "max_concurrent": max(1, int(max_concurrent)),
        "paused": is_queue_paused(r, cfg),
        "aggregate_speed_bps": int(speed_sum) if speed_sum > 0 else 0,
        "worst_eta_sec": worst_eta,
        "median_eta_sec": median_eta,
    }


def drain_pending_jobs(r: redis.Redis, cfg: dict[str, Any]) -> dict[str, Any]:
    """Pause queue and cancel all PENDING jobs (in-flight untouched).

    Returns counts of cancelled job ids.
    """
    set_queue_paused(r, cfg, True)
    queue_key = cfg["redis"]["queue_key"]
    cancelled_ids: list[str] = []

    # Empty the list first.
    while True:
        job_id = r.lpop(queue_key)
        if not job_id:
            break
        job_id = str(job_id).strip()
        if not job_id:
            continue
        key = job_key(cfg, job_id)
        data = r.hgetall(key) or {}
        state = data.get("state") or RestoreState.PENDING.value
        if state == RestoreState.PENDING.value:
            r.hset(
                key,
                mapping={
                    "state": RestoreState.CANCELLED.value,
                    "updated_at": utc_now_iso(),
                    "error": "Cancelled by queue stop (pending drain)",
                    "eta_sec": "",
                    "speed_bps": "",
                },
            )
            cancelled_ids.append(job_id)

    # Sweep any PENDING hashes not on the list (orphans / races).
    prefix = cfg["redis"]["job_key_prefix"]
    suffix = cfg["redis"]["job_log_suffix"]
    for key in r.scan_iter(f"{prefix}*", count=200):
        if key.endswith(suffix):
            continue
        data = r.hgetall(key) or {}
        if (data.get("state") or "") != RestoreState.PENDING.value:
            continue
        job_id = data.get("job_id") or key[len(prefix) :]
        r.hset(
            key,
            mapping={
                "state": RestoreState.CANCELLED.value,
                "updated_at": utc_now_iso(),
                "error": "Cancelled by queue stop (pending drain)",
                "eta_sec": "",
                "speed_bps": "",
            },
        )
        if job_id not in cancelled_ids:
            cancelled_ids.append(str(job_id))

    return {"cancelled": len(cancelled_ids), "cancelled_job_ids": cancelled_ids}
