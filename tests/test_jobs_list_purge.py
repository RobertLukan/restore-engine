"""Newest-first job listing and terminal purge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from job_hygiene import collect_jobs, paginate, purge_terminal_jobs
from states import RestoreState
from tests.test_plans import CFG, FakeRedis


def test_collect_jobs_newest_first() -> None:
    r = FakeRedis()
    prefix = CFG["redis"]["job_key_prefix"]
    older = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    newer = datetime.now(timezone.utc).isoformat()
    r.hset(
        f"{prefix}old",
        mapping={
            "job_id": "old",
            "state": RestoreState.COMPLETED.value,
            "created_at": older,
            "updated_at": older,
            "vm_name": "old-vm",
        },
    )
    r.hset(
        f"{prefix}new",
        mapping={
            "job_id": "new",
            "state": RestoreState.COMPLETED.value,
            "created_at": newer,
            "updated_at": newer,
            "vm_name": "new-vm",
        },
    )
    jobs = collect_jobs(r, CFG, record_fn=lambda d: d)
    assert [j["job_id"] for j in jobs] == ["new", "old"]
    page = paginate(jobs, offset=0, limit=1)
    assert page["items"][0]["job_id"] == "new"
    assert page["has_more"] is True
    assert page["total"] == 2


def test_purge_terminal_keeps_active() -> None:
    r = FakeRedis()
    prefix = CFG["redis"]["job_key_prefix"]
    suffix = CFG["redis"]["job_log_suffix"]
    now = datetime.now(timezone.utc).isoformat()
    r.hset(
        f"{prefix}done",
        mapping={"job_id": "done", "state": RestoreState.COMPLETED.value, "updated_at": now},
    )
    r.rpush(f"{prefix}done{suffix}", '{"m":1}')
    r.hset(
        f"{prefix}run",
        mapping={"job_id": "run", "state": RestoreState.RESTORING.value, "updated_at": now},
    )
    r.hset(
        f"{prefix}pend",
        mapping={"job_id": "pend", "state": RestoreState.PENDING.value, "updated_at": now},
    )
    result = purge_terminal_jobs(r, CFG)
    assert result["deleted"] == 1
    assert r.hgetall(f"{prefix}done") == {}
    assert r.hgetall(f"{prefix}run")["job_id"] == "run"
    assert r.hgetall(f"{prefix}pend")["job_id"] == "pend"
