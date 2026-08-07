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

from pve_client import (
    TaskCancelled,
    connect_proxmox,
    start_qemu_vm,
    stop_qemu_vm,
    submit_restore,
    wait_for_qemu_agent,
    wait_for_task,
)
from progress_parse import metrics_mapping_from_tick, parse_restore_progress, safe_float, safe_int
from pbs_wire import estimate_wire_compression_for_job
from queue_control import is_queue_paused
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
    unique = data.get("unique", "1") != "0"
    force = data.get("force", "1") != "0"
    restore_mode = (data.get("restore_mode") or "normal").strip().lower()
    archive = data.get("archive")
    if not archive:
        raise RuntimeError(f"Job {job_id} has no archive volid stored")

    set_state(r, cfg, job_id, RestoreState.RESTORING, progress="5")
    append_log(
        r,
        cfg,
        job_id,
        "INFO",
        "RESTORING",
        f"Submitting {restore_mode} restore for {backup_id} -> VMID {target_vmid}"
        f" (unique={int(unique)}, force={int(force)}, live={int(live_restore)})",
    )

    wire_ratio: float | None = None
    disk_sparsity: float | None = None
    try:
        sample_n = int((cfg.get("worker") or {}).get("wire_sample_chunks", 24))
    except (TypeError, ValueError):
        sample_n = 24
    est = estimate_wire_compression_for_job(cfg, backup_id, sample_size=max(4, sample_n))
    if est is not None:
        wire_ratio = float(est.ratio)
        disk_sparsity = float(est.sparsity_ratio)
        r.hset(
            key,
            mapping={
                "wire_compression_ratio": f"{wire_ratio:.6f}",
                "wire_sample_chunks": str(est.samples),
                "disk_sparsity_ratio": f"{disk_sparsity:.6f}",
                "disk_nonzero_chunks": str(est.nonzero_positions),
                "disk_total_chunks": str(est.total_positions),
                "updated_at": utc_now_iso(),
            },
        )
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            "RESTORING",
            (
                f"Wire sample ≈ compression {wire_ratio:.2f}, sparsity {disk_sparsity:.2f} "
                f"({est.nonzero_positions}/{est.total_positions} non-zero chunks, "
                f"{est.samples} sampled)"
            ),
        )
    else:
        append_log(
            r,
            cfg,
            job_id,
            "WARN",
            "RESTORING",
            "Wire compression sample unavailable; showing logical non-zero rates",
        )

    proxmox = connect_proxmox(cfg)
    upid = submit_restore(
        proxmox,
        node=node,
        target_vmid=target_vmid,
        archive=archive,
        target_storage=target_storage,
        live_restore=live_restore,
        bwlimit=bwlimit,
        unique=unique,
        force=force,
    )
    started = utc_now_iso()
    r.hset(
        key,
        mapping={
            "pve_upid": upid,
            "progress": "15",
            "restore_started_at": started,
            "updated_at": started,
            "progress_samples": "0",
        },
    )
    append_log(r, cfg, job_id, "INFO", "RESTORING", f"PVE task started: {upid}")

    poll = float((cfg.get("worker") or {}).get("task_poll_interval_sec", 3))
    tick_state: dict[str, Any] = {
        "last_tick": time.time(),
        "prev_bytes": None,
        "prev_speed": None,
        "prev_log_network_bytes": None,
        "prev_network_speed": None,
        "samples": 0,
    }
    backup_size = safe_int(data.get("backup_size_bytes"), 0)

    def on_tick(_status: dict[str, Any], new_lines: list[str]) -> None:
        now = time.time()
        dt = max(0.001, now - float(tick_state["last_tick"]))
        tick_state["last_tick"] = now
        if not new_lines:
            return
        parsed = parse_restore_progress(new_lines)
        tick_state["samples"] = int(tick_state["samples"]) + 1
        mapping = metrics_mapping_from_tick(
            parsed=parsed,
            restore_started_at=started,
            prev_bytes_done=tick_state["prev_bytes"],
            prev_speed_bps=tick_state["prev_speed"],
            sample_count=int(tick_state["samples"]),
            tick_dt_sec=dt,
            backup_size_bytes=backup_size,
            prev_network_bytes=tick_state["prev_log_network_bytes"],
            prev_network_speed_bps=tick_state["prev_network_speed"],
            wire_compression_ratio=wire_ratio,
            disk_sparsity_ratio=disk_sparsity,
        )
        if "bytes_done" in mapping:
            tick_state["prev_bytes"] = safe_int(mapping["bytes_done"], 0)
        if "speed_bps" in mapping:
            tick_state["prev_speed"] = safe_float(mapping["speed_bps"], 0.0)
        if parsed.network_bytes_done is not None:
            tick_state["prev_log_network_bytes"] = int(parsed.network_bytes_done)
        if "network_speed_bps" in mapping:
            tick_state["prev_network_speed"] = safe_float(mapping["network_speed_bps"], 0.0)
        mapping["progress_samples"] = str(tick_state["samples"])
        mapping["updated_at"] = utc_now_iso()
        r.hset(key, mapping=mapping)

    try:
        wait_for_task(
            proxmox,
            node,
            upid,
            poll_interval_sec=poll,
            should_cancel=lambda: cancel_requested(r, cfg, job_id),
            on_tick=on_tick,
        )
    except TaskCancelled:
        mark_cancelled(r, cfg, job_id)
        append_log(r, cfg, job_id, "INFO", "CANCELLED", f"PVE restore task {upid} stopped")
        return

    power_on = data.get("power_on", "0") == "1" or live_restore
    try:
        qga_wait_sec = max(0, int(data.get("qga_wait_sec") or 0))
    except (TypeError, ValueError):
        qga_wait_sec = 0
    if qga_wait_sec > 0:
        power_on = True

    if power_on:
        if not live_restore:
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                "RESTORING",
                f"Starting VMID {target_vmid} after restore",
            )
            try:
                start_upid = start_qemu_vm(proxmox, node, target_vmid)
            except Exception as exc:
                raise RuntimeError(f"Failed to start VMID {target_vmid}: {exc}") from exc
            if start_upid:
                try:
                    wait_for_task(
                        proxmox,
                        node,
                        start_upid,
                        poll_interval_sec=poll,
                        should_cancel=lambda: cancel_requested(r, cfg, job_id),
                    )
                except TaskCancelled:
                    mark_cancelled(r, cfg, job_id)
                    append_log(r, cfg, job_id, "INFO", "CANCELLED", "VM start cancelled")
                    return
            r.hset(key, mapping={"powered_off": "0", "updated_at": utc_now_iso()})

        if qga_wait_sec > 0:
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                "RESTORING",
                f"Waiting up to {qga_wait_sec}s for QEMU guest agent on VMID {target_vmid}",
            )
            try:
                waited = wait_for_qemu_agent(
                    proxmox,
                    node,
                    target_vmid,
                    timeout_sec=float(qga_wait_sec),
                    poll_interval_sec=min(5.0, max(1.0, poll)),
                    should_cancel=lambda: cancel_requested(r, cfg, job_id),
                )
            except TaskCancelled:
                mark_cancelled(r, cfg, job_id)
                append_log(r, cfg, job_id, "INFO", "CANCELLED", "QGA wait cancelled")
                return
            except TimeoutError as exc:
                r.hset(
                    key,
                    mapping={
                        "qga_ok": "0",
                        "qga_waited_sec": str(qga_wait_sec),
                        "updated_at": utc_now_iso(),
                    },
                )
                raise RuntimeError(str(exc)) from exc
            r.hset(
                key,
                mapping={
                    "qga_ok": "1",
                    "qga_waited_sec": f"{waited:.1f}",
                    "updated_at": utc_now_iso(),
                },
            )
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                "RESTORING",
                f"QEMU guest agent OK after {waited:.1f}s",
            )
    else:
        # Powered-off policy: ensure guest is stopped after restore.
        stop_qemu_vm(proxmox, node, target_vmid)
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            "RESTORING",
            f"Ensured VMID {target_vmid} is powered off",
        )

    set_state(
        r,
        cfg,
        job_id,
        RestoreState.COMPLETED,
        progress="100",
        eta_sec="0",
        speed_bps="",
    )
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
        # Pause: no new claims and no plan enqueue (in-flight keep running).
        if is_queue_paused(r, cfg):
            time.sleep(1)
            continue

        # Advance ordered plan runs (next group when current group is terminal).
        try:
            plans_module.advance_plan_runs(r, cfg, enqueue_fn=enqueue_restores, job_key_fn=redis_job_key)
        except Exception:
            log.exception("Plan run advancement failed")

        # Daily (configurable) readiness tick for ENABLED plans.
        try:
            n = plans_module.tick_plan_readiness(
                r,
                cfg,
                on_error=lambda plan, exc: log.exception(
                    "Plan readiness check failed for %s", plan.get("id")
                ),
            )
            if n:
                log.info("Plan readiness tick checked %s plan(s)", n)
        except Exception:
            log.exception("Plan readiness tick failed")

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
