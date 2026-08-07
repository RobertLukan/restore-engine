"""Tests for Backups-table VMID grouping (UI parity helper)."""

from __future__ import annotations

from backup_inventory import group_backups_by_vmid


def _snap(vmid: int, ts: str, name: str = "vm", **extra: object) -> dict:
    row = {
        "vmid": vmid,
        "timestamp": ts,
        "name": name,
        "backup_id": f"src|vm/{vmid}/{ts}",
        "source_label": "main · ds",
        "size_bytes": 100,
    }
    row.update(extra)
    return row


def test_group_backups_by_vmid_latest_default() -> None:
    rows = [
        _snap(100, "2026-05-01T00:00:00Z", name="old"),
        _snap(100, "2026-05-03T00:00:00Z", name="new"),
        _snap(101, "2026-04-01T00:00:00Z", name="other"),
        _snap(100, "2026-05-02T00:00:00Z", name="mid"),
    ]
    groups = group_backups_by_vmid(rows)
    assert [g["vmid"] for g in groups] == [100, 101]
    g100 = groups[0]
    assert g100["latest"]["timestamp"] == "2026-05-03T00:00:00Z"
    assert g100["name"] == "new"
    assert [s["timestamp"] for s in g100["snapshots"]] == [
        "2026-05-03T00:00:00Z",
        "2026-05-02T00:00:00Z",
        "2026-05-01T00:00:00Z",
    ]
    assert len(g100["snapshots"]) == 3
    assert groups[1]["latest"]["name"] == "other"


def test_group_backups_by_vmid_empty() -> None:
    assert group_backups_by_vmid([]) == []
