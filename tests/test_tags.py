from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from pve_client import parse_tags


# ---- pure helpers -----------------------------------------------------------

def test_parse_tags_semicolon_and_dedup() -> None:
    cfg = "name: web01\ntags: deployed;prod;deployed\ncores: 4\n"
    assert parse_tags(cfg) == ["deployed", "prod"]


def test_parse_tags_single() -> None:
    assert parse_tags("tags: deployed\n") == ["deployed"]


def test_parse_tags_absent() -> None:
    assert parse_tags("name: web01\ncores: 4\n") == []


def test_normalize_cutoff(main_module: Any) -> None:
    n = main_module.normalize_cutoff
    assert n("") == "9999-12-31T23:59:59Z"
    assert n(None) == "9999-12-31T23:59:59Z"
    assert n("2026-05-01") == "2026-05-01T23:59:59Z"
    assert n("2026-05-01T14:30") == "2026-05-01T14:30:59Z"
    assert n("2026-05-01T14:30:00Z") == "2026-05-01T14:30:00Z"


def _row(vmid: int, ts: str, sid: str = "main/main/root") -> dict[str, Any]:
    return {
        "backup_id": f"{sid}|vm/{vmid}/{ts}",
        "voltail": f"vm/{vmid}/{ts}",
        "vmid": vmid,
        "name": f"vm-{vmid}",
        "timestamp": ts,
        "pve_storage": "pbs-main",
        "source_label": "main",
    }


def test_latest_per_vmid_respects_cutoff(main_module: Any) -> None:
    rows = [
        _row(100, "2026-05-01T00:00:00Z"),
        _row(100, "2026-05-03T00:00:00Z"),
        _row(100, "2026-05-05T00:00:00Z"),  # after cutoff
        _row(101, "2026-04-01T00:00:00Z"),
    ]
    picked = main_module._latest_per_vmid(rows, "2026-05-04T00:00:00Z")
    by_vmid = {r["vmid"]: r for r in picked}
    assert by_vmid[100]["timestamp"] == "2026-05-03T00:00:00Z"
    assert by_vmid[101]["timestamp"] == "2026-04-01T00:00:00Z"


# ---- endpoints --------------------------------------------------------------

@pytest.fixture
def client(main_module: Any) -> TestClient:
    return TestClient(main_module.app)


def _login(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"password": "test-dashboard-secret"}).status_code == 200


def test_resolve_tags_endpoint(client: TestClient, main_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _login(client)
    rows = [_row(100, "2026-05-01T00:00:00Z"), _row(101, "2026-05-01T00:00:00Z")]
    monkeypatch.setattr(main_module, "list_vm_backups", lambda cfg: rows)
    monkeypatch.setattr(
        main_module,
        "_resolve_tags",
        lambda cfg, r, node, force=False: (
            {rows[0]["backup_id"]: ["deployed"], rows[1]["backup_id"]: ["test"]},
            {},
        ),
    )
    res = client.post("/api/backups/resolve-tags", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["all_tags"] == ["deployed", "test"]
    assert body["tags"][rows[0]["backup_id"]] == ["deployed"]


def test_restore_tag_group_selects_latest_tagged(
    client: TestClient, main_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    rows = [
        _row(100, "2026-05-01T00:00:00Z"),
        _row(100, "2026-05-03T00:00:00Z"),  # latest for 100 <= cutoff, tagged
        _row(101, "2026-05-02T00:00:00Z"),  # 101 latest, NOT tagged
    ]
    monkeypatch.setattr(main_module, "list_vm_backups", lambda cfg: rows)

    def fake_tags(cfg, candidate_rows, node, force=False):
        out = {}
        for row in candidate_rows:
            out[row["backup_id"]] = ["deployed"] if row["vmid"] == 100 else ["other"]
        return out, {}

    monkeypatch.setattr(main_module, "_resolve_tags", fake_tags)
    monkeypatch.setattr(main_module, "redis_client", lambda: object())

    captured: dict[str, Any] = {}

    def fake_enqueue(r, cfg, selected, **kwargs):
        captured["selected"] = selected
        captured["kwargs"] = kwargs
        return {"enqueued": len(selected), "job_ids": ["x"] * len(selected), "proxmox_vmids_assigned": [100]}

    monkeypatch.setattr(main_module, "_enqueue_restores", fake_enqueue)

    res = client.post(
        "/api/jobs/restore-tag-group",
        json={
            "tag": "deployed",
            "at_or_before": "2026-05-04",
            "proxmox_storage": "local-lvm",
            "proxmox_vmid_start": 200,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["enqueued"] == 1
    assert body["matched_vmids"] == [100]
    # Only the latest tagged snapshot for VM 100 was chosen.
    assert [r["backup_id"] for r in captured["selected"]] == [rows[1]["backup_id"]]


def test_restore_tag_group_no_match_returns_zero(
    client: TestClient, main_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    rows = [_row(100, "2026-05-03T00:00:00Z")]
    monkeypatch.setattr(main_module, "list_vm_backups", lambda cfg: rows)
    monkeypatch.setattr(
        main_module,
        "_resolve_tags",
        lambda cfg, r, node, force=False: ({rows[0]["backup_id"]: ["other"]}, {}),
    )
    monkeypatch.setattr(main_module, "redis_client", lambda: object())
    res = client.post(
        "/api/jobs/restore-tag-group",
        json={"tag": "deployed", "proxmox_storage": "local-lvm", "proxmox_vmid_start": 200},
    )
    assert res.status_code == 200
    assert res.json()["enqueued"] == 0
