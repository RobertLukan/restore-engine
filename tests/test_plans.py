"""Unit tests for inventory groups, locations, plans, and member resolution."""

from __future__ import annotations

from typing import Any

import pytest

import plans


class FakeRedis:
    """Minimal Redis stand-in for plan CRUD tests (decode_responses=True style)."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}

    def get(self, key: str) -> str | None:
        return self.kv.get(key)

    def set(self, key: str, value: str) -> bool:
        self.kv[key] = value
        return True

    def delete(self, key: str) -> int:
        return 1 if self.kv.pop(key, None) is not None else 0

    def sadd(self, key: str, *members: str) -> int:
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.update(members)
        return len(s) - before

    def srem(self, key: str, *members: str) -> int:
        s = self.sets.setdefault(key, set())
        removed = 0
        for m in members:
            if m in s:
                s.remove(m)
                removed += 1
        return removed

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def hset(self, key: str, mapping: dict[str, str] | None = None, **kwargs: str) -> int:
        h = self.hashes.setdefault(key, {})
        data = dict(mapping or {})
        data.update(kwargs)
        h.update({str(k): str(v) for k, v in data.items()})
        return len(data)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def rpush(self, key: str, *values: str) -> int:
        lst = self.lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, r: FakeRedis) -> None:
        self.r = r
        self.ops: list[tuple] = []

    def set(self, key: str, value: str) -> FakePipeline:
        self.ops.append(("set", key, value))
        return self

    def sadd(self, key: str, *members: str) -> FakePipeline:
        self.ops.append(("sadd", key, members))
        return self

    def srem(self, key: str, *members: str) -> FakePipeline:
        self.ops.append(("srem", key, members))
        return self

    def delete(self, key: str) -> FakePipeline:
        self.ops.append(("delete", key))
        return self

    def execute(self) -> list[Any]:
        results: list[Any] = []
        for op in self.ops:
            kind = op[0]
            if kind == "set":
                results.append(self.r.set(op[1], op[2]))
            elif kind == "sadd":
                results.append(self.r.sadd(op[1], *op[2]))
            elif kind == "srem":
                results.append(self.r.srem(op[1], *op[2]))
            elif kind == "delete":
                results.append(self.r.delete(op[1]))
        self.ops.clear()
        return results


CFG = {
    "redis": {
        "url": "redis://127.0.0.1:6379/15",
        "queue_key": "restore:test:queue",
        "job_key_prefix": "restore:test:job:",
        "job_log_suffix": ":log",
        "group_key_prefix": "restore:test:group:",
        "location_key_prefix": "restore:test:location:",
        "plan_key_prefix": "restore:test:plan:",
        "plan_run_key_prefix": "restore:test:planrun:",
        "groups_index": "restore:test:groups",
        "locations_index": "restore:test:locations",
        "plans_index": "restore:test:plans",
        "plan_runs_index": "restore:test:planruns",
        "active_plan_runs_key": "restore:test:planruns:active",
    }
}


def test_group_location_plan_crud() -> None:
    r = FakeRedis()
    group = plans.create_group(r, CFG, {"name": "Tier1", "tags": ["prod", "app"]})
    assert group["id"]
    assert plans.get_group(r, CFG, group["id"])["name"] == "Tier1"

    loc = plans.create_location(
        r, CFG, {"name": "DR node", "node": "pve1", "storage": "local-lvm", "vmid_start": 200}
    )
    plan = plans.create_plan(
        r, CFG, {"name": "App stack", "group_ids": [group["id"]], "location_id": loc["id"]}
    )
    assert plan["verification"] == "NOT_VERIFIED"
    assert len(plans.list_plans(r, CFG)) == 1

    updated = plans.update_plan(
        r, CFG, plan["id"], {"name": "App stack", "group_ids": [group["id"]], "location_id": loc["id"]}
    )
    assert updated["name"] == "App stack"

    assert plans.delete_plan(r, CFG, plan["id"])
    assert plans.delete_group(r, CFG, group["id"])
    assert plans.delete_location(r, CFG, loc["id"])


def test_normalize_group_requires_match() -> None:
    with pytest.raises(ValueError, match="tag or one vmid"):
        plans.normalize_group({"name": "empty"})


def test_resolve_group_rows_tags_and_vmids() -> None:
    backups = [
        {
            "backup_id": "vm/10/2026-01-01T00:00:00Z",
            "vmid": 10,
            "name": "a",
            "timestamp": "2026-01-01T00:00:00Z",
            "pve_storage": "pbs",
            "voltail": "vm/10/2026-01-01T00:00:00Z",
            "source_id": "main",
        },
        {
            "backup_id": "vm/10/2026-02-01T00:00:00Z",
            "vmid": 10,
            "name": "a",
            "timestamp": "2026-02-01T00:00:00Z",
            "pve_storage": "pbs",
            "voltail": "vm/10/2026-02-01T00:00:00Z",
            "source_id": "main",
        },
        {
            "backup_id": "vm/20/2026-02-01T00:00:00Z",
            "vmid": 20,
            "name": "b",
            "timestamp": "2026-02-01T00:00:00Z",
            "pve_storage": "pbs",
            "voltail": "vm/20/2026-02-01T00:00:00Z",
            "source_id": "main",
        },
    ]
    tags = {
        "vm/10/2026-02-01T00:00:00Z": ["prod", "app"],
        "vm/20/2026-02-01T00:00:00Z": ["prod"],
    }
    group = {"name": "g", "tags": ["prod", "app"], "vmids": [], "source_ids": []}
    rows = plans.resolve_group_rows(
        group, backups, cutoff="2026-12-31T23:59:59Z", tags_by_backup_id=tags
    )
    assert [row["vmid"] for row in rows] == [10]

    vmid_group = {"name": "g2", "tags": [], "vmids": [20], "source_ids": []}
    rows2 = plans.resolve_group_rows(vmid_group, backups, cutoff="2026-12-31T23:59:59Z")
    assert [row["vmid"] for row in rows2] == [20]


def test_advance_plan_run_enqueues_second_group() -> None:
    from jobs import job_key
    from states import PlanRunStatus, RestoreState

    r = FakeRedis()
    cfg = CFG
    run_id = "run-1"
    run = {
        "id": run_id,
        "plan_id": "p1",
        "status": PlanRunStatus.RUNNING.value,
        "node": "pve",
        "storage": "local-lvm",
        "vmid_start": 100,
        "next_vmid": 101,
        "bwlimit": 0,
        "live_restore": False,
        "halt_on_error": True,
        "group_ids": ["g0", "g1"],
        "job_ids_by_group": [["job-a"], []],
        "current_group_index": 0,
        "pending_group_rows": [
            [],
            [
                {
                    "backup_id": "vm/20/2026-01-01T00:00:00Z",
                    "vmid": 20,
                    "name": "b",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "pve_storage": "pbs",
                    "voltail": "vm/20/2026-01-01T00:00:00Z",
                    "source_label": "",
                }
            ],
        ],
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "",
        "error": "",
    }
    plans.save_plan_run(r, cfg, run)
    r.hset(
        job_key(cfg, "job-a"),
        mapping={"job_id": "job-a", "state": RestoreState.COMPLETED.value},
    )

    enqueued: list[Any] = []

    def fake_enqueue(r_, cfg_, rows, **kwargs):
        enqueued.append(rows)
        jid = "job-b"
        r_.hset(job_key(cfg_, jid), mapping={"job_id": jid, "state": RestoreState.PENDING.value})
        return {"enqueued": 1, "job_ids": [jid], "proxmox_vmids_assigned": [101]}

    # First tick: advance index to group 1
    plans.advance_plan_runs(r, cfg, enqueue_fn=fake_enqueue, job_key_fn=job_key)
    # Second tick: enqueue group 1
    plans.advance_plan_runs(r, cfg, enqueue_fn=fake_enqueue, job_key_fn=job_key)

    assert len(enqueued) == 1
    updated = plans.get_plan_run(r, cfg, run_id)
    assert updated["job_ids_by_group"][1] == ["job-b"]
    assert updated["status"] == PlanRunStatus.RUNNING.value
