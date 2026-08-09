"""Scheduled plan start resolves guest tags (same path as readiness)."""

from __future__ import annotations

from typing import Any

import plans
from tests.test_plans import CFG, FakeRedis


def test_resolve_tags_cached_uses_redis_and_extract(monkeypatch: Any) -> None:
    r = FakeRedis()
    row = {
        "backup_id": "vm/10/2026-02-01T00:00:00Z",
        "vmid": 10,
        "pve_storage": "pbs",
        "voltail": "vm/10/2026-02-01T00:00:00Z",
    }
    calls = {"n": 0}

    def fake_extract(_px: Any, _node: str, volid: str) -> str:
        calls["n"] += 1
        assert "vm/10" in volid
        return "tags: prod;app\n"

    monkeypatch.setattr("pve_client.extract_vm_config", fake_extract)
    tags, errors = plans._resolve_tags_cached(r, CFG, [row], "pve1", object())
    assert errors == {}
    assert tags[row["backup_id"]] == ["prod", "app"]
    assert calls["n"] == 1
    tags2, _ = plans._resolve_tags_cached(r, CFG, [row], "pve1", object())
    assert tags2[row["backup_id"]] == ["prod", "app"]
    assert calls["n"] == 1  # cache hit


def test_scheduled_style_tag_resolve_filters_group() -> None:
    """Empty tags_by_id would drop tag groups; populated map keeps matching VMs."""
    backups = [
        {
            "backup_id": "vm/10/2026-02-01T00:00:00Z",
            "vmid": 10,
            "name": "a",
            "timestamp": "2026-02-01T00:00:00Z",
            "pve_storage": "pbs",
            "voltail": "vm/10/2026-02-01T00:00:00Z",
            "source_id": "main",
        },
        {
            "backup_id": "vm/20/2026-02-01T00:00:00Z",
            "vmid": 20,
            "name": "b",
            "timestamp": "2026-02-01T00:00:00Z",
            "pve_storage": "pbs",
            "voltail": "vm/20/2026-02-01T00:00:00Z",
            "source_id": "main",
        },
    ]
    group = {"name": "g", "tags": ["prod"], "vmids": [], "source_ids": []}
    empty = plans.resolve_group_rows(
        group, backups, cutoff="2026-12-31T23:59:59Z", tags_by_backup_id={}
    )
    assert empty == []
    tagged = plans.resolve_group_rows(
        group,
        backups,
        cutoff="2026-12-31T23:59:59Z",
        tags_by_backup_id={
            "vm/10/2026-02-01T00:00:00Z": ["prod"],
            "vm/20/2026-02-01T00:00:00Z": ["other"],
        },
    )
    assert [row["vmid"] for row in tagged] == [10]
