"""Post-B4: network isolation, DR overwrite, schedule due helpers, concurrency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from concurrency import reconcile_slots, release_slot, try_acquire_slot
from jobs import enqueue_restores
from plans import normalize_location, plans_due_for_schedule
from pve_client import apply_network_isolation


CFG = {
    "redis": {
        "job_key_prefix": "test:job:",
        "job_log_suffix": ":log",
        "queue_key": "test:queue",
        "concurrency_slots_key": "test:slots",
    },
    "worker": {"max_concurrent_restores": 2},
}


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.kv: dict[str, str] = {}

    def hset(self, key: str, mapping: dict[str, Any] | None = None, **kwargs: Any) -> None:
        data = dict(mapping or {})
        data.update({k: str(v) for k, v in kwargs.items()})
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in data.items()})

    def rpush(self, key: str, *values: str) -> None:
        self.lists.setdefault(key, []).extend(values)

    def scan_iter(self, match: str = "*", count: int = 200):  # noqa: ARG002
        # Minimal glob: prefix* for job keys.
        prefix = match[:-1] if match.endswith("*") else match
        for key in list(self.hashes.keys()) + list(self.kv.keys()) + list(self.lists.keys()):
            if key.startswith(prefix):
                yield key

    def hget(self, key: str, field: str) -> str | None:
        return (self.hashes.get(key) or {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key) or {})

    def get(self, key: str) -> str | None:
        return self.kv.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:  # noqa: ARG002
        self.kv[key] = str(value)

    def incr(self, key: str) -> int:
        n = int(self.kv.get(key) or 0) + 1
        self.kv[key] = str(n)
        return n

    def decr(self, key: str) -> int:
        n = int(self.kv.get(key) or 0) - 1
        self.kv[key] = str(n)
        return n


def _row(vmid: int = 109) -> dict[str, Any]:
    return {
        "backup_id": f"main/ds/root|vm/{vmid}/2026-01-01T00:00:00Z",
        "name": f"vm-{vmid}",
        "vmid": vmid,
        "pve_storage": "pbs-main",
        "voltail": f"vm/{vmid}/2026-01-01T00:00:00Z",
        "size_bytes": 1000,
        "source_label": "main",
    }


def test_location_remap_requires_bridge() -> None:
    with pytest.raises(ValueError, match="lab_bridge"):
        normalize_location(
            {
                "name": "lab",
                "node": "pve",
                "storage": "local-lvm",
                "network_mode": "remap",
            }
        )


def test_location_unlink_marks_isolated() -> None:
    loc = normalize_location(
        {"name": "lab", "node": "pve", "storage": "local-lvm", "network_mode": "unlink"}
    )
    assert loc["network_mode"] == "unlink"
    assert loc["isolated"] is True


def test_enqueue_stores_network_and_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda px: set())
    r = FakeRedis()
    result = enqueue_restores(
        r,  # type: ignore[arg-type]
        CFG,
        [_row(50)],
        nodes=["pve"],
        target_storage="local-lvm",
        vmid_start=300,
        live_restore=False,
        bwlimit=0,
        network_mode="unlink",
        http_check_url="http://10.0.0.1/health",
    )
    job = list(r.hashes.values())[0]
    assert job["network_mode"] == "unlink"
    assert job["http_check_url"] == "http://10.0.0.1/health"
    assert result["job_ids"]


def test_enqueue_dr_overwrite_destroys(monkeypatch: pytest.MonkeyPatch) -> None:
    destroyed: list[tuple[str, int]] = []

    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda px: {109})
    monkeypatch.setattr("jobs.find_qemu_node", lambda px, vmid: "pve1")
    monkeypatch.setattr(
        "jobs.find_guest_resource",
        lambda px, vmid: {"type": "qemu", "node": "pve1", "vmid": int(vmid)},
    )
    monkeypatch.setattr("jobs.qemu_is_managed_by_tool", lambda px, node, vmid: True)
    monkeypatch.setattr(
        "jobs.destroy_owned_qemu_vm",
        lambda px, node, vmid, **_kw: destroyed.append((node, int(vmid))),
    )
    r = FakeRedis()
    result = enqueue_restores(
        r,  # type: ignore[arg-type]
        CFG,
        [_row(109)],
        nodes=["pve1"],
        target_storage="local-lvm",
        vmid_start=100,
        live_restore=False,
        bwlimit=0,
        restore_mode="dr",
        overwrite=True,
    )
    assert destroyed == [("pve1", 109)]
    assert result["proxmox_vmids_assigned"] == [109]
    job = list(r.hashes.values())[0]
    assert job["overwrite"] == "1"
    assert job["force"] == "1"


def test_enqueue_dr_overwrite_refuses_foreign_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda px: {109})
    monkeypatch.setattr(
        "jobs.find_guest_resource",
        lambda px, vmid: {"type": "qemu", "node": "pve1", "vmid": int(vmid)},
    )
    monkeypatch.setattr("jobs.qemu_is_managed_by_tool", lambda px, node, vmid: False)
    r = FakeRedis()
    with pytest.raises(RuntimeError, match="not provisioned by restore-engine"):
        enqueue_restores(
            r,  # type: ignore[arg-type]
            CFG,
            [_row(109)],
            nodes=["pve1"],
            target_storage="local-lvm",
            vmid_start=100,
            live_restore=False,
            bwlimit=0,
            restore_mode="dr",
            overwrite=True,
        )


def test_apply_network_isolation_unlink(monkeypatch: pytest.MonkeyPatch) -> None:
    puts: dict[str, Any] = {}

    class _CfgEndpoint:
        def get(self) -> dict[str, str]:
            return {"net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0"}

        def put(self, **kwargs: Any) -> None:
            puts.update(kwargs)

    class _Qemu:
        def __init__(self, vmid: int) -> None:
            self.config = _CfgEndpoint()

    class _Node:
        def qemu(self, vmid: int) -> _Qemu:
            return _Qemu(vmid)

    class _Px:
        def nodes(self, node: str) -> _Node:  # noqa: ARG002
            return _Node()

    changed = apply_network_isolation(_Px(), "pve", 100, mode="unlink")  # type: ignore[arg-type]
    assert changed == ["net0"]
    assert "link_down=1" in puts["net0"]


def test_plans_due_for_schedule() -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    plans = [
        {
            "id": "a",
            "enabled": True,
            "schedule_enabled": True,
            "schedule_interval_hours": 24,
            "last_scheduled_run_at": (now - timedelta(hours=25)).isoformat(),
        },
        {
            "id": "b",
            "enabled": True,
            "schedule_enabled": True,
            "schedule_interval_hours": 24,
            "last_scheduled_run_at": (now - timedelta(hours=1)).isoformat(),
        },
        {"id": "c", "enabled": True, "schedule_enabled": False, "schedule_interval_hours": 1},
    ]
    due = plans_due_for_schedule(plans, now=now)
    assert [p["id"] for p in due] == ["a"]


def test_concurrency_slots() -> None:
    r = FakeRedis()
    assert try_acquire_slot(r, CFG, limit=2) is True  # type: ignore[arg-type]
    assert try_acquire_slot(r, CFG, limit=2) is True  # type: ignore[arg-type]
    assert try_acquire_slot(r, CFG, limit=2) is False  # type: ignore[arg-type]
    release_slot(r, CFG)  # type: ignore[arg-type]
    assert try_acquire_slot(r, CFG, limit=2) is True  # type: ignore[arg-type]


def test_reconcile_slots_clears_leaks() -> None:
    r = FakeRedis()
    r.set("test:slots", "2")
    r.hset("test:job:a", mapping={"state": "PENDING"})
    r.hset("test:job:b", mapping={"state": "RESTORING"})
    assert reconcile_slots(r, CFG) == 1  # type: ignore[arg-type]
    assert r.get("test:slots") == "1"
