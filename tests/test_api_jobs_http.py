"""HTTP tests for restore-selected, job get/log/stats, and stop."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from states import RestoreState
from tests.test_plans import FakeRedis


def client(main_module: Any) -> TestClient:
    return TestClient(main_module.app)


def _login(c: TestClient) -> None:
    assert c.post("/api/auth/login", json={"password": "test-dashboard-secret"}).status_code == 200


def test_restore_selected_get_stats_stop(
    main_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = FakeRedis()
    monkeypatch.setattr(main_module, "redis_client", lambda: r)
    c = client(main_module)
    _login(c)

    backup = {
        "backup_id": "main|vm/109/2026-08-07T10:00:00Z",
        "vmid": 109,
        "name": "vm-109",
        "timestamp": "2026-08-07T10:00:00Z",
        "pve_storage": "pbs-main",
        "voltail": "vm/109/2026-08-07T10:00:00Z",
        "source_id": "main",
        "size_bytes": 1000,
    }
    monkeypatch.setattr(main_module, "list_vm_backups", lambda _cfg: [backup])

    def fake_enqueue(r_in: Any, cfg: Any, rows: list, **_k: Any) -> dict[str, Any]:
        job_id = "http-job-1"
        prefix = cfg["redis"]["job_key_prefix"]
        r_in.hset(
            f"{prefix}{job_id}",
            mapping={
                "job_id": job_id,
                "state": RestoreState.PENDING.value,
                "backup_id": rows[0]["backup_id"],
                "vm_name": rows[0]["name"],
                "source_vmid": str(rows[0]["vmid"]),
                "proxmox_vmid": "3500",
                "proxmox_node": "pve",
                "proxmox_storage": "local-lvm",
                "live_restore": "0",
                "bwlimit": "0",
                "restore_mode": "normal",
                "power_on": "0",
                "progress": "0",
                "created_at": "2026-08-26T00:00:00+00:00",
                "updated_at": "2026-08-26T00:00:00+00:00",
            },
        )
        r_in.rpush(cfg["redis"]["queue_key"], job_id)
        r_in.rpush(
            f"{prefix}{job_id}{cfg['redis']['job_log_suffix']}",
            '{"ts":"t","level":"INFO","stage":"PENDING","message":"queued"}',
        )
        return {
            "enqueued": 1,
            "job_ids": [job_id],
            "proxmox_vmids_assigned": [3500],
            "proxmox_nodes_assigned": ["pve"],
            "proxmox_storages_assigned": ["local-lvm"],
            "load_balance_nodes": ["pve"],
            "storage_by_node": {"pve": "local-lvm"},
            "restore_mode": "normal",
            "power_on": False,
            "qga_wait_sec": 0,
        }

    monkeypatch.setattr(main_module, "_enqueue_restores", fake_enqueue)

    enq = c.post(
        "/api/jobs/restore-selected",
        json={
            "backup_ids": [backup["backup_id"]],
            "proxmox_node": "pve",
            "proxmox_storage": "local-lvm",
            "proxmox_vmid_start": 3500,
        },
    )
    assert enq.status_code == 200, enq.text
    assert enq.json()["enqueued"] == 1
    job_id = enq.json()["job_ids"][0]

    got = c.get(f"/api/jobs/{job_id}")
    assert got.status_code == 200
    assert got.json()["state"] == RestoreState.PENDING.value

    log = c.get(f"/api/jobs/{job_id}/log")
    assert log.status_code == 200
    assert len(log.json()) >= 1

    stats = c.get("/api/jobs/stats")
    assert stats.status_code == 200
    body = stats.json()
    assert body["pending"] >= 1
    assert "max_concurrent" in body

    stop = c.post(f"/api/jobs/{job_id}/stop")
    assert stop.status_code == 200, stop.text
    assert stop.json()["state"] == RestoreState.CANCELLED.value
    assert r.hgetall(f"{main_module.load_config()['redis']['job_key_prefix']}{job_id}")[
        "cancel_requested"
    ] == "1"
