"""Job Redis hygiene: TTL on terminal jobs and paginated listing helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import redis

from states import RestoreState

TERMINAL = {
    RestoreState.COMPLETED.value,
    RestoreState.FAILED.value,
    RestoreState.CANCELLED.value,
}


def job_ttl_sec(cfg: dict[str, Any]) -> int:
    """Seconds to keep finished jobs (0 = never expire). Default 14 days."""
    try:
        return max(0, int((cfg.get("worker") or {}).get("job_ttl_sec", 14 * 86400) or 0))
    except (TypeError, ValueError):
        return 14 * 86400


def apply_job_ttl(r: redis.Redis, cfg: dict[str, Any], job_id: str, *, state: str) -> None:
    ttl = job_ttl_sec(cfg)
    if ttl <= 0 or state not in TERMINAL:
        return
    prefix = cfg["redis"]["job_key_prefix"]
    suffix = cfg["redis"]["job_log_suffix"]
    key = f"{prefix}{job_id}"
    log_key = f"{key}{suffix}"
    try:
        r.expire(key, ttl)
        r.expire(log_key, ttl)
    except Exception:
        pass


def purge_expired_scan(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    max_scan: int = 500,
) -> int:
    """Drop finished jobs older than TTL if Redis TTL was never set (legacy keys)."""
    ttl = job_ttl_sec(cfg)
    if ttl <= 0:
        return 0
    prefix = cfg["redis"]["job_key_prefix"]
    suffix = cfg["redis"]["job_log_suffix"]
    cutoff = datetime.now(timezone.utc).timestamp() - ttl
    purged = 0
    scanned = 0
    for key in r.scan_iter(f"{prefix}*", count=100):
        if key.endswith(suffix):
            continue
        scanned += 1
        if scanned > max_scan:
            break
        data = r.hgetall(key) or {}
        state = data.get("state") or ""
        if state not in TERMINAL:
            continue
        updated = data.get("updated_at") or data.get("created_at") or ""
        try:
            # ISO timestamps from utc_now_iso
            ts = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, AttributeError):
            continue
        if ts > cutoff:
            continue
        job_id = data.get("job_id") or key[len(prefix) :]
        r.delete(key)
        r.delete(f"{prefix}{job_id}{suffix}")
        purged += 1
    return purged


def _job_sort_key(job: Any) -> str:
    """Newest-first key: prefer updated_at, else created_at."""
    if isinstance(job, dict):
        return str(job.get("updated_at") or job.get("created_at") or "")
    return str(getattr(job, "updated_at", None) or getattr(job, "created_at", None) or "")


def _job_field(job: Any, name: str, default: Any = "") -> Any:
    if isinstance(job, dict):
        return job.get(name, default)
    return getattr(job, name, default)


def job_matches_query(job: Any, query: str) -> bool:
    """True when ``query`` matches VM name or source/target VMID (substring, case-insensitive)."""
    needle = (query or "").strip().lower()
    if not needle:
        return True
    name = str(_job_field(job, "vm_name") or "").lower()
    if needle in name:
        return True
    for field in ("proxmox_vmid", "source_vmid"):
        if needle in str(_job_field(job, field) or "").lower():
            return True
    return False


def filter_jobs(jobs: list[Any], query: str | None) -> list[Any]:
    q = (query or "").strip()
    if not q:
        return jobs
    return [j for j in jobs if job_matches_query(j, q)]


def collect_jobs(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    state: str | None = None,
    record_fn: Callable[[dict[str, str]], Any],
) -> list[Any]:
    prefix = cfg["redis"]["job_key_prefix"]
    suffix = cfg["redis"]["job_log_suffix"]
    out: list[Any] = []
    for key in r.scan_iter(f"{prefix}*", count=100):
        if key.endswith(suffix):
            continue
        data = r.hgetall(key)
        if not data:
            continue
        rec = record_fn(data)
        if state is None or getattr(rec, "state", None) == state or (isinstance(rec, dict) and rec.get("state") == state):
            out.append(rec)
    # Newest first so the first page (UI default) shows recent restores.
    return sorted(out, key=_job_sort_key, reverse=True)


def paginate(items: list[Any], *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    offset = max(0, int(offset))
    limit = max(1, min(1000, int(limit)))
    total = len(items)
    slice_ = items[offset : offset + limit]
    return {
        "items": slice_,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
    }


def purge_terminal_jobs(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    older_than_sec: int | None = None,
    max_delete: int = 5000,
) -> dict[str, int]:
    """Delete finished jobs (and their log lists). Active jobs are never removed.

    ``older_than_sec``: if set, only purge terminal jobs whose updated_at is older
    than this many seconds. ``None`` means purge all terminal jobs.
    """
    prefix = cfg["redis"]["job_key_prefix"]
    suffix = cfg["redis"]["job_log_suffix"]
    now = datetime.now(timezone.utc).timestamp()
    cutoff = None if older_than_sec is None else now - max(0, int(older_than_sec))
    deleted = 0
    scanned = 0
    for key in r.scan_iter(f"{prefix}*", count=200):
        if key.endswith(suffix):
            continue
        scanned += 1
        data = r.hgetall(key) or {}
        state = data.get("state") or ""
        if state not in TERMINAL:
            continue
        if cutoff is not None:
            updated = data.get("updated_at") or data.get("created_at") or ""
            try:
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, AttributeError):
                continue
            if ts > cutoff:
                continue
        job_id = data.get("job_id") or key[len(prefix) :]
        r.delete(key)
        r.delete(f"{prefix}{job_id}{suffix}")
        deleted += 1
        if deleted >= max_delete:
            break
    return {"scanned": scanned, "deleted": deleted}
