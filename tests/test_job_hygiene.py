"""Tests for job TTL hygiene helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from job_hygiene import apply_job_ttl, job_ttl_sec, purge_expired_scan
from states import RestoreState
from tests.test_plans import CFG, FakeRedis


def test_job_ttl_sec_defaults() -> None:
    assert job_ttl_sec({"worker": {}}) == 14 * 86400
    assert job_ttl_sec({"worker": {"job_ttl_sec": 0}}) == 0
    assert job_ttl_sec({"worker": {"job_ttl_sec": 3600}}) == 3600


def test_apply_job_ttl_on_terminal() -> None:
    r = FakeRedis()
    cfg = {**CFG, "worker": {"job_ttl_sec": 120}}
    job_id = "ttl1"
    prefix = cfg["redis"]["job_key_prefix"]
    suffix = cfg["redis"]["job_log_suffix"]
    key = f"{prefix}{job_id}"
    log_key = f"{key}{suffix}"
    r.hset(key, mapping={"job_id": job_id, "state": RestoreState.COMPLETED.value})
    r.rpush(log_key, '{"m":"x"}')

    apply_job_ttl(r, cfg, job_id, state=RestoreState.PENDING.value)
    assert key not in r.ttls

    apply_job_ttl(r, cfg, job_id, state=RestoreState.COMPLETED.value)
    assert r.ttls[key] == 120
    assert r.ttls[log_key] == 120


def test_purge_expired_scan_removes_stale_terminal() -> None:
    r = FakeRedis()
    cfg = {**CFG, "worker": {"job_ttl_sec": 3600}}
    prefix = cfg["redis"]["job_key_prefix"]
    suffix = cfg["redis"]["job_log_suffix"]
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()

    r.hset(
        f"{prefix}old",
        mapping={
            "job_id": "old",
            "state": RestoreState.FAILED.value,
            "updated_at": old,
        },
    )
    r.rpush(f"{prefix}old{suffix}", "log")
    r.hset(
        f"{prefix}new",
        mapping={
            "job_id": "new",
            "state": RestoreState.COMPLETED.value,
            "updated_at": fresh,
        },
    )
    r.hset(
        f"{prefix}active",
        mapping={
            "job_id": "active",
            "state": RestoreState.RESTORING.value,
            "updated_at": old,
        },
    )

    purged = purge_expired_scan(r, cfg)
    assert purged == 1
    assert r.hgetall(f"{prefix}old") == {}
    assert r.lists.get(f"{prefix}old{suffix}") is None or r.llen(f"{prefix}old{suffix}") == 0
    assert r.hgetall(f"{prefix}new")["job_id"] == "new"
    assert r.hgetall(f"{prefix}active")["state"] == RestoreState.RESTORING.value
