"""Largest-first enqueue ordering for restore makespan."""

from __future__ import annotations

import pytest

from jobs import (
    enqueue_largest_first_enabled,
    enqueue_restores,
    sort_rows_largest_first,
)
from tests.test_plans import FakeRedis


def test_sort_rows_largest_first_prefers_nonzero_then_size() -> None:
    rows = [
        {"vmid": 10, "name": "small", "size_bytes": 100, "nonzero_bytes": 50},
        {"vmid": 20, "name": "huge-gross", "size_bytes": 9_000, "nonzero_bytes": 10},
        {"vmid": 30, "name": "big-nonzero", "size_bytes": 500, "nonzero_bytes": 400},
        {"vmid": 40, "name": "medium", "size_bytes": 200},
    ]
    ordered = sort_rows_largest_first(rows)
    assert [r["name"] for r in ordered] == [
        "big-nonzero",  # nonzero 400
        "medium",  # size 200 (no nonzero)
        "small",  # nonzero 50
        "huge-gross",  # nonzero 10 wins over large gross
    ]


def test_sort_rows_largest_first_tie_break_higher_vmid() -> None:
    rows = [
        {"vmid": 11, "size_bytes": 100},
        {"vmid": 22, "size_bytes": 100},
        {"vmid": 5, "size_bytes": 100},
    ]
    ordered = sort_rows_largest_first(rows)
    assert [r["vmid"] for r in ordered] == [22, 11, 5]


def test_sort_rows_does_not_mutate_input() -> None:
    rows = [{"vmid": 1, "size_bytes": 1}, {"vmid": 2, "size_bytes": 9}]
    original = list(rows)
    sort_rows_largest_first(rows)
    assert rows == original


def test_enqueue_largest_first_enabled_defaults_true() -> None:
    assert enqueue_largest_first_enabled({}) is True
    assert enqueue_largest_first_enabled({"worker": {}}) is True
    assert enqueue_largest_first_enabled({"worker": {"enqueue_largest_first": False}}) is False
    assert enqueue_largest_first_enabled({"worker": {"enqueue_largest_first": True}}) is True


def _base_cfg(**worker: object) -> dict:
    cfg: dict = {
        "redis": {
            "job_key_prefix": "restore:job:",
            "job_log_suffix": ":log",
            "queue_key": "restore:queue",
        }
    }
    if worker:
        cfg["worker"] = dict(worker)
    return cfg


def _row(vmid: int, size: int, *, nonzero: int | None = None) -> dict:
    row = {
        "backup_id": f"vm/{vmid}/2026-01-01T00:00:00Z",
        "vmid": vmid,
        "name": f"vm{vmid}",
        "pve_storage": "pbs",
        "voltail": f"vm/{vmid}/2026-01-01T00:00:00Z",
        "source_label": "",
        "size_bytes": size,
    }
    if nonzero is not None:
        row["nonzero_bytes"] = nonzero
    return row


def test_enqueue_rpush_order_largest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    r = FakeRedis()
    cfg = _base_cfg()
    rows = [
        _row(101, 100),
        _row(102, 900),
        _row(103, 400),
    ]
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg, cluster_id=None: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda prox: set())

    result = enqueue_restores(
        r,
        cfg,
        rows,
        nodes=["pve1"],
        target_storage="local-lvm",
        vmid_start=5000,
        live_restore=False,
        bwlimit=0,
    )
    assert result["enqueued"] == 3
    queue = r.lrange("restore:queue", 0, -1)
    names = []
    for job_id in queue:
        data = r.hgetall(f"restore:job:{job_id}")
        names.append(data.get("vm_name"))
    assert names == ["vm102", "vm103", "vm101"]
    # Normal VMIDs follow size order (largest first).
    assert result["proxmox_vmids_assigned"] == [5000, 5001, 5002]
    vmids_by_name = {}
    for job_id in queue:
        data = r.hgetall(f"restore:job:{job_id}")
        vmids_by_name[data["vm_name"]] = int(data["proxmox_vmid"])
    assert vmids_by_name == {"vm102": 5000, "vm103": 5001, "vm101": 5002}


def test_enqueue_preserves_call_order_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    r = FakeRedis()
    cfg = _base_cfg(enqueue_largest_first=False)
    rows = [
        _row(101, 100),
        _row(102, 900),
        _row(103, 400),
    ]
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg, cluster_id=None: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda prox: set())

    result = enqueue_restores(
        r,
        cfg,
        rows,
        nodes=["pve1"],
        target_storage="local-lvm",
        vmid_start=5000,
        live_restore=False,
        bwlimit=0,
    )
    queue = r.lrange("restore:queue", 0, -1)
    names = [r.hgetall(f"restore:job:{jid}").get("vm_name") for jid in queue]
    assert names == ["vm101", "vm102", "vm103"]
    assert result["proxmox_vmids_assigned"] == [5000, 5001, 5002]
