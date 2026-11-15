"""HA resource helpers and plan add_to_ha plumbing."""

from __future__ import annotations

from typing import Any

import pytest

import plans
from jobs import enqueue_restores
from pve_client import (
    add_qemu_to_ha,
    ha_resource_sid,
    list_ha_resource_sids,
    qemu_in_ha,
    remove_qemu_from_ha,
)
from states import RestoreState
from tests.test_plans import CFG, FakeRedis
import worker


class _FakeHaResources:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.posted: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def get(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def post(self, **params: Any) -> None:
        self.posted.append(dict(params))
        sid = str(params.get("sid") or "")
        self.rows.append({"sid": sid, "type": params.get("type", "vm")})

    def __call__(self, sid: str) -> Any:
        parent = self

        class _One:
            def delete(self) -> None:
                parent.deleted.append(str(sid))
                parent.rows = [r for r in parent.rows if str(r.get("sid")) not in {str(sid), f"vm:{sid}"}]

        return _One()


class _FakeProxmox:
    def __init__(self) -> None:
        self.cluster = type("C", (), {})()
        self.cluster.ha = type("H", (), {})()
        self.cluster.ha.resources = _FakeHaResources()


def test_ha_resource_sid() -> None:
    assert ha_resource_sid(100) == "vm:100"


def test_add_qemu_to_ha_and_idempotent() -> None:
    px = _FakeProxmox()
    assert add_qemu_to_ha(px, 200, state="stopped") == "added"
    assert px.cluster.ha.resources.posted[0]["sid"] == "vm:200"
    assert px.cluster.ha.resources.posted[0]["state"] == "stopped"
    assert qemu_in_ha(px, 200) is True
    assert add_qemu_to_ha(px, 200, state="started") == "already"
    assert len(px.cluster.ha.resources.posted) == 1


def test_add_qemu_to_ha_with_group() -> None:
    px = _FakeProxmox()
    add_qemu_to_ha(px, 10, state="started", group="priority")
    assert px.cluster.ha.resources.posted[0]["group"] == "priority"


def test_remove_qemu_from_ha() -> None:
    px = _FakeProxmox()
    add_qemu_to_ha(px, 55, state="started")
    assert remove_qemu_from_ha(px, 55) == "removed"
    assert remove_qemu_from_ha(px, 55) == "absent"
    assert list_ha_resource_sids(px) == set()


def test_normalize_plan_add_to_ha() -> None:
    data = plans.normalize_plan(
        {
            "name": "DR",
            "group_ids": ["g1"],
            "location_id": "loc1",
            "add_to_ha": True,
            "ha_group": " ha-a ",
        }
    )
    assert data["add_to_ha"] is True
    assert data["ha_group"] == "ha-a"
    data2 = plans.normalize_plan(
        {"name": "DR", "group_ids": ["g1"], "location_id": "loc1"}
    )
    assert data2["add_to_ha"] is False
    assert data2["ha_group"] == ""


def test_restore_selected_request_keeps_ha_fields() -> None:
    """RE-06: ad-hoc restore-selected must not silently drop HA options."""
    from main import RestoreSelectedRequest

    body = RestoreSelectedRequest(
        backup_ids=["main|vm/1/2026-01-01T00:00:00Z"],
        add_to_ha=True,
        ha_group="ha1",
    )
    dumped = body.model_dump()
    assert dumped["add_to_ha"] is True
    assert dumped["ha_group"] == "ha1"


def test_enqueue_stores_add_to_ha(monkeypatch: pytest.MonkeyPatch) -> None:
    r = FakeRedis()
    monkeypatch.setattr(
        "jobs.connect_proxmox",
        lambda cfg, cluster_id=None: object(),
    )
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda _px: set())
    monkeypatch.setattr(
        "jobs.assign_nodes_least_loaded",
        lambda nodes, n, active_counts=None: [nodes[0]] * n,
    )
    rows = [
        {
            "backup_id": "main|vm/1/2026-01-01T00:00:00Z",
            "vmid": 1,
            "name": "vm1",
            "timestamp": "2026-01-01T00:00:00Z",
            "pve_storage": "pbs-main",
            "voltail": "vm/1/2026-01-01T00:00:00Z",
            "size_bytes": 100,
        }
    ]
    result = enqueue_restores(
        r,
        CFG,
        rows,
        nodes=["pve1"],
        target_storage="local-lvm",
        vmid_start=500,
        live_restore=False,
        bwlimit=0,
        add_to_ha=True,
        ha_group="grp1",
    )
    job_id = result["job_ids"][0]
    data = r.hgetall(f"{CFG['redis']['job_key_prefix']}{job_id}")
    assert data["add_to_ha"] == "1"
    assert data["ha_group"] == "grp1"


def test_process_job_adds_to_ha_when_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_worker_process import _patch_happy_path, _seed_job

    r = FakeRedis()
    job_id = "job-ha"
    _seed_job(r, job_id, add_to_ha="1", ha_group="pri", power_on="0")
    calls = _patch_happy_path(monkeypatch)
    ha_calls: list[dict[str, Any]] = []

    def fake_ha(_px: Any, vmid: int, *, state: str = "started", group: str = "") -> str:
        ha_calls.append({"vmid": vmid, "state": state, "group": group})
        return "added"

    monkeypatch.setattr(worker, "add_qemu_to_ha", fake_ha)
    worker.process_job(r, CFG, job_id)
    data = r.hgetall(f"{CFG['redis']['job_key_prefix']}{job_id}")
    assert data["state"] == RestoreState.COMPLETED.value
    assert data["ha_ok"] == "1"
    assert ha_calls == [{"vmid": 3500, "state": "stopped", "group": "pri"}]
    assert calls["stop"] == 1
