from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(main_module: Any) -> TestClient:
    return TestClient(main_module.app)


def test_jobs_requires_session(client: TestClient) -> None:
    r = client.get("/api/jobs")
    assert r.status_code == 401


def test_login_and_credentials_roundtrip(client: TestClient) -> None:
    bad = client.post("/api/auth/login", json={"password": "wrong"})
    assert bad.status_code == 401

    ok = client.post("/api/auth/login", json={"password": "test-dashboard-secret"})
    assert ok.status_code == 200

    creds = client.get("/api/ui/credentials")
    assert creds.status_code == 200
    data = creds.json()
    assert "pbs_servers" in data and "proxmox" in data
    assert data["proxmox"].get("host") == "127.0.0.1"
    servers = data["pbs_servers"]
    assert len(servers) == 1
    assert servers[0]["id"] == "main"
    assert servers[0]["mounts"][0]["datastore"] == "main"
    assert servers[0]["mounts"][0]["pve_storage"] == "pbs-main"
    # Secrets are masked, never returned in the clear.
    assert servers[0].get("api_token_secret") == main_secret_mask()
    assert data["proxmox"].get("api_token_secret") == main_secret_mask()


def main_secret_mask() -> str:
    import ui

    return ui.MASK
