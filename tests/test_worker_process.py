"""Unit tests for worker.process_job (mocked PVE/PBS wire)."""

from __future__ import annotations

from typing import Any

import pytest

import worker
from states import RestoreState
from tests.test_plans import CFG, FakeRedis


def _seed_job(r: FakeRedis, job_id: str, **extra: str) -> None:
    prefix = CFG["redis"]["job_key_prefix"]
    mapping = {
        "job_id": job_id,
        "state": RestoreState.PENDING.value,
        "proxmox_node": "pve",
        "proxmox_vmid": "3500",
        "backup_id": "main|vm/109/2026-08-07T10:00:00Z",
        "proxmox_storage": "local-lvm",
        "archive": "pbs-main:backup/vm/109/2026-08-07T10:00:00Z",
        "power_on": "0",
        "network_mode": "none",
        "live_restore": "0",
        "bwlimit": "0",
        "unique": "1",
        "force": "1",
        "restore_mode": "normal",
        "backup_size_bytes": "0",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }
    mapping.update(extra)
    r.hset(f"{prefix}{job_id}", mapping=mapping)


def _patch_happy_path(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"submit": 0, "wait": 0, "stamp": 0, "stop": 0}

    monkeypatch.setattr(worker, "estimate_wire_compression_for_job", lambda *a, **k: None)
    monkeypatch.setattr(worker, "connect_proxmox", lambda cfg: object())

    def submit(*_a: Any, **_k: Any) -> str:
        calls["submit"] += 1
        return "UPID:pve:0001:restore"

    def wait(*_a: Any, **_k: Any) -> None:
        calls["wait"] += 1

    def stamp(*_a: Any, **_k: Any) -> None:
        calls["stamp"] += 1

    def stop(*_a: Any, **_k: Any) -> None:
        calls["stop"] += 1

    monkeypatch.setattr(worker, "submit_restore", submit)
    monkeypatch.setattr(worker, "wait_for_task", wait)
    monkeypatch.setattr(worker, "mark_qemu_managed_by_tool", stamp)
    monkeypatch.setattr(worker, "stop_qemu_vm", stop)
    return calls


def test_process_job_happy_path_powered_off(monkeypatch: pytest.MonkeyPatch) -> None:
    r = FakeRedis()
    job_id = "job-ok"
    _seed_job(r, job_id)
    calls = _patch_happy_path(monkeypatch)

    worker.process_job(r, CFG, job_id)

    data = r.hgetall(f"{CFG['redis']['job_key_prefix']}{job_id}")
    assert data["state"] == RestoreState.COMPLETED.value
    assert data["progress"] == "100"
    assert data.get("pve_upid") == "UPID:pve:0001:restore"
    assert data.get("managed_marked") == "1"
    assert calls["submit"] == 1
    assert calls["wait"] == 1
    assert calls["stamp"] == 1
    assert calls["stop"] == 1


def test_process_job_cancel_before_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    r = FakeRedis()
    job_id = "job-cancel"
    _seed_job(r, job_id, cancel_requested="1")
    calls = _patch_happy_path(monkeypatch)

    worker.process_job(r, CFG, job_id)

    data = r.hgetall(f"{CFG['redis']['job_key_prefix']}{job_id}")
    assert data["state"] == RestoreState.CANCELLED.value
    assert calls["submit"] == 0


def test_process_job_submit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    r = FakeRedis()
    job_id = "job-fail"
    _seed_job(r, job_id)
    monkeypatch.setattr(worker, "estimate_wire_compression_for_job", lambda *a, **k: None)
    monkeypatch.setattr(worker, "connect_proxmox", lambda cfg: object())
    monkeypatch.setattr(
        worker,
        "submit_restore",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PVE refused restore")),
    )

    with pytest.raises(RuntimeError, match="PVE refused restore"):
        worker.process_job(r, CFG, job_id)

    data = r.hgetall(f"{CFG['redis']['job_key_prefix']}{job_id}")
    assert data["state"] == RestoreState.RESTORING.value
