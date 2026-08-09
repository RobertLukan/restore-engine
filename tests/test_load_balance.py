"""Cluster VMID listing and least-loaded node assignment."""

from __future__ import annotations

from typing import Any

import pytest

from jobs import active_restore_counts_by_node, enqueue_restores
from pve_client import assign_nodes_least_loaded, qemu_vmids_in_use_cluster
from states import RestoreState


class _FakeResources:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def get(self, type: str = "vm") -> list[dict[str, Any]]:  # noqa: A002
        assert type == "vm"
        return self._rows


class _FakeCluster:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.resources = _FakeResources(rows)


class _FakeProxmox:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cluster = _FakeCluster(rows)


def test_qemu_vmids_in_use_cluster_includes_lxc() -> None:
    """QEMU and LXC share the Proxmox VMID namespace — both must count as in-use."""
    prox = _FakeProxmox(
        [
            {"type": "qemu", "vmid": 100, "node": "pve1"},
            {"type": "lxc", "vmid": 101, "node": "pve1"},
            {"type": "qemu", "vmid": 200, "node": "pve2"},
            {"type": "qemu", "vmid": "bad"},
            {"type": "storage", "vmid": 999},  # ignored
        ]
    )
    assert qemu_vmids_in_use_cluster(prox) == {100, 101, 200}  # type: ignore[arg-type]


def test_allocate_skips_lxc_held_vmid() -> None:
    from pve_client import allocate_sequential_free_vmids

    # LXC occupies 100; next free for Normal restore must be 101+.
    used = qemu_vmids_in_use_cluster(
        _FakeProxmox([{"type": "lxc", "vmid": 100, "node": "pve"}])  # type: ignore[arg-type]
    )
    ids, cursor = allocate_sequential_free_vmids(set(used), 100, 2)
    assert ids == [101, 102]
    assert cursor == 103


def test_assign_nodes_least_loaded_spreads_and_respects_active() -> None:
    # Empty active counts: round-robin by list order among equals.
    assert assign_nodes_least_loaded(["a", "b"], 4) == ["a", "b", "a", "b"]
    # Prefer the less busy node first.
    assert assign_nodes_least_loaded(["a", "b"], 3, active_counts={"a": 2, "b": 0}) == ["b", "b", "a"]


def test_assign_nodes_requires_candidates() -> None:
    with pytest.raises(ValueError, match="at least one"):
        assign_nodes_least_loaded([], 1)


def test_enqueue_load_balances_across_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_plans import FakeRedis

    r = FakeRedis()
    cfg = {
        "redis": {
            "job_key_prefix": "restore:job:",
            "job_log_suffix": ":log",
            "queue_key": "restore:queue",
        }
    }
    rows = [
        {
            "backup_id": f"vm/{i}/2026-01-01T00:00:00Z",
            "vmid": i,
            "name": f"vm{i}",
            "pve_storage": "pbs",
            "voltail": f"vm/{i}/2026-01-01T00:00:00Z",
            "source_label": "",
        }
        for i in (10, 11, 12, 13)
    ]

    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda prox: {3500})

    # Seed one active job on pve1 so new work prefers pve2 first.
    r.hset(
        "restore:job:seed",
        mapping={
            "state": RestoreState.RESTORING.value,
            "proxmox_node": "pve1",
        },
    )

    result = enqueue_restores(
        r,
        cfg,
        rows,
        nodes=["pve1", "pve2"],
        target_storage="local-lvm",
        vmid_start=3600,
        live_restore=True,
        bwlimit=0,
    )
    assert result["enqueued"] == 4
    assert result["proxmox_vmids_assigned"] == [3600, 3601, 3602, 3603]
    # pve2 starts with 0 active vs pve1's 1 → pve2 first, then alternate by load.
    assert result["proxmox_nodes_assigned"] == ["pve2", "pve1", "pve2", "pve1"]
    assert active_restore_counts_by_node(r, cfg)["pve1"] >= 1


def test_enqueue_storage_by_node(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_plans import FakeRedis

    r = FakeRedis()
    cfg = {
        "redis": {
            "job_key_prefix": "restore:job:",
            "job_log_suffix": ":log",
            "queue_key": "restore:queue",
        }
    }
    rows = [
        {
            "backup_id": f"vm/{i}/2026-01-01T00:00:00Z",
            "vmid": i,
            "name": f"vm{i}",
            "pve_storage": "pbs",
            "voltail": f"vm/{i}/2026-01-01T00:00:00Z",
            "source_label": "",
        }
        for i in (1, 2)
    ]
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda prox: set())

    result = enqueue_restores(
        r,
        cfg,
        rows,
        nodes=["pve1", "pve2"],
        storage_by_node={"pve1": "zfs-mirror", "pve2": "local-lvm"},
        vmid_start=100,
        live_restore=False,
        bwlimit=0,
    )
    assert result["proxmox_nodes_assigned"] == ["pve1", "pve2"]
    assert result["proxmox_storages_assigned"] == ["zfs-mirror", "local-lvm"]
    job0 = r.hgetall("restore:job:" + result["job_ids"][0])
    assert job0["proxmox_storage"] == "zfs-mirror"


def test_normalize_location_nodes() -> None:
    from plans import normalize_location

    loc = normalize_location(
        {"name": "DR", "node": "pve1", "nodes": ["pve2", "pve1", "pve2"], "storage": "local-lvm"}
    )
    assert loc["node"] == "pve1"
    assert loc["nodes"] == ["pve1", "pve2"]
    assert loc["storage_by_node"] == {"pve1": "local-lvm", "pve2": "local-lvm"}

    loc2 = normalize_location(
        {
            "name": "DR",
            "nodes": ["pveA", "pveB"],
            "storage_by_node": {"pveA": "zfs-mirror", "pveB": "nvme-zfs"},
        }
    )
    assert loc2["node"] == "pveA"
    assert loc2["storage"] == "zfs-mirror"
    assert loc2["storage_by_node"] == {"pveA": "zfs-mirror", "pveB": "nvme-zfs"}


def test_resolve_storage_for_node() -> None:
    from pve_client import resolve_storage_for_node

    assert resolve_storage_for_node("a", storage_by_node={"a": "zfs"}, default_storage="lvm") == "zfs"
    assert resolve_storage_for_node("b", storage_by_node={"a": "zfs"}, default_storage="lvm") == "lvm"
    with pytest.raises(RuntimeError, match="No target storage"):
        resolve_storage_for_node("b", storage_by_node={"a": "zfs"}, default_storage="")
