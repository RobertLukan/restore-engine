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
    apply_network_isolation,
    connect_proxmox,
    get_qemu_config,
    get_qemu_guest_hostname,
    hostname_matches_pve_name,
    mark_qemu_managed_by_tool,
    MANAGED_TAG,
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
    try:
        from job_hygiene import apply_job_ttl

        apply_job_ttl(r, cfg, job_id, state=state.value)
    except Exception:
        pass


def cancel_requested(r: redis.Redis, cfg: dict[str, Any], job_id: str) -> bool:
    return r.hget(redis_job_key(cfg, job_id), "cancel_requested") == "1"


def mark_cancelled(r: redis.Redis, cfg: dict[str, Any], job_id: str) -> None:
    set_state(r, cfg, job_id, RestoreState.CANCELLED)
    append_log(r, cfg, job_id, "INFO", "CANCELLED", "Job cancelled by operator")


def task_timeout_sec(cfg: dict[str, Any]) -> float:
    """Max seconds to wait for a PVE restore/start task. ``<= 0`` = no limit."""
    try:
        return float((cfg.get("worker") or {}).get("task_timeout_sec", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


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
    task_timeout = task_timeout_sec(cfg)
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
            timeout_sec=task_timeout,
            should_cancel=lambda: cancel_requested(r, cfg, job_id),
            on_tick=on_tick,
        )
    except TaskCancelled:
        mark_cancelled(r, cfg, job_id)
        append_log(r, cfg, job_id, "INFO", "CANCELLED", f"PVE restore task {upid} stopped")
        return
    except TimeoutError as exc:
        raise RuntimeError(str(exc)) from exc

    # Ownership stamp so teardown/overwrite never touches foreign cluster guests.
    # Fail the job if the stamp cannot be applied — unmarked VMs break reclaim/teardown.
    stamp_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            mark_qemu_managed_by_tool(
                proxmox,
                node,
                target_vmid,
                job_id=job_id,
                plan_run_id=str(data.get("plan_run_id") or ""),
            )
            r.hset(key, mapping={"managed_marked": "1", "updated_at": utc_now_iso()})
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                "RESTORING",
                f"Marked VMID {target_vmid} as restore-engine managed",
            )
            stamp_err = None
            break
        except Exception as exc:
            stamp_err = exc
            append_log(
                r,
                cfg,
                job_id,
                "WARN",
                "RESTORING",
                f"Ownership stamp attempt {attempt}/3 failed for VMID {target_vmid}: {exc}",
            )
            time.sleep(min(2.0 * attempt, 5.0))
    if stamp_err is not None:
        r.hset(key, mapping={"managed_marked": "0", "updated_at": utc_now_iso()})
        raise RuntimeError(
            f"Failed to stamp ownership marker on VMID {target_vmid} after retries: {stamp_err}. "
            "Guest was restored but is unmarked — reclaim/teardown will refuse it until tagged "
            f"'{MANAGED_TAG}' manually or the guest is removed in Proxmox."
        ) from stamp_err

    power_on = data.get("power_on", "0") == "1" or live_restore
    try:
        qga_wait_sec = max(0, int(data.get("qga_wait_sec") or 0))
    except (TypeError, ValueError):
        qga_wait_sec = 0
    if qga_wait_sec > 0:
        power_on = True

    net_mode = (data.get("network_mode") or "none").strip().lower()
    lab_bridge = (data.get("lab_bridge") or "").strip()
    if net_mode in {"unlink", "remap"}:
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            "RESTORING",
            f"Applying network isolation mode={net_mode}"
            + (f" bridge={lab_bridge}" if lab_bridge else ""),
        )
        try:
            changed = apply_network_isolation(
                proxmox, node, target_vmid, mode=net_mode, lab_bridge=lab_bridge
            )
            append_log(
                r,
                cfg,
                job_id,
                "INFO",
                "RESTORING",
                f"Network isolation updated: {', '.join(changed) or 'no net devices'}",
            )
        except Exception as exc:
            raise RuntimeError(f"Network isolation failed for VMID {target_vmid}: {exc}") from exc

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
                        timeout_sec=task_timeout,
                        should_cancel=lambda: cancel_requested(r, cfg, job_id),
                    )
                except TaskCancelled:
                    mark_cancelled(r, cfg, job_id)
                    append_log(r, cfg, job_id, "INFO", "CANCELLED", "VM start cancelled")
                    return
                except TimeoutError as exc:
                    raise RuntimeError(str(exc)) from exc
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
            _check_guest_hostname(r, cfg, job_id, key, proxmox, node, target_vmid)
        elif power_on:
            # Best-effort hostname capture when powered on without an explicit QGA wait.
            _check_guest_hostname(r, cfg, job_id, key, proxmox, node, target_vmid)

        http_url = (data.get("http_check_url") or "").strip()
        if http_url:
            append_log(r, cfg, job_id, "INFO", "RESTORING", f"HTTP check {http_url}")
            try:
                from urllib import request as urlrequest

                req = urlrequest.Request(http_url, method="GET")
                with urlrequest.urlopen(req, timeout=30) as resp:
                    code = int(getattr(resp, "status", None) or resp.getcode())
                    if code >= 400:
                        raise RuntimeError(f"HTTP check returned {code}")
                r.hset(key, mapping={"http_check_ok": "1", "updated_at": utc_now_iso()})
                append_log(r, cfg, job_id, "INFO", "RESTORING", f"HTTP check OK ({code})")
            except Exception as exc:
                r.hset(key, mapping={"http_check_ok": "0", "updated_at": utc_now_iso()})
                raise RuntimeError(f"HTTP check failed: {exc}") from exc
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


