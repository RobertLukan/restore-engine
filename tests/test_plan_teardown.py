"""Tests for plan-run teardown, cancel, and drill powered-off policy."""

from __future__ import annotations

from typing import Any

import plans
from states import PlanRunStatus, RestoreState
from tests.test_plans import CFG, FakeRedis


def _seed_run(r: FakeRedis, *, status: str = PlanRunStatus.COMPLETED.value, drill: bool = False) -> dict[str, Any]:
    run = {
        "id": "run-1",
        "plan_id": "plan-1",
        "plan_name": "Demo",
        "status": status,
        "location_id": "loc-1",
        "node": "pve1",
        "nodes": ["pve1"],
        "storage": "local-lvm",
        "vmid_start": 200,
        "live_restore": False,
        "powered_off": True,
        "drill": drill,
        "auto_teardown": False,
        "restore_mode": "normal",
        "halt_on_error": True,
        "group_ids": ["g1"],
        "job_ids_by_group": [["job-a", "job-b"]],
        "assigned_targets": [
            {"vmid": 201, "node": "pve1"},
            {"vmid": 202, "node": "pve1"},
        ],
        "teardown_status": "",
        "teardown_results": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:10:00+00:00",
        "error": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:10:00+00:00",
    }
    plans.save_plan_run(r, CFG, run)
    for jid, vmid in (("job-a", 201), ("job-b", 202)):
        r.hset(
            f"{CFG['redis']['job_key_prefix']}{jid}",
            mapping={
                "job_id": jid,
                "state": RestoreState.COMPLETED.value,
                "proxmox_vmid": str(vmid),
                "proxmox_node": "pve1",
                "plan_run_id": "run-1",
                "pve_upid": "UPID:pve1:1:1:1:qmrestore:" + str(vmid) + ":",
                "managed_marked": "1",
            },
        )
    return run


def test_teardown_destroys_assigned_vmids() -> None:
    r = FakeRedis()
    _seed_run(r)
    destroyed: list[tuple[str, int]] = []

    def fake_destroy(_px: Any, node: str, vmid: int, **_kw: Any) -> None:
        destroyed.append((node, vmid))

    updated = plans.teardown_plan_run(
        r,
        CFG,
        "run-1",
        destroy_fn=fake_destroy,
        connect_fn=lambda _cfg: object(),
        find_node_fn=lambda *_a, **_k: "pve1",
    )
    assert updated["teardown_status"] == "completed"
    assert sorted(destroyed) == [("pve1", 201), ("pve1", 202)]
    assert all(x["ok"] for x in updated["teardown_results"])


def test_teardown_rejects_running_without_cancel() -> None:
    r = FakeRedis()
    _seed_run(r, status=PlanRunStatus.RUNNING.value)
    try:
        plans.teardown_plan_run(
            r,
            CFG,
            "run-1",
            destroy_fn=lambda *_a, **_k: None,
            connect_fn=lambda _cfg: object(),
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "RUNNING" in str(exc)


def test_teardown_partial_on_error() -> None:
    r = FakeRedis()
    _seed_run(r)

    def fake_destroy(_px: Any, node: str, vmid: int, **_kw: Any) -> None:
        if vmid == 202:
            raise RuntimeError("busy")

    updated = plans.teardown_plan_run(
        r,
        CFG,
        "run-1",
        destroy_fn=fake_destroy,
        connect_fn=lambda _cfg: object(),
    )
    assert updated["teardown_status"] == "partial"
    assert updated["teardown_results"][0]["ok"] is True
    assert updated["teardown_results"][1]["ok"] is False


def test_cancel_plan_run_marks_pending_and_stops_advance() -> None:
    r = FakeRedis()
    run = _seed_run(r, status=PlanRunStatus.RUNNING.value)
    run["pending_group_rows"] = [[{"backup_id": "x"}]]
    plans.save_plan_run(r, CFG, run)
    r.hset(
        f"{CFG['redis']['job_key_prefix']}job-a",
        mapping={"job_id": "job-a", "state": RestoreState.PENDING.value, "proxmox_vmid": "201", "proxmox_node": "pve1"},
    )
    r.hset(
        f"{CFG['redis']['job_key_prefix']}job-b",
        mapping={"job_id": "job-b", "state": RestoreState.RESTORING.value, "proxmox_vmid": "202", "proxmox_node": "pve1"},
    )
    r.rpush(CFG["redis"]["queue_key"], "job-a")

    out = plans.cancel_plan_run(r, CFG, "run-1")
    assert out["status"] == PlanRunStatus.CANCELLED.value
    assert out.get("pending_group_rows") in (None, [])
    a = r.hgetall(f"{CFG['redis']['job_key_prefix']}job-a")
    b = r.hgetall(f"{CFG['redis']['job_key_prefix']}job-b")
    assert a["state"] == RestoreState.CANCELLED.value
    assert b.get("cancel_requested") == "1"


def test_start_plan_run_drill_forces_powered_off() -> None:
    r = FakeRedis()
    plan = {
        "id": "plan-1",
        "name": "P",
        "group_ids": ["g1"],
        "location_id": "loc-1",
        "halt_on_error": True,
        "enabled": True,
        "verification": "NOT_VERIFIED",
    }
    location = {
        "id": "loc-1",
        "name": "Lab",
        "node": "pve1",
        "nodes": ["pve1"],
        "storage": "local-lvm",
        "storage_by_node": {},
        "vmid_start": 300,
        "bwlimit": 0,
        "live_restore": True,
        "restore_mode": "normal",
    }
    row = {
        "backup_id": "b1",
        "vmid": 10,
        "name": "vm10",
        "timestamp": "2026-01-01T00:00:00Z",
        "pve_storage": "pbs",
        "voltail": "vm/10/2026-01-01T00:00:00Z",
        "size_bytes": 1,
    }
    captured: dict[str, Any] = {}

    def fake_enqueue(_r: Any, _cfg: Any, rows: list, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "job_ids": ["j1"],
            "proxmox_vmids_assigned": [300],
            "proxmox_nodes_assigned": ["pve1"],
        }

    # Persist plan entity key so verification update works.
    plans._save_entity(
        r,
        CFG,
        key=plans.plan_key(CFG, plan["id"]),
        index=plans.plans_index(CFG),
        entity_id=plan["id"],
        data=plan,
    )

    run = plans.start_plan_run(
        r,
        CFG,
        plan=plan,
        location=location,
        cutoff="2026-01-01T00:00:00+00:00",
        group_rows=[[row]],
        enqueue_fn=fake_enqueue,
        drill=True,
        auto_teardown=True,
    )
    assert run["drill"] is True
    assert run["powered_off"] is True
    assert run["live_restore"] is False
    assert run["auto_teardown"] is True
    assert captured.get("live_restore") is False
    assert run["assigned_targets"] == [{"vmid": 300, "node": "pve1"}]


def test_require_verified_flag() -> None:
    assert plans.require_verified_to_run({}) is False
    assert plans.require_verified_to_run({"worker": {"require_verified_to_run": True}}) is True
    assert plans.require_verified_to_run({"plans": {"require_verified_to_run": True}}) is True
