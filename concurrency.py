"""Redis-backed restore concurrency slots (safe across multiple worker processes)."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import redis

log = logging.getLogger("restore-concurrency")


def _slots_key(cfg: dict[str, Any]) -> str:
    return str((cfg.get("redis") or {}).get("concurrency_slots_key") or "restore:concurrency:slots")


def _heartbeat_key(cfg: dict[str, Any]) -> str:
    return str((cfg.get("redis") or {}).get("worker_heartbeat_key") or "restore:worker:heartbeat")


def max_concurrent(cfg: dict[str, Any]) -> int:
    return max(1, int((cfg.get("worker") or {}).get("max_concurrent_restores", 2) or 2))


def active_slots(r: redis.Redis, cfg: dict[str, Any]) -> int:
    try:
        return max(0, int(r.get(_slots_key(cfg)) or 0))
    except (TypeError, ValueError):
        return 0


def count_restoring_jobs(r: redis.Redis, cfg: dict[str, Any]) -> int:
    """Count Redis jobs currently in RESTORING (best-effort scan)."""
    prefix = str((cfg.get("redis") or {}).get("job_key_prefix") or "restore:job:")
    suffix = str((cfg.get("redis") or {}).get("job_log_suffix") or ":log")
    n = 0
    try:
        for key in r.scan_iter(f"{prefix}*", count=200):
            if str(key).endswith(suffix):
                continue
            try:
                state = r.hget(key, "state")
            except Exception:
                continue
            if state == "RESTORING":
                n += 1
    except Exception:
        log.exception("Failed scanning restoring jobs for slot reconcile")
    return n


def reconcile_slots(r: redis.Redis, cfg: dict[str, Any]) -> int:
    """Reset leaked concurrency slots to match in-flight RESTORING jobs.

    Worker kills/restarts can leave ``restore:concurrency:slots`` elevated so
    ``try_acquire_slot`` never succeeds and PENDING jobs sit forever.
    """
    key = _slots_key(cfg)
    restoring = count_restoring_jobs(r, cfg)
    try:
        before = active_slots(r, cfg)
        r.set(key, restoring)
        if before != restoring:
            log.warning(
                "Reconciled concurrency slots %s -> %s (restoring jobs=%s)",
                before,
                restoring,
                restoring,
            )
        return restoring
    except Exception:
        log.exception("Failed to reconcile concurrency slots")
        return restoring


def try_acquire_slot(r: redis.Redis, cfg: dict[str, Any], *, limit: int | None = None) -> bool:
    """Atomically claim one concurrency slot. Returns False if at capacity."""
    cap = max_concurrent(cfg) if limit is None else max(1, int(limit))
    key = _slots_key(cfg)
    # INCR then roll back if over limit (simple; rare race corrected next loop).
    try:
        n = int(r.incr(key))
    except Exception:
        return False
    if n > cap:
        try:
            r.decr(key)
        except Exception:
            pass
        return False
    return True


def release_slot(r: redis.Redis, cfg: dict[str, Any]) -> None:
    key = _slots_key(cfg)
    try:
        n = int(r.decr(key))
        if n < 0:
            r.set(key, 0)
    except Exception:
        log.exception("Failed to release concurrency slot")


def touch_worker_heartbeat(r: redis.Redis, cfg: dict[str, Any], *, ttl_sec: int = 60) -> str:
    """Write a short-lived heartbeat so Compose healthcheck can detect a live worker."""
    token = f"{uuid.uuid4().hex}:{int(time.time())}"
    key = _heartbeat_key(cfg)
    try:
        r.set(key, token, ex=max(15, int(ttl_sec)))
    except Exception:
        log.exception("Failed to write worker heartbeat")
    return token


def worker_heartbeat_ok(r: redis.Redis, cfg: dict[str, Any]) -> bool:
    try:
        return bool(r.get(_heartbeat_key(cfg)))
    except Exception:
        return False