def _check_guest_hostname(
    r: redis.Redis,
    cfg: dict[str, Any],
    job_id: str,
    key: str,
    proxmox: Any,
    node: str,
    target_vmid: int,
) -> None:
    """Compare guest OS hostname to PVE VM name. Mismatch is a warning, never a hard fail."""
    pve_name = ""
    try:
        pve_cfg = get_qemu_config(proxmox, node, target_vmid)
        pve_name = str(pve_cfg.get("name") or "").strip()
    except Exception as exc:
        append_log(
            r,
            cfg,
            job_id,
            "WARN",
            "RESTORING",
            f"Could not read PVE name for VMID {target_vmid}: {exc}",
        )
    guest_host = get_qemu_guest_hostname(proxmox, node, target_vmid)
    mapping: dict[str, str] = {
        "pve_name": pve_name,
        "guest_hostname": guest_host or "",
        "updated_at": utc_now_iso(),
    }
    if not guest_host:
        mapping["hostname_match"] = ""
        mapping["hostname_warning"] = ""
        r.hset(key, mapping=mapping)
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            "RESTORING",
            f"Guest hostname unavailable via QGA (PVE name={pve_name or '—'})",
        )
        return
    match = hostname_matches_pve_name(guest_host, pve_name) if pve_name else False
    mapping["hostname_match"] = "1" if match else "0"
    if match:
        mapping["hostname_warning"] = ""
        r.hset(key, mapping=mapping)
        append_log(
            r,
            cfg,
            job_id,
            "INFO",
            "RESTORING",
            f"Guest hostname '{guest_host}' matches PVE name '{pve_name}'",
        )
        return
    warning = (
        f"Guest hostname '{guest_host}' does not match PVE name '{pve_name or '—'}'"
    )
    mapping["hostname_warning"] = warning
    r.hset(key, mapping=mapping)
    append_log(r, cfg, job_id, "WARN", "RESTORING", warning)


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

    import concurrency as concurrency_module
    from pbs_client import list_vm_backups

    def run_job(job_id: str) -> None:
        try:
            process_job(r, cfg, job_id)
        except Exception as exc:
            log.exception("Job %s failed", job_id)
            set_state(r, cfg, job_id, RestoreState.FAILED, error=str(exc), progress="0")
            append_log(r, cfg, job_id, "ERROR", "FAILED", str(exc))
            try:
                import notifications as notifications_module

                data = r.hgetall(redis_job_key(cfg, job_id)) or {}
                notifications_module.notify_job_failed(cfg, job=data)
            except Exception:
                pass
        finally:
            concurrency_module.release_slot(r, cfg)

    def _start_scheduled(plan: dict[str, Any], location: dict[str, Any]) -> dict[str, Any]:
        from datetime import datetime, timezone

        cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        backups = list_vm_backups(cfg)
        groups: list[dict[str, Any]] = []
        for gid in plan.get("group_ids") or []:
            group = plans_module.get_group(r, cfg, str(gid))
            if not group:
                raise RuntimeError(f"Missing group in plan: {gid}")
            groups.append(group)
        need_tags = any(g.get("tags") for g in groups)
        tags_by_id: dict[str, list[str]] = {}
        if need_tags:
            # Latest-per-vmid under cutoff (same candidates as readiness / manual run).
            best: dict[int, dict[str, Any]] = {}
            for row in backups:
                if row.get("timestamp", "") > cutoff:
                    continue
                try:
                    vmid = int(row["vmid"])
                except (TypeError, ValueError, KeyError):
                    continue
                cur = best.get(vmid)
                if cur is None or row["timestamp"] > cur["timestamp"]:
                    best[vmid] = row
            candidates = list(best.values())
            node0 = ""
            for n in location.get("nodes") or []:
                if str(n).strip():
                    node0 = str(n).strip()
                    break
            if not node0:
                node0 = str(location.get("node") or "").strip()
            if not node0:
                raise RuntimeError("Scheduled plan needs a location node to resolve guest tags")
            if candidates:
                proxmox = connect_proxmox(cfg)
                tags_by_id, tag_errors = plans_module._resolve_tags_cached(
                    r, cfg, candidates, node0, proxmox
                )
                if tag_errors:
                    log.warning(
                        "Scheduled plan %s: tag resolve failed for %s backup(s)",
                        plan.get("id"),
                        len(tag_errors),
                    )
        group_rows = [
            plans_module.resolve_group_rows(
                group, backups, cutoff=cutoff, tags_by_backup_id=tags_by_id
            )
            for group in groups
        ]
        if not any(group_rows):
            raise RuntimeError("Scheduled plan resolved to zero backups")
        drill = bool(plan.get("schedule_drill", True))
        return plans_module.start_plan_run(
            r,
            cfg,
            plan=plan,
            location=location,
            cutoff=cutoff,
            group_rows=group_rows,
            enqueue_fn=enqueue_restores,
            drill=drill,
            auto_teardown=drill,
            power_on=False,
            qga_wait_sec=0,
        )

    log.info("Restore worker started (initial max_concurrent_restores=%s)", _current_max_concurrent(cfg))
    try:
        concurrency_module.reconcile_slots(r, cfg)
    except Exception:
        log.exception("Concurrency slot reconcile failed at startup")
    last_purge = 0.0
    while True:
        concurrency_module.touch_worker_heartbeat(r, cfg, ttl_sec=60)

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

        # Scheduled drills / plan runs.
        try:
            n = plans_module.tick_scheduled_plan_runs(
                r,
                cfg,
                start_fn=_start_scheduled,
                on_error=lambda plan, exc: log.exception(
                    "Scheduled plan run failed for %s", plan.get("id")
                ),
            )
            if n:
                log.info("Scheduled plan tick started %s run(s)", n)
        except Exception:
            log.exception("Scheduled plan tick failed")

        # Occasional legacy job purge.
        now = time.time()
        if now - last_purge > 300:
            last_purge = now
            try:
                from job_hygiene import purge_expired_scan

                purged = purge_expired_scan(r, cfg)
                if purged:
                    log.info("Purged %s expired job(s)", purged)
            except Exception:
                log.exception("Job purge failed")

        # Re-read the limit each iteration so it can be tuned live from the dashboard.
        limit = _current_max_concurrent(cfg)
        if not concurrency_module.try_acquire_slot(r, cfg, limit=limit):
            time.sleep(1)
            continue
        item = r.blpop(queue_key, timeout=2)
        if not item:
            concurrency_module.release_slot(r, cfg)
            continue
        _, job_id = item
        threading.Thread(target=run_job, args=(job_id,), daemon=True).start()


if __name__ == "__main__":
    worker_loop()
