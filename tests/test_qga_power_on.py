"""Tests for post-restore power-on and QGA wait policy."""

from __future__ import annotations

from typing import Any

import pytest

import plans
from jobs import enqueue_restores
from pve_client import TaskCancelled, wait_for_qemu_agent
from tests.test_plans import CFG, FakeRedis


def test_enqueue_power_on_and_qga_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobs.connect_proxmox", lambda _cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda _px: set())
    r = FakeRedis()
    rows = [
        {
            "backup_id": "b1",
            "vmid": 10,
            "name": "vm10",
            "timestamp": "2026-01-01T00:00:00Z",
            "pve_storage": "pbs",
            "voltail": "vm/10/2026-01-01T00:00:00Z",
            "size_bytes": 1,
        }
    ]
    result = enqueue_restores(
        r,
        CFG,
        rows,
        node="pve1",
        target_storage="local-lvm",
        vmid_start=200,
        live_restore=False,
        bwlimit=0,
        power_on=True,
        qga_wait_sec=90,
    )
    assert result["power_on"] is True
    assert result["qga_wait_sec"] == 90
    job = r.hgetall(f"{CFG['redis']['job_key_prefix']}{result['job_ids'][0]}")
    assert job["power_on"] == "1"
    assert job["powered_off"] == "0"
    assert job["qga_wait_sec"] == "90"


def test_qga_implies_power_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobs.connect_proxmox", lambda _cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda _px: set())
    r = FakeRedis()
    rows = [
        {
            "backup_id": "b1",
            "vmid": 11,
            "name": "vm11",
            "timestamp": "2026-01-01T00:00:00Z",
            "pve_storage": "pbs",
            "voltail": "vm/11/2026-01-01T00:00:00Z",
            "size_bytes": 1,
        }
    ]
    result = enqueue_restores(
        r,
        CFG,
        rows,
        node="pve1",
        target_storage="local-lvm",
        vmid_start=210,
        live_restore=False,
        bwlimit=0,
        power_on=False,
        qga_wait_sec=60,
    )
    assert result["power_on"] is True
    job = r.hgetall(f"{CFG['redis']['job_key_prefix']}{result['job_ids'][0]}")
    assert job["power_on"] == "1"


def test_wait_for_qemu_agent_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_ping(_px: Any, _node: str, _vmid: int) -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    monkeypatch.setattr("pve_client.qemu_agent_ping", fake_ping)
    monkeypatch.setattr("pve_client.time.sleep", lambda _s: None)
    waited = wait_for_qemu_agent(object(), "pve1", 100, timeout_sec=30, poll_interval_sec=1)
    assert waited >= 0
    assert calls["n"] == 2


def test_wait_for_qemu_agent_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pve_client.qemu_agent_ping", lambda *_a, **_k: False)
    monkeypatch.setattr("pve_client.time.sleep", lambda _s: None)
    # Force immediate deadline by using tiny timeout and advancing monotonic.
    times = [100.0, 100.5, 102.0]

    def fake_mono() -> float:
        return times.pop(0) if times else 200.0

    monkeypatch.setattr("pve_client.time.monotonic", fake_mono)
    with pytest.raises(TimeoutError):
        wait_for_qemu_agent(object(), "pve1", 100, timeout_sec=1, poll_interval_sec=0.5)


def test_wait_for_qemu_agent_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pve_client.qemu_agent_ping", lambda *_a, **_k: False)
    monkeypatch.setattr("pve_client.time.sleep", lambda _s: None)
    with pytest.raises(TaskCancelled):
        wait_for_qemu_agent(
            object(),
            "pve1",
            100,
            timeout_sec=30,
            poll_interval_sec=1,
            should_cancel=lambda: True,
        )


def test_start_plan_run_power_on_overrides_drill_default() -> None:
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
        "power_on": False,
        "qga_wait_sec": 0,
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
        auto_teardown=True,
        power_on=True,
        qga_wait_sec=45,
    )
    assert run["drill"] is True
    assert run["power_on"] is True
    assert run["powered_off"] is False
    assert run["qga_wait_sec"] == 45
    assert captured.get("power_on") is True
    assert captured.get("qga_wait_sec") == 45


def test_drill_ignores_location_power_on_default() -> None:
    r = FakeRedis()
    plan = {
        "id": "plan-2",
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
        "power_on": True,
        "qga_wait_sec": 90,
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

    def fake_enqueue(_r: Any, _cfg: Any, rows: list, **kwargs: Any) -> dict[str, Any]:
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
        auto_teardown=True,
        power_on=False,
        qga_wait_sec=0,
    )
    assert run["drill"] is True
    assert run["power_on"] is False
    assert run["powered_off"] is True
    assert run["live_restore"] is False
    assert run["qga_wait_sec"] == 0
