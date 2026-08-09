"""DR restore mode: keep source VMID / fail if already on cluster."""

from __future__ import annotations

from typing import Any

import pytest

from jobs import enqueue_restores


CFG = {
    "redis": {
        "job_key_prefix": "test:job:",
        "job_log_suffix": ":log",
        "queue_key": "test:queue",
    }
}


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}

    def hset(self, key: str, mapping: dict[str, Any] | None = None, **kwargs: Any) -> None:
        data = dict(mapping or {})
        data.update({k: str(v) for k, v in kwargs.items()})
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in data.items()})

    def rpush(self, key: str, *values: str) -> None:
        self.lists.setdefault(key, []).extend(values)

    def scan_iter(self, match: str = "*", count: int = 200):  # noqa: ARG002
        return iter([])

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key) or {})


def _row(vmid: int) -> dict[str, Any]:
    return {
        "backup_id": f"main/ds/root|vm/{vmid}/2026-01-01T00:00:00Z",
        "name": f"vm-{vmid}",
        "vmid": vmid,
        "pve_storage": "pbs-main",
        "voltail": f"vm/{vmid}/2026-01-01T00:00:00Z",
        "size_bytes": 1000,
        "source_label": "main",
    }


def test_enqueue_dr_uses_source_vmid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda px: {200, 201})
    r = FakeRedis()
    result = enqueue_restores(
        r,  # type: ignore[arg-type]
        CFG,
        [_row(109), _row(110)],
        nodes=["pve"],
        target_storage="local-lvm",
        vmid_start=3600,
        live_restore=True,
        bwlimit=0,
        restore_mode="dr",
    )
    assert result["proxmox_vmids_assigned"] == [109, 110]
    assert result["restore_mode"] == "dr"
    jobs = list(r.hashes.values())
    assert {j["proxmox_vmid"] for j in jobs} == {"109", "110"}
    assert all(j["restore_mode"] == "dr" for j in jobs)
    assert all(j["unique"] == "0" for j in jobs)
    assert all(j["force"] == "0" for j in jobs)
    assert all(j["live_restore"] == "1" for j in jobs)


def test_enqueue_dr_fails_when_vmid_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda px: {109})
    r = FakeRedis()
    with pytest.raises(RuntimeError, match="VMID 109 already exists"):
        enqueue_restores(
            r,  # type: ignore[arg-type]
            CFG,
            [_row(109)],
            nodes=["pve"],
            target_storage="local-lvm",
            vmid_start=100,
            live_restore=False,
            bwlimit=0,
            restore_mode="dr",
        )


def test_enqueue_dr_overwrite_refuses_lxc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda px: {109})
    monkeypatch.setattr(
        "jobs.find_guest_resource",
        lambda px, vmid: {"type": "lxc", "node": "pve", "vmid": int(vmid)},
    )
    r = FakeRedis()
    with pytest.raises(RuntimeError, match="LXC container"):
        enqueue_restores(
            r,  # type: ignore[arg-type]
            CFG,
            [_row(109)],
            nodes=["pve"],
            target_storage="local-lvm",
            vmid_start=100,
            live_restore=False,
            bwlimit=0,
            restore_mode="dr",
            overwrite=True,
        )


def test_enqueue_dr_overwrite_refuses_foreign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda px: {109})
    monkeypatch.setattr(
        "jobs.find_guest_resource",
        lambda px, vmid: {"type": "qemu", "node": "pve", "vmid": int(vmid)},
    )
    monkeypatch.setattr("jobs.qemu_is_managed_by_tool", lambda px, node, vmid: False)
    r = FakeRedis()
    with pytest.raises(RuntimeError, match="not provisioned by restore-engine"):
        enqueue_restores(
            r,  # type: ignore[arg-type]
            CFG,
            [_row(109)],
            nodes=["pve"],
            target_storage="local-lvm",
            vmid_start=100,
            live_restore=False,
            bwlimit=0,
            restore_mode="dr",
            overwrite=True,
        )


def test_enqueue_dr_rejects_duplicate_source_vmids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda px: set())
    r = FakeRedis()
    with pytest.raises(RuntimeError, match="duplicate source VMID"):
        enqueue_restores(
            r,  # type: ignore[arg-type]
            CFG,
            [_row(109), _row(109)],
            nodes=["pve"],
            target_storage="local-lvm",
            vmid_start=100,
            live_restore=False,
            bwlimit=0,
            restore_mode="dr",
        )
