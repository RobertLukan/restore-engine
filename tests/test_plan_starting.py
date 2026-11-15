"""RE-05: STARTING status prevents premature completion during enqueue."""

from __future__ import annotations

import json
from typing import Any

from plans import (
    _save_entity,
    advance_plan_runs,
    plan_key,
    plans_index,
    save_plan_run,
    start_plan_run,
)
from states import PlanRunStatus
from tests.test_plans import FakeRedis


CFG = {
    "redis": {
        "job_key_prefix": "restore:job:",
        "job_log_suffix": ":log",
        "queue_key": "restore:queue",
        "plans_index": "restore:plans",
        "plan_runs_index": "restore:planruns",
        "active_plan_runs_key": "restore:planruns:active",
        "plan_key_prefix": "restore:plan:",
        "plan_run_key_prefix": "restore:planrun:",
        "groups_index": "restore:groups",
        "group_key_prefix": "restore:group:",
        "locations_index": "restore:locations",
        "location_key_prefix": "restore:location:",
    }
}


def test_advance_skips_starting_empty_run() -> None:
    r = FakeRedis()
    run = {
        "id": "run-starting",
        "plan_id": "p1",
        "status": PlanRunStatus.STARTING.value,
        "job_ids_by_group": [[]],
        "pending_group_rows": [],
        "current_group_index": 0,
        "group_ids": ["g1"],
    }
    save_plan_run(r, CFG, run)

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise AssertionError("enqueue must not be called while STARTING")

    touched = advance_plan_runs(r, CFG, enqueue_fn=boom)
    assert touched == 0
    stored = json.loads(r.get("restore:planrun:run-starting") or "{}")
    assert stored["status"] == PlanRunStatus.STARTING.value
    assert "run-starting" in (r.smembers("restore:planruns:active") or set())


def test_start_publishes_running_only_after_enqueue() -> None:
    """Worker tick during enqueue must not finalize the run (RE-05)."""
    r = FakeRedis()
    mid_enqueue_status: list[str] = []

    def enqueue_fn(*_a: Any, **_k: Any) -> dict[str, Any]:
        advance_plan_runs(
            r,
            CFG,
            enqueue_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("nested")),
        )
        for key, val in list(r.kv.items()):
            if key.startswith("restore:planrun:"):
                mid_enqueue_status.append(json.loads(val)["status"])
        return {
            "enqueued": 1,
            "job_ids": ["job-1"],
            "proxmox_vmids_assigned": [100],
            "proxmox_nodes_assigned": ["pve"],
            "proxmox_storages_assigned": ["local-lvm"],
        }

    plan = {
        "id": "plan-1",
        "name": "P",
        "group_ids": ["g1"],
        "location_id": "loc-1",
        "halt_on_error": True,
    }
    location = {
        "id": "loc-1",
        "name": "L",
        "node": "pve",
        "nodes": ["pve"],
        "storage": "local-lvm",
        "storage_by_node": {"pve": "local-lvm"},
        "vmid_start": 100,
        "bwlimit": 0,
        "live_restore": False,
        "restore_mode": "normal",
        "power_on": False,
        "qga_wait_sec": 0,
        "network_mode": "none",
        "lab_bridge": "",
        "isolated": False,
        "http_check_url": "",
        "network_checks_from_orchestrator": False,
        "pve_cluster_id": "default",
    }
    group_rows = [
        [
            {
                "backup_id": "b1",
                "vmid": 1,
                "name": "vm1",
                "timestamp": "2026-01-01T00:00:00Z",
                "pve_storage": "pbs",
                "voltail": "vm/1/2026-01-01T00:00:00Z",
                "source_label": "",
                "size_bytes": 1,
            }
        ]
    ]
    _save_entity(
        r,
        CFG,
        key=plan_key(CFG, plan["id"]),
        index=plans_index(CFG),
        entity_id=plan["id"],
        data=plan,
    )

    run = start_plan_run(
        r,
        CFG,
        plan=plan,
        location=location,
        cutoff="9999-12-31T23:59:59Z",
        group_rows=group_rows,
        enqueue_fn=enqueue_fn,
        drill=False,
        auto_teardown=False,
    )
    assert PlanRunStatus.STARTING.value in mid_enqueue_status
    assert PlanRunStatus.COMPLETED.value not in mid_enqueue_status
    assert run["status"] == PlanRunStatus.RUNNING.value
    assert run["job_ids_by_group"][0] == ["job-1"]
