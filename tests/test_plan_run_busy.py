"""Only one plan run may be RUNNING at a time."""

from __future__ import annotations

from typing import Any

import pytest

import plans
from tests.test_plans import CFG, FakeRedis


def _plan_loc() -> tuple[dict[str, Any], dict[str, Any]]:
    plan = {
        "id": "plan-a",
        "name": "Plan A",
        "group_ids": ["g1"],
        "location_id": "loc1",
        "halt_on_error": True,
        "enabled": True,
    }
    location = {
        "id": "loc1",
        "name": "Loc",
        "node": "pve1",
        "nodes": ["pve1"],
        "storage": "local-lvm",
        "storage_by_node": {"pve1": "local-lvm"},
        "vmid_start": 5000,
        "bwlimit": 0,
        "live_restore": False,
        "restore_mode": "normal",
        "network_mode": "none",
        "isolated": True,
    }
    return plan, location


def _row(vmid: int = 101) -> dict[str, Any]:
    return {
        "backup_id": f"vm/{vmid}/2026-01-01T00:00:00Z",
        "vmid": vmid,
        "name": f"vm{vmid}",
        "pve_storage": "pbs",
        "voltail": f"vm/{vmid}/2026-01-01T00:00:00Z",
        "size_bytes": 1000,
        "source_label": "",
    }


def test_second_plan_run_blocked_while_running() -> None:
    r = FakeRedis()
    plan, location = _plan_loc()
    n = {"i": 0}

    def enqueue(_r: Any, _cfg: Any, rows: list, **_kwargs: Any) -> dict[str, Any]:
        n["i"] += 1
        return {
            "job_ids": [f"j{n['i']}"],
            "proxmox_vmids_assigned": [5000 + n["i"]],
            "proxmox_nodes_assigned": ["pve1"],
        }

    row = {**_row(101), "timestamp": "2026-01-01T00:00:00Z"}
    run1 = plans.start_plan_run(
        r,
        CFG,
        plan=plan,
        location=location,
        cutoff="9999-12-31T23:59:59Z",
        group_rows=[[row]],
        enqueue_fn=enqueue,
    )
    assert run1["status"] == plans.PlanRunStatus.RUNNING.value

    plan2 = {**plan, "id": "plan-b", "name": "Plan B"}
    with pytest.raises(plans.PlanRunBusyError, match="already in progress"):
        plans.start_plan_run(
            r,
            CFG,
            plan=plan2,
            location=location,
            cutoff="9999-12-31T23:59:59Z",
            group_rows=[[{**_row(102), "timestamp": "2026-01-01T00:00:00Z"}]],
            enqueue_fn=enqueue,
        )


def test_find_running_plan_run_none_when_idle() -> None:
    r = FakeRedis()
    assert plans.find_running_plan_run(r, CFG) is None
