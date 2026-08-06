from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient


def test_version_public(main_module: Any) -> None:
    client = TestClient(main_module.app)
    r = client.get("/version")
    assert r.status_code == 200
    data = r.json()
    assert data.get("version")
    assert data.get("name")
