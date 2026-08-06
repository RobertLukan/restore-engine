"""Redis-driven restore worker: PBS archive -> Proxmox VE restore."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis
import yaml

from pve_client import TaskCancelled, connect_proxmox, submit_restore, wait_for_task
from states import RestoreState
import plans as plans_module
from jobs import enqueue_restores, job_key as redis_job_key


_cfg_path_override = (os.environ.get("RESTORE_ENGINE_CONFIG") or "").strip()
CONFIG_PATH = (
    Path(_cfg_path_override).expanduser()
    if _cfg_path_override
    else Path(__file__).resolve().parent / "config.yaml"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("restore-worker")


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def job_log_key(cfg: dict[str, Any], job_id: str) -> str:
    return f"{redis_job_key(cfg, job_id)}{cfg['redis']['job_log_suffix']}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_log(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    level: str,
    stage: str,
    message: str,
) -> None:
    payload = {
        "ts": utc_now_iso(),
        "level": level,
        "stage": stage,
        "message": message,
    }
    r.rpush(job_log_key(cfg, job_id), json.dumps(payload))


def set_state(r: redis.Redis, cfg: dict[str, Any], job_id: str, state: RestoreState, **extra: str) -> None:
    mapping = {"state": state.value, "updated_at": utc_now_iso(), **extra}
    r.hset(redis_job_key(cfg, job_id), mapping=mapping)


def cancel_requested(r: redis.Redis, cfg: dict[str, Any], job_id: str) -> bool:
    return r.hget(redis_job_key(cfg, job_id), "cancel_requested") == "1"


def mark_cancelled(r: redis.Redis, cfg: dict[str, Any], job_id: str) -> None:
    set_state(r, cfg, job_id, RestoreState.CANCELLED)
    append_log(r, cfg, job_id, "INFO", "CANCELLED", "Job cancelled by operator")


def process_job(r: redis.Redis, cfg: dict[str, Any], job_id: str) -> None:
    key = redis_job_key(cfg, job_id)
    data = r.hgetall(key)
    if not data:
        return
    if cancel_requested(r, cfg, job_id):
        mark_cancelled(r, cfg, job_id)
        return

    node = data["proxmox_node"]
    target_vmid = int(data["proxmox_vmid"])
    backup_id = data["backup_id"]
    target_storage = data["proxmox_storage"]
    live_restore = data.get("live_restore", "0") == "1"
    try:
        bwlimit = int(data.get("bwlimit") or 0)
    except (TypeError, ValueError):
        bwlimit = 0
    archive = data.get("archive")
    if not archive:
        raise RuntimeError(f"Job {job_id} has no archive volid stored")

    set_state(r, cfg, job_id, RestoreState.RESTORING, progress="5")
    append_log(r, cfg, job_id, "INFO", "RESTORING", f"Submitting restore for {backup_id} -> VMID {target_vmid}")

    proxmox = connect_proxmox(cfg)
    upid = submit_restore(
        proxmox,
        node=node,
        target_vmid=target_vmid,
        archive=archive,
        target_storage=target_storage,
        live_restore=live_restore,
        bwlimit=bwlimit,
    )
    r.hset(key, mapping={"pve_upid": upid, "progress": "15"})
    append_log(r, cfg, job_id, "INFO", "RESTORING", f"PVE task started: {upid}")

    poll = float((cfg.get("worker") or {}).get("task_poll_interval_sec", 3))
    try:
        wait_for_task(
            proxmox,
            node,
            upid,
            poll_interval_sec=poll,
            should_cancel=lambda: cancel_requested(r, cfg, job_id),
        )
    except TaskCancelled:
        mark_cancelled(r, cfg, job_id)
        append_log(r, cfg, job_id, "INFO", "CANCELLED", f"PVE restore task {upid} stopped")
        return
    set_state(r, cfg, job_id, RestoreState.COMPLETED, progress="100")
    append_log(r, cfg, job_id, "INFO", "COMPLETED", f"Restore completed for VMID {target_vmid}")


def _current_max_concurrent(cfg: dict[str, Any]) -> int:
    """Read the concurrency limit fresh so UI/config changes apply without restart."""
    try:
        cfg = load_config()
    except Exception:
        pass
    return max(1, int((cfg.get("worker") or {}).get("max_concurrent_restores", 2)))


def worker_loop() -> None:
    cfg = load_config()
    r = redis.from_url(cfg["redis"]["url"], decode_responses=True)
    queue_key = cfg["redis"]["queue_key"]

    active_lock = threading.Lock()
    active_count = 0

    def run_job(job_id: str) -> None:
        nonlocal active_count
        try:
            process_job(r, cfg, job_id)
        except Exception as exc:
            log.exception("Job %s failed", job_id)
            set_state(r, cfg, job_id, RestoreState.FAILED, error=str(exc), progress="0")
            append_log(r, cfg, job_id, "ERROR", "FAILED", str(exc))
        finally:
            with active_lock:
                active_count -= 1

    log.info("Restore worker started (initial max_concurrent_restores=%s)", _current_max_concurrent(cfg))
    while True:
        # Advance ordered plan runs (next group when current group is terminal).
        try:
            plans_module.advance_plan_runs(r, cfg, enqueue_fn=enqueue_restores, job_key_fn=redis_job_key)
        except Exception:
            log.exception("Plan run advancement failed")

        # Re-read the limit each iteration so it can be tuned live from the dashboard.
        limit = _current_max_concurrent(cfg)
        with active_lock:
            at_capacity = active_count >= limit
        if at_capacity:
            time.sleep(1)
            continue
        item = r.blpop(queue_key, timeout=2)
        if not item:
            continue
        _, job_id = item
        with active_lock:
            active_count += 1
        threading.Thread(target=run_job, args=(job_id,), daemon=True).start()


if __name__ == "__main__":
    worker_loop()
