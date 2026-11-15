"""RE-03: pending jobs reserve VMIDs; normal restores do not force overwrite."""

from __future__ import annotations

import pytest

from jobs import enqueue_restores, reserved_vmids_from_jobs
from states import RestoreState
from tests.test_plans import FakeRedis


def _cfg() -> dict:
    return {
        "redis": {
            "job_key_prefix": "restore:job:",
            "job_log_suffix": ":log",
            "queue_key": "restore:queue",
        }
    }


def _row(vmid: int) -> dict:
    return {
        "backup_id": f"vm/{vmid}/2026-01-01T00:00:00Z",
        "vmid": vmid,
        "name": f"vm{vmid}",
        "pve_storage": "pbs",
        "voltail": f"vm/{vmid}/2026-01-01T00:00:00Z",
        "source_label": "",
        "size_bytes": 1000,
    }


def test_successive_pending_batches_get_distinct_vmids(monkeypatch: pytest.MonkeyPatch) -> None:
    r = FakeRedis()
    cfg = _cfg()
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg, cluster_id=None: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda prox: set())

    first = enqueue_restores(
        r,
        cfg,
        [_row(10)],
        node="pve",
        target_storage="local-lvm",
        vmid_start=5000,
        live_restore=False,
        bwlimit=0,
    )
    second = enqueue_restores(
        r,
        cfg,
        [_row(11)],
        node="pve",
        target_storage="local-lvm",
        vmid_start=5000,
        live_restore=False,
        bwlimit=0,
    )
    assert first["proxmox_vmids_assigned"] == [5000]
    assert second["proxmox_vmids_assigned"] == [5001]
    assert reserved_vmids_from_jobs(r, cfg, cluster_id="default") == {5000, 5001}

    job0 = r.hgetall("restore:job:" + first["job_ids"][0])
    assert job0["force"] == "0"
    assert job0["state"] == RestoreState.PENDING.value
