"""HTTP tests for plan check / run / cancel routes."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from states import PlanRunStatus, PlanVerification
from tests.test_plans import FakeRedis
import plans as plans_module


def client(main_module: Any) -> TestClient:
    return TestClient(main_module.app)


def _login(c: TestClient) -> None:
    assert c.post("/api/auth/login", json={"password": "test-dashboard-secret"}).status_code == 200


def _wire_redis(main_module: Any, monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    r = FakeRedis()
    monkeypatch.setattr(main_module, "redis_client", lambda: r)
    return r


def _create_plan_via_api(c: TestClient) -> dict[str, Any]:
    g = c.post("/api/groups", json={"name": "g1", "tags": [], "vmids": [109]})
    assert g.status_code == 200, g.text
    loc = c.post(
        "/api/locations",
        json={
            "name": "lab",
            "node": "pve",
            "nodes": ["pve"],
            "storage": "local-lvm",
            "storage_by_node": {"pve": "local-lvm"},
            "vmid_start": 3500,
            "restore_mode": "normal",
            "network_mode": "none",
        },
    )
    assert loc.status_code == 200, loc.text
    plan = c.post(
        "/api/plans",
        json={
            "name": "P1",
            "group_ids": [g.json()["id"]],
            "location_id": loc.json()["id"],
            "enabled": True,
        },
    )
    assert plan.status_code == 200, plan.text
    return plan.json()


def test_plans_check_run_cancel(
    main_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _wire_redis(main_module, monkeypatch)
    c = client(main_module)
    _login(c)
    plan = _create_plan_via_api(c)
    plan_id = plan["id"]

    def fake_check(r_in: Any, cfg: Any, plan_in: dict[str, Any], **_k: Any) -> tuple[dict, dict]:
        updated = {
            **plan_in,
            "verification": PlanVerification.VERIFIED.value,
            "last_check_at": "2026-08-26T00:00:00+00:00",
            "last_check_ok": True,
        }
        plans_module._save_entity(
            r_in,
            cfg,
            key=plans_module.plan_key(cfg, plan_id),
            index=plans_module.plans_index(cfg),
            entity_id=plan_id,
            data=updated,
        )
        check = {"ok": True, "summary": "ready", "items": []}
        return updated, check

    monkeypatch.setattr(main_module.plans_module, "run_plan_readiness", fake_check)

    check_res = c.post(f"/api/plans/{plan_id}/check", json={})
    assert check_res.status_code == 200, check_res.text
    assert check_res.json()["check"]["ok"] is True
    assert check_res.json()["plan"]["verification"] == PlanVerification.VERIFIED.value

    backup_row = {
        "backup_id": "main|vm/109/2026-08-07T10:00:00Z",
        "vmid": 109,
        "name": "vm-109",
        "timestamp": "2026-08-07T10:00:00Z",
        "pve_storage": "pbs-main",
        "voltail": "vm/109/2026-08-07T10:00:00Z",
        "source_id": "main",
        "size_bytes": 1000,
    }

    monkeypatch.setattr(
        main_module,
        "_resolve_plan_group_rows",
        lambda *_a, **_k: [[backup_row]],
    )

    def fake_start(r_in: Any, cfg: Any, **kwargs: Any) -> dict[str, Any]:
        run = {
            "id": "run-http-1",
            "plan_id": kwargs["plan"]["id"],
            "plan_name": kwargs["plan"].get("name", ""),
            "status": PlanRunStatus.RUNNING.value,
            "location_id": kwargs["location"]["id"],
            "location_name": kwargs["location"].get("name", ""),
            "node": kwargs["location"]["node"],
            "nodes": [kwargs["location"]["node"]],
            "storage": kwargs["location"]["storage"],
            "job_ids_by_group": [["job-pending-1"]],
            "pending_group_rows": [],
            "current_group_index": 0,
            "drill": False,
            "created_at": "2026-08-26T00:00:00+00:00",
            "updated_at": "2026-08-26T00:00:00+00:00",
        }
        plans_module.save_plan_run(r_in, cfg, run)
        prefix = cfg["redis"]["job_key_prefix"]
        r_in.hset(
            f"{prefix}job-pending-1",
            mapping={
                "job_id": "job-pending-1",
                "state": "PENDING",
                "backup_id": backup_row["backup_id"],
                "vm_name": "vm-109",
                "source_vmid": "109",
                "proxmox_vmid": "3500",
                "proxmox_node": "pve",
                "proxmox_storage": "local-lvm",
                "live_restore": "0",
                "created_at": "2026-08-26T00:00:00+00:00",
                "updated_at": "2026-08-26T00:00:00+00:00",
            },
        )
        r_in.rpush(cfg["redis"]["queue_key"], "job-pending-1")
        return run

    monkeypatch.setattr(main_module.plans_module, "start_plan_run", fake_start)

    preview = c.post(
        f"/api/plans/{plan_id}/members",
        json={},
    )
    assert preview.status_code == 200, preview.text
    prev = preview.json()
    assert prev["member_count"] == 1
    assert prev["members"][0]["vmid"] == 109
    assert prev["members"][0]["name"] == "vm-109"
    assert prev["groups"][0]["member_count"] == 1

    run_res = c.post(
        f"/api/plans/{plan_id}/run",
        json={"allow_unverified": True, "drill": True},
    )
    assert run_res.status_code == 200, run_res.text
    run_body = run_res.json()
    assert run_body["id"] == "run-http-1"
    assert run_body["status"] == PlanRunStatus.RUNNING.value

    cancel_res = c.post("/api/plan-runs/run-http-1/cancel")
    assert cancel_res.status_code == 200, cancel_res.text
    assert cancel_res.json()["status"] == PlanRunStatus.CANCELLED.value
    job = r.hgetall(f"{main_module.load_config()['redis']['job_key_prefix']}job-pending-1")
    assert job["state"] == "CANCELLED"
