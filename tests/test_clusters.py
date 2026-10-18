"""Tests for pve_clusters shim, connect-by-id, mount defaulting, and job field."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from clusters import (
    DEFAULT_CLUSTER_ID,
    default_pve_cluster_id,
    get_pve_cluster,
    load_pve_clusters,
    mirror_proxmox_from_clusters,
    normalize_mount_cluster_id,
)
from jobs import enqueue_restores
from sources import load_sources


def test_legacy_proxmox_shim_to_default_cluster() -> None:
    cfg = {
        "proxmox": {
            "host": "10.0.0.20",
            "port": 8006,
            "user": "root@pam",
            "password": "secret",
            "default_node": "pve1",
            "storage": "local-lvm",
        }
    }
    clusters = load_pve_clusters(cfg)
    assert len(clusters) == 1
    assert clusters[0]["id"] == DEFAULT_CLUSTER_ID
    assert clusters[0]["host"] == "10.0.0.20"
    assert clusters[0]["default_node"] == "pve1"
    assert get_pve_cluster(cfg)["host"] == "10.0.0.20"
    assert default_pve_cluster_id(cfg) == DEFAULT_CLUSTER_ID


def test_pve_clusters_list_preferred_over_legacy() -> None:
    cfg = {
        "proxmox": {"host": "legacy.example", "user": "root@pam", "password": "x"},
        "pve_clusters": [
            {
                "id": "dr",
                "host": "10.1.1.1",
                "user": "root@pam",
                "password": "y",
                "default_node": "dr1",
            },
            {
                "id": "default",
                "host": "10.0.0.20",
                "user": "root@pam",
                "password": "z",
            },
        ],
    }
    clusters = load_pve_clusters(cfg)
    assert [c["id"] for c in clusters] == ["dr", "default"]
    assert get_pve_cluster(cfg, "dr")["host"] == "10.1.1.1"
    assert get_pve_cluster(cfg)["id"] == "default"
    assert default_pve_cluster_id(cfg) == "default"


def test_connect_proxmox_by_cluster_id() -> None:
    from pve_client import connect_proxmox

    cfg = {
        "pve_clusters": [
            {
                "id": "a",
                "host": "10.0.0.1",
                "port": 8006,
                "verify_ssl": False,
                "api_token_id": "root@pam!t",
                "api_token_secret": "sec-a",
            },
            {
                "id": "b",
                "host": "10.0.0.2",
                "port": 8006,
                "verify_ssl": False,
                "api_token_id": "root@pam!t",
                "api_token_secret": "sec-b",
            },
        ]
    }
    with patch("pve_client.ProxmoxAPI") as mock_api:
        connect_proxmox(cfg, cluster_id="b")
        kwargs = mock_api.call_args.kwargs
        assert kwargs["host"] == "10.0.0.2"
        assert kwargs["token_value"] == "sec-b"


def test_mount_defaults_pve_cluster_id() -> None:
    cfg = {
        "proxmox": {"host": "10.0.0.20", "user": "root@pam", "password": "x"},
        "pbs_servers": [
            {
                "id": "main",
                "host": "10.0.0.10",
                "mounts": [
                    {"datastore": "main", "namespace": "", "pve_storage": "pbs-main"},
                    {
                        "datastore": "main",
                        "namespace": "team",
                        "pve_storage": "pbs-team",
                        "pve_cluster_id": "dr",
                    },
                ],
            }
        ],
    }
    sources = load_sources(cfg)
    assert sources[0].pve_cluster_id == DEFAULT_CLUSTER_ID
    assert sources[1].pve_cluster_id == "dr"
    assert normalize_mount_cluster_id("") == DEFAULT_CLUSTER_ID
    assert normalize_mount_cluster_id("  x  ") == "x"


def test_mirror_proxmox_from_clusters() -> None:
    cfg: dict[str, Any] = {
        "pve_clusters": [
            {"id": "default", "host": "10.0.0.20", "user": "root@pam", "password": "pw"},
        ]
    }
    mirror_proxmox_from_clusters(cfg)
    assert cfg["proxmox"]["host"] == "10.0.0.20"
    assert "id" not in cfg["proxmox"]


def test_enqueue_stores_pve_cluster_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_plans import FakeRedis

    cfg = {
        "redis": {"job_key_prefix": "job:", "queue_key": "q", "job_log_suffix": ":log"},
        "proxmox": {
            "host": "10.0.0.20",
            "user": "root@pam",
            "password": "x",
            "default_node": "pve",
            "storage": "local-lvm",
        },
        "worker": {"enqueue_largest_first": False},
    }
    rows = [
        {
            "backup_id": "main/main/root|vm/100/2020-01-01T00:00:00Z",
            "vmid": 100,
            "name": "web",
            "pve_storage": "pbs-main",
            "voltail": "vm/100/2020-01-01T00:00:00Z",
            "pve_cluster_id": "default",
            "size_bytes": 1000,
        }
    ]
    r = FakeRedis()
    monkeypatch.setattr("jobs.connect_proxmox", lambda cfg, cluster_id=None: object())
    monkeypatch.setattr("jobs.qemu_vmids_in_use_cluster", lambda prox: set())

    result = enqueue_restores(
        r,
        cfg,
        rows,
        nodes=["pve"],
        target_storage="local-lvm",
        storage_by_node={"pve": "local-lvm"},
        vmid_start=200,
        live_restore=False,
        bwlimit=0,
    )

    assert result["pve_cluster_id"] == "default"
    assert result["enqueued"] == 1
    job_id = result["job_ids"][0]
    data = r.hgetall(f"job:{job_id}")
    assert data.get("pve_cluster_id") == "default"
