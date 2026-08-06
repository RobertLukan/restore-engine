from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from starlette.testclient import TestClient


@pytest.fixture
def client(main_module: Any) -> TestClient:
    return TestClient(main_module.app)


def _login(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"password": "test-dashboard-secret"})
    assert r.status_code == 200


def test_credentials_include_worker_and_restore_defaults(client: TestClient) -> None:
    _login(client)
    data = client.get("/api/ui/credentials").json()
    assert "worker" in data
    assert data["worker"]["max_concurrent_restores"] == 1  # from minimal_config fixture
    assert data["proxmox"]["restore_bwlimit"] == 0
    assert data["proxmox"]["live_restore_default"] is False


def test_restore_defaults_expose_perf_settings(client: TestClient) -> None:
    _login(client)
    defs = client.get("/api/restore-defaults").json()
    assert defs["bwlimit"] == 0
    assert defs["live_restore"] is False
    assert defs["max_concurrent_restores"] == 1


def test_put_worker_and_defaults_persist(
    client: TestClient, ui_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    # Work against a throwaway copy so the shared fixture is never mutated.
    tmp_cfg = tmp_path / "config.yaml"
    shutil.copy(ui_module.CONFIG_PATH, tmp_cfg)
    monkeypatch.setattr(ui_module, "CONFIG_PATH", tmp_cfg)

    r = client.put(
        "/api/ui/credentials",
        json={
            "worker": {"max_concurrent_restores": 5},
            "proxmox": {"restore_bwlimit": 40960, "live_restore_default": True},
        },
    )
    assert r.status_code == 200

    saved = yaml.safe_load(tmp_cfg.read_text())
    assert saved["worker"]["max_concurrent_restores"] == 5
    assert saved["proxmox"]["restore_bwlimit"] == 40960
    assert saved["proxmox"]["live_restore_default"] is True


def test_put_pbs_servers_preserves_masked_secret_and_drops_legacy(
    client: TestClient, ui_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    tmp_cfg = tmp_path / "config.yaml"
    shutil.copy(ui_module.CONFIG_PATH, tmp_cfg)
    monkeypatch.setattr(ui_module, "CONFIG_PATH", tmp_cfg)

    mask = ui_module.MASK
    r = client.put(
        "/api/ui/credentials",
        json={
            "pbs_servers": [
                {
                    "id": "main",
                    "host": "10.9.9.9",
                    "port": 8007,
                    "verify_ssl": False,
                    "api_token_id": "root@pam!restore",
                    "api_token_secret": mask,  # unchanged -> keep saved secret
                    "mounts": [
                        {"datastore": "main", "namespace": "", "pve_storage": "pbs-main"},
                        {"datastore": "main", "namespace": "team-a", "pve_storage": "pbs-teamA"},
                    ],
                },
                {
                    "id": "dr",
                    "host": "10.8.8.8",
                    "api_token_id": "root@pam!restore",
                    "api_token_secret": "brand-new-secret",
                    "mounts": [{"datastore": "backup", "namespace": "", "pve_storage": "pbs-dr"}],
                },
            ]
        },
    )
    assert r.status_code == 200

    saved = yaml.safe_load(tmp_cfg.read_text())
    servers = saved["pbs_servers"]
    assert len(servers) == 2
    main = next(s for s in servers if s["id"] == "main")
    assert main["host"] == "10.9.9.9"
    assert main["api_token_secret"] == "unused-in-tests"  # preserved from fixture
    assert len(main["mounts"]) == 2
    dr = next(s for s in servers if s["id"] == "dr")
    assert dr["api_token_secret"] == "brand-new-secret"
    # Legacy single-PBS keys are removed.
    assert "pbs" not in saved
    assert "pbs_storage" not in saved.get("proxmox", {})


def test_put_worker_clamps_to_minimum_one(
    client: TestClient, ui_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    tmp_cfg = tmp_path / "config.yaml"
    shutil.copy(ui_module.CONFIG_PATH, tmp_cfg)
    monkeypatch.setattr(ui_module, "CONFIG_PATH", tmp_cfg)

    r = client.put("/api/ui/credentials", json={"worker": {"max_concurrent_restores": 0}})
    assert r.status_code == 200
    saved = yaml.safe_load(tmp_cfg.read_text())
    assert saved["worker"]["max_concurrent_restores"] == 1
