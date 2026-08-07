"""Queue pause / resume / stop and job stats."""

from __future__ import annotations

from states import RestoreState
from tests.test_plans import CFG, FakeRedis
from queue_control import (
    collect_job_stats,
    drain_pending_jobs,
    is_queue_paused,
    set_queue_paused,
)


def test_pause_resume_flag() -> None:
    r = FakeRedis()
    cfg = CFG
    assert is_queue_paused(r, cfg) is False
    set_queue_paused(r, cfg, True)
    assert is_queue_paused(r, cfg) is True
    set_queue_paused(r, cfg, False)
    assert is_queue_paused(r, cfg) is False


def test_drain_pending_cancels_queue_and_hashes() -> None:
    r = FakeRedis()
    cfg = CFG
    prefix = cfg["redis"]["job_key_prefix"]
    queue = cfg["redis"]["queue_key"]

    r.hset(
        f"{prefix}a",
        mapping={"job_id": "a", "state": RestoreState.PENDING.value, "proxmox_node": "pve"},
    )
    r.hset(
        f"{prefix}b",
        mapping={"job_id": "b", "state": RestoreState.RESTORING.value, "proxmox_node": "pve", "speed_bps": "1048576", "eta_sec": "30"},
    )
    r.hset(
        f"{prefix}c",
        mapping={"job_id": "c", "state": RestoreState.PENDING.value, "proxmox_node": "pve"},
    )
    r.rpush(queue, "a", "c")

    result = drain_pending_jobs(r, cfg)
    assert result["cancelled"] >= 2
    assert is_queue_paused(r, cfg) is True
    assert r.llen(queue) == 0
    assert r.hgetall(f"{prefix}a")["state"] == RestoreState.CANCELLED.value
    assert r.hgetall(f"{prefix}c")["state"] == RestoreState.CANCELLED.value
    assert r.hgetall(f"{prefix}b")["state"] == RestoreState.RESTORING.value


def test_collect_job_stats() -> None:
    r = FakeRedis()
    cfg = CFG
    prefix = cfg["redis"]["job_key_prefix"]
    r.hset(
        f"{prefix}1",
        mapping={
            "job_id": "1",
            "state": RestoreState.RESTORING.value,
            "speed_bps": "2097152",
            "eta_sec": "40",
        },
    )
    r.hset(f"{prefix}2", mapping={"job_id": "2", "state": RestoreState.PENDING.value})
    set_queue_paused(r, cfg, True)
    stats = collect_job_stats(r, cfg, max_concurrent=3)
    assert stats["active"] == 1
    assert stats["pending"] == 1
    assert stats["paused"] is True
    assert stats["max_concurrent"] == 3
    assert stats["aggregate_speed_bps"] == 2097152
    assert stats["worst_eta_sec"] == 40
