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
        self.zsets: dict[str, dict[str, float]] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.kv.get(key)

    def set(self, key: str, value: str) -> bool:
        self.kv[key] = value
        return True

    def delete(self, key: str) -> int:
        removed = 0
        if key in self.kv:
            del self.kv[key]
            removed += 1
        if key in self.hashes:
            del self.hashes[key]
            removed += 1
        if key in self.lists:
            del self.lists[key]
            removed += 1
        if key in self.zsets:
            del self.zsets[key]
            removed += 1
        return removed

    def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    def lpop(self, key: str) -> str | None:
        lst = self.lists.get(key)
        if not lst:
            return None
        return lst.pop(0)

    def lrem(self, key: str, count: int, value: str) -> int:
        lst = self.lists.get(key, [])
        removed = 0
        if count == 0:
            new = [x for x in lst if x != value]
            removed = len(lst) - len(new)
            self.lists[key] = new
            return removed
        # Simplified: remove all matches when count != 0 for tests.
        new = []
        for x in lst:
            if x == value and removed < abs(count):
                removed += 1
                continue
            new.append(x)
        self.lists[key] = new
        return removed

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

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        z = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in z:
                added += 1
            z[str(member)] = float(score)
        return added

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        n = len(items)
        if end < 0:
            end = n + end
        end = min(end, n - 1)
        if start < 0:
            start = n + start
        if n == 0 or start > end or start >= n:
            return []
        return [m for m, _ in items[start : end + 1]]

    def zrevrange(self, key: str, start: int, end: int) -> list[str]:
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1], reverse=True)
        n = len(items)
        if end < 0:
            end = n + end
        end = min(end, n - 1)
        if start < 0:
            start = n + start
        if n == 0 or start > end or start >= n:
            return []
        return [m for m, _ in items[start : end + 1]]

    def zrem(self, key: str, *members: str) -> int:
        z = self.zsets.setdefault(key, {})
        removed = 0
        for m in members:
            if m in z:
                del z[m]
                removed += 1
        return removed

    def hset(
        self,
        key: str,
        field: Any = None,
        value: Any = None,
        mapping: dict[str, str] | None = None,
        **kwargs: str,
    ) -> int:
        h = self.hashes.setdefault(key, {})
        data: dict[str, str] = {}
        if mapping is not None:
            data.update({str(k): str(v) for k, v in mapping.items()})
        if isinstance(field, dict):
            data.update({str(k): str(v) for k, v in field.items()})
        elif field is not None and value is not None:
            data[str(field)] = str(value)
        data.update({str(k): str(v) for k, v in kwargs.items()})
        h.update(data)
        return len(data)

    def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = int(ttl)
        return True

    def lpush(self, key: str, *values: str) -> int:
        lst = self.lists.setdefault(key, [])
        for v in reversed(values):
            lst.insert(0, str(v))
        return len(lst)

    def ltrim(self, key: str, start: int, end: int) -> bool:
        lst = self.lists.get(key, [])
        n = len(lst)
        if end < 0:
            end = n + end
        end = min(end, n - 1)
        if start < 0:
            start = n + start
        if n == 0 or start > end:
            self.lists[key] = []
        else:
            self.lists[key] = lst[start : end + 1]
        return True

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        lst = self.lists.get(key, [])
        n = len(lst)
        if end < 0:
            end = n + end
        end = min(end, n - 1)
        if start < 0:
            start = n + start
        if n == 0 or start > end or start >= n:
            return []
        return list(lst[start : end + 1])

    def ping(self) -> bool:
        return True

    def rpush(self, key: str, *values: str) -> int:
        lst = self.lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    def scan_iter(self, match: str = "*", count: int = 10):
        # Minimal glob: prefix* only (enough for job key scans).
        prefix = match[:-1] if match.endswith("*") else match
        for key in list(self.hashes.keys()) + list(self.kv.keys()) + list(self.lists.keys()) + list(self.sets.keys()):
            if match.endswith("*"):
                if key.startswith(prefix):
                    yield key
            elif key == match:
                yield key

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

    def zadd(self, key: str, mapping: dict[str, float]) -> FakePipeline:
        self.ops.append(("zadd", key, mapping))
        return self

    def zrem(self, key: str, *members: str) -> FakePipeline:
        self.ops.append(("zrem", key, members))
        return self

    def hset(
        self,
        key: str,
        field: Any = None,
        value: Any = None,
        mapping: dict[str, str] | None = None,
        **kwargs: str,
    ) -> FakePipeline:
        data: dict[str, str] = {}
        if mapping is not None:
            data.update({str(k): str(v) for k, v in mapping.items()})
        if isinstance(field, dict):
            data.update({str(k): str(v) for k, v in field.items()})
        elif field is not None and value is not None:
            data[str(field)] = str(value)
        data.update({str(k): str(v) for k, v in kwargs.items()})
        self.ops.append(("hset", key, data))
        return self

    def lrem(self, key: str, count: int, value: str) -> FakePipeline:
        self.ops.append(("lrem", key, count, value))
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
            elif kind == "zadd":
                results.append(self.r.zadd(op[1], op[2]))
            elif kind == "zrem":
                results.append(self.r.zrem(op[1], *op[2]))
            elif kind == "hset":
                results.append(self.r.hset(op[1], mapping=op[2]))
            elif kind == "lrem":
                results.append(self.r.lrem(op[1], op[2], op[3]))
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
    with pytest.raises(ValueError, match="at least one of"):
        plans.normalize_group({"name": "empty"})


def test_normalize_group_name_patterns_and_ranges() -> None:
    g = plans.normalize_group(
        {
            "name": "web",
            "name_patterns": ["web-*", "re:^db-"],
            "vmid_ranges": ["100-110", {"start": 200, "end": 205}],
        }
    )
    assert g["name_patterns"] == ["web-*", "re:^db-"]
    assert g["vmid_ranges"] == [{"start": 100, "end": 110}, {"start": 200, "end": 205}]
    assert g["vmids"] == []


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


def test_resolve_group_rows_name_patterns_and_ranges() -> None:
    backups = [
        {
            "backup_id": "a",
            "vmid": 101,
            "name": "web-front",
            "timestamp": "2026-02-01T00:00:00Z",
            "source_id": "main",
        },
        {
            "backup_id": "b",
            "vmid": 150,
            "name": "db-primary",
            "timestamp": "2026-02-01T00:00:00Z",
            "source_id": "main",
        },
        {
            "backup_id": "c",
            "vmid": 250,
            "name": "util",
            "timestamp": "2026-02-01T00:00:00Z",
            "source_id": "main",
        },
        {
            "backup_id": "d",
            "vmid": 999,
            "name": "pinned",
            "timestamp": "2026-02-01T00:00:00Z",
            "source_id": "main",
        },
    ]
    # Name glob + range + explicit vmid (union).
    group = {
        "name": "mix",
        "tags": [],
        "vmids": [999],
        "name_patterns": ["web-*"],
        "vmid_ranges": [{"start": 140, "end": 160}],
        "source_ids": [],
    }
    rows = plans.resolve_group_rows(group, backups, cutoff="2026-12-31T23:59:59Z")
    assert [r["vmid"] for r in rows] == [101, 150, 999]

    regex_group = {
        "name": "re",
        "tags": [],
        "vmids": [],
        "name_patterns": ["re:^db-"],
        "vmid_ranges": [],
        "source_ids": [],
    }
    rows2 = plans.resolve_group_rows(regex_group, backups, cutoff="2026-12-31T23:59:59Z")
    assert [r["vmid"] for r in rows2] == [150]


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
