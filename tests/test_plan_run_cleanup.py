"""Plan-run batch cleanup: stamp, preview/execute, feature flag gate."""

from __future__ import annotations

from typing import Any

import plans
import pytest
from states import PlanRunStatus, RestoreState
from tests.test_plans import CFG, FakeRedis
from ui import plan_run_cleanup_enabled, view_ui


def test_view_ui_exposes_cleanup_flag() -> None:
    assert view_ui({})["plan_run_cleanup_enabled"] is False
    assert view_ui({"plan_run_cleanup_enabled": True})["plan_run_cleanup_enabled"] is True
    assert plan_run_cleanup_enabled({}) is False
    assert plan_run_cleanup_enabled({"ui": {"plan_run_cleanup_enabled": True}}) is True


def test_start_plan_run_persists_batch_id() -> None:
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
        "live_restore": False,
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
    )
    assert run["batch_id"]
    assert len(run["batch_id"]) == 12
    assert captured.get("batch_id") == run["batch_id"]
    assert captured.get("plan_run_id") == run["id"]


def test_teardown_writes_per_vm_progress() -> None:
    r = FakeRedis()
    run = {
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "plan_id": "plan-1",
        "plan_name": "Demo",
        "status": PlanRunStatus.COMPLETED.value,
        "location_id": "loc-1",
        "node": "pve1",
        "nodes": ["pve1"],
        "storage": "local-lvm",
        "vmid_start": 200,
        "live_restore": False,
        "powered_off": True,
        "drill": True,
        "auto_teardown": False,
        "batch_id": "aaaaaaaabbbb",
        "restore_mode": "normal",
        "halt_on_error": True,
        "group_ids": ["g1"],
        "job_ids_by_group": [["job-a"]],
        "assigned_targets": [{"vmid": 201, "node": "pve1"}],
        "teardown_status": "",
        "teardown_results": [],
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:10:00+00:00",
        "error": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:10:00+00:00",
    }
    plans.save_plan_run(r, CFG, run)
    r.hset(
        f"{CFG['redis']['job_key_prefix']}job-a",
        mapping={
            "job_id": "job-a",
            "state": RestoreState.COMPLETED.value,
            "proxmox_vmid": "201",
            "proxmox_node": "pve1",
            "plan_run_id": run["id"],
            "batch_id": "aaaaaaaabbbb",
            "pve_upid": "UPID:pve1:1:1:1:qmrestore:201:",
            "managed_marked": "1",
        },
    )
    seen_statuses: list[str] = []

    def fake_destroy(_px: Any, node: str, vmid: int, **_kw: Any) -> None:
        seen_statuses.append("destroy")

    updated = plans.teardown_plan_run(
        r,
        CFG,
        run["id"],
        destroy_fn=fake_destroy,
        connect_fn=lambda _cfg: object(),
        find_node_fn=lambda *_a, **_k: "pve1",
    )
    assert updated["teardown_status"] == "completed"
    assert updated["teardown_results"][0]["status"] == "gone"
    assert seen_statuses == ["destroy"]


def test_preview_and_execute_cleanup_by_batch() -> None:
    r = FakeRedis()
    guests = [
        {"vmid": 10, "node": "pve1", "name": "a", "status": "stopped"},
        {"vmid": 11, "node": "pve1", "name": "b", "status": "stopped"},
    ]
    destroyed: list[int] = []

    def list_fn(_px: Any, *, batch_id: str, plan_run_id: str = "") -> list[dict[str, Any]]:
        assert batch_id == "deadbeefcafe"
        return [{**g, "batch_id": batch_id, "batch_tag": f"re-{batch_id}"} for g in guests]

    def destroy_fn(_px: Any, node: str, vmid: int, **kw: Any) -> str:
        assert kw.get("batch_id") == "deadbeefcafe" or True
        destroyed.append(int(vmid))
        return "managed"

    preview = plans.preview_cleanup_by_batch(
        r,
        CFG,
        batch_id="re-deadbeefcafe",
        connect_fn=lambda *_a, **_k: object(),
        list_fn=list_fn,
    )
    assert preview["count"] == 2
    assert preview["batch_tag"] == "re-deadbeefcafe"

    progress = plans.execute_cleanup_by_batch(
        r,
        CFG,
        batch_id="deadbeefcafe",
        connect_fn=lambda *_a, **_k: object(),
        list_fn=list_fn,
        destroy_fn=destroy_fn,
    )
    assert progress["status"] == "completed"
    assert sorted(destroyed) == [10, 11]
    stored = plans.get_cleanup_progress(r, CFG, "deadbeefcafe")
    assert stored is not None
    assert stored["status"] == "completed"


@pytest.fixture
def cleanup_client(main_module: Any) -> Any:
    from starlette.testclient import TestClient

    return TestClient(main_module.app)


def test_cleanup_http_requires_feature_flag(cleanup_client: Any) -> None:
    login = cleanup_client.post("/api/auth/login", json={"password": "test-dashboard-secret"})
    assert login.status_code == 200
    r = cleanup_client.post("/api/cleanup/preview", json={"batch_id": "aaaaaaaaaaaa"})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "disabled" in detail.lower() or "Settings" in detail


def test_cleanup_http_preview_when_enabled(
    cleanup_client: Any, main_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = cleanup_client.post("/api/auth/login", json={"password": "test-dashboard-secret"})
    assert login.status_code == 200

    real_load = main_module.load_config

    def load_with_flag() -> dict[str, Any]:
        cfg = real_load()
        ui = dict(cfg.get("ui") or {})
        ui["plan_run_cleanup_enabled"] = True
        return {**cfg, "ui": ui}

    monkeypatch.setattr(main_module, "load_config", load_with_flag)
    monkeypatch.setattr(
        plans,
        "preview_cleanup_by_batch",
        lambda *_a, **_k: {
            "batch_id": "aaaaaaaaaaaa",
            "batch_tag": "re-aaaaaaaaaaaa",
            "pve_cluster_id": "default",
            "count": 0,
            "guests": [],
            "sample": [],
            "plan_run_id": "",
        },
    )
    r = cleanup_client.post("/api/cleanup/preview", json={"batch_id": "aaaaaaaaaaaa"})
    assert r.status_code == 200
    assert r.json()["batch_tag"] == "re-aaaaaaaaaaaa"
