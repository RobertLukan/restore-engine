"""Tests for recovery-plan readiness checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import plans
from states import PlanVerification
from tests.test_plans import CFG, FakeRedis


def _seed_plan(r: FakeRedis, *, restore_mode: str = "normal", vmids: list[int] | None = None) -> dict[str, Any]:
    group = plans.create_group(
        r,
        CFG,
        {"name": "g1", "tags": [], "vmids": vmids or [109]},
    )
    loc = plans.create_location(
        r,
        CFG,
        {
            "name": "lab",
            "node": "pve",
            "nodes": ["pve"],
            "storage": "local-lvm",
            "storage_by_node": {"pve": "local-lvm"},
            "vmid_start": 3500,
            "restore_mode": restore_mode,
        },
    )
    plan = plans.create_plan(
        r,
        CFG,
        {"name": "P1", "group_ids": [group["id"]], "location_id": loc["id"], "enabled": True},
    )
    return plan


def _backup(vmid: int = 109, *, size_bytes: int = 1000) -> dict[str, Any]:
    return {
        "backup_id": f"main|vm/{vmid}/2026-08-07T10:00:00Z",
        "vmid": vmid,
        "name": f"vm-{vmid}",
        "timestamp": "2026-08-07T10:00:00Z",
        "pve_storage": "pbs",
        "voltail": f"vm/{vmid}/2026-08-07T10:00:00Z",
        "source_id": "main",
        "source_label": "main",
        "size_bytes": size_bytes,
    }


def _estimate_fn(nonzero: int = 400) -> Any:
    def _fn(backup_id: str) -> dict[str, Any]:
        return {"backup_id": backup_id, "nonzero_bytes": nonzero}

    return _fn


def test_readiness_fails_empty_group() -> None:
    r = FakeRedis()
    plan = _seed_plan(r, vmids=[999])  # no backup for 999

    updated, check = plans.run_plan_readiness(
        r,
        CFG,
        plan,
        list_backups_fn=lambda _c: [_backup(109)],
        probe_pbs_fn=lambda _c: (True, [{"ok": True, "label": "main", "detail": "ok"}]),
        test_pve_fn=lambda _c: (True, "PVE ok"),
        connect_pve_fn=lambda _c: object(),
        list_nodes_fn=lambda _px: [{"node": "pve"}],
        list_storages_fn=lambda _px, _n: [
            {"id": "local-lvm", "usable_for_vm_disks": True, "enabled": True}
        ],
        vmids_in_use_fn=lambda _px: set(),
        resolve_tags_fn=lambda *_a, **_k: ({}, {}),
        estimate_size_fn=_estimate_fn(),
        persist=True,
    )
    assert check["ok"] is False
    assert updated["verification"] == PlanVerification.NOT_VERIFIED.value
    assert updated["last_check_at"]
    assert any(i["code"] == "group.empty" for i in check["items"])


def test_readiness_dr_fails_when_vmid_in_use() -> None:
    r = FakeRedis()
    plan = _seed_plan(r, restore_mode="dr", vmids=[109])

    updated, check = plans.run_plan_readiness(
        r,
        CFG,
        plan,
        list_backups_fn=lambda _c: [_backup(109)],
        probe_pbs_fn=lambda _c: (True, [{"ok": True, "label": "main", "detail": "ok"}]),
        test_pve_fn=lambda _c: (True, "PVE ok"),
        connect_pve_fn=lambda _c: object(),
        list_nodes_fn=lambda _px: [{"node": "pve"}],
        list_storages_fn=lambda _px, _n: [
            {"id": "local-lvm", "usable_for_vm_disks": True, "enabled": True}
        ],
        vmids_in_use_fn=lambda _px: {109},
        resolve_tags_fn=lambda *_a, **_k: ({}, {}),
        estimate_size_fn=_estimate_fn(),
        persist=True,
    )
    assert check["ok"] is False
    assert updated["verification"] == PlanVerification.NOT_VERIFIED.value
    assert any(i["code"] == "vmid.dr_in_use" for i in check["items"])


def test_readiness_happy_path_sets_verified() -> None:
    r = FakeRedis()
    plan = _seed_plan(r, restore_mode="normal", vmids=[109])

    updated, check = plans.run_plan_readiness(
        r,
        CFG,
        plan,
        list_backups_fn=lambda _c: [_backup(109)],
        probe_pbs_fn=lambda _c: (True, [{"ok": True, "label": "main", "detail": "ok"}]),
        test_pve_fn=lambda _c: (True, "PVE ok"),
        connect_pve_fn=lambda _c: object(),
        list_nodes_fn=lambda _px: [{"node": "pve"}],
        list_storages_fn=lambda _px, _n: [
            {"id": "local-lvm", "usable_for_vm_disks": True, "enabled": True}
        ],
        vmids_in_use_fn=lambda _px: {100, 101},
        resolve_tags_fn=lambda *_a, **_k: ({}, {}),
        estimate_size_fn=_estimate_fn(400),
        persist=True,
    )
    assert check["ok"] is True
    assert updated["verification"] == PlanVerification.VERIFIED.value
    assert updated["last_check_at"] == check["checked_at"]
    assert updated["last_check"]["member_count"] == 1
    assert check["size_summary"]["gross_bytes"] == 1000
    assert check["size_summary"]["nonzero_bytes"] == 400
    assert any(i["code"] == "plan.sizes" for i in check["items"])
    assert "gross" in check["summary"]
    assert "approx net" in check["summary"]
    stored = plans.get_plan(r, CFG, plan["id"])
    assert stored["verification"] == "VERIFIED"


def test_readiness_size_summary_sums_unique_backups() -> None:
    r = FakeRedis()
    plan = _seed_plan(r, restore_mode="normal", vmids=[109, 110])
    backups = [
        _backup(109, size_bytes=10_000),
        _backup(110, size_bytes=30_000),
    ]

    def est(bid: str) -> dict[str, Any]:
        return {"backup_id": bid, "nonzero_bytes": 1_000 if "109" in bid else 5_000}

    _, check = plans.run_plan_readiness(
        r,
        CFG,
        plan,
        list_backups_fn=lambda _c: backups,
        probe_pbs_fn=lambda _c: (True, [{"ok": True, "label": "main", "detail": "ok"}]),
        test_pve_fn=lambda _c: (True, "PVE ok"),
        connect_pve_fn=lambda _c: object(),
        list_nodes_fn=lambda _px: [{"node": "pve"}],
        list_storages_fn=lambda _px, _n: [
            {"id": "local-lvm", "usable_for_vm_disks": True, "enabled": True}
        ],
        vmids_in_use_fn=lambda _px: set(),
        resolve_tags_fn=lambda *_a, **_k: ({}, {}),
        estimate_size_fn=est,
        persist=False,
    )
    assert check["ok"] is True
    assert check["member_count"] == 2
    assert check["size_summary"]["gross_bytes"] == 40_000
    assert check["size_summary"]["nonzero_bytes"] == 6_000
    assert check["size_summary"]["nonzero_estimated_count"] == 2


def test_summarize_member_sizes_dedupes_and_tolerates_estimate_errors() -> None:
    r = FakeRedis()
    rows = [
        [_backup(109, size_bytes=100), _backup(109, size_bytes=100)],
        [_backup(110, size_bytes=50)],
    ]

    def est(bid: str) -> dict[str, Any]:
        if "110" in bid:
            raise RuntimeError("boom")
        return {"nonzero_bytes": 40}

    summary = plans.summarize_member_sizes(r, CFG, rows, estimate_size_fn=est)
    assert summary["backup_count"] == 2
    assert summary["gross_bytes"] == 150
    assert summary["nonzero_bytes"] == 40
    assert summary["nonzero_estimated_count"] == 1
    assert summary["nonzero_missing_count"] == 1


def test_plans_due_for_check_selection() -> None:
    now = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)
    plans_list = [
        {"id": "a", "enabled": True, "last_check_at": ""},
        {
            "id": "b",
            "enabled": True,
            "last_check_at": (now - timedelta(hours=25)).isoformat(),
        },
        {
            "id": "c",
            "enabled": True,
            "last_check_at": (now - timedelta(hours=1)).isoformat(),
        },
        {"id": "d", "enabled": False, "last_check_at": ""},
    ]
    due = plans.plans_due_for_check(plans_list, interval_sec=86400, now=now)
    assert [p["id"] for p in due] == ["a", "b"]
