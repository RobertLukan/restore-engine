"""API token auth and audit log."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(main_module: Any) -> TestClient:
    return TestClient(main_module.app)


def test_bearer_operator_can_read_jobs(
    client: TestClient, ui_module: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = ui_module.load_yaml()
    cfg.setdefault("ui", {})["api_tokens"] = [
        {"name": "ci", "token": "test-operator-token", "role": "operator"}
    ]
    path = tmp_path / "cfg.yaml"
    import yaml

    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setattr(ui_module, "CONFIG_PATH", path)
    # Also point main's load if needed — ui.CONFIG_PATH is what require_ui_session uses.
    r = client.get("/api/jobs", headers={"Authorization": "Bearer test-operator-token"})
    assert r.status_code == 200
    body = r.json()
    assert "items" in body


def test_viewer_token_cannot_post(
    client: TestClient, ui_module: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = ui_module.load_yaml()
    cfg.setdefault("ui", {})["api_tokens"] = [
        {"name": "ro", "token": "test-viewer-token", "role": "viewer"}
    ]
    path = tmp_path / "cfg.yaml"
    import yaml

    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setattr(ui_module, "CONFIG_PATH", path)
    r = client.post(
        "/api/jobs/queue/pause",
        headers={"Authorization": "Bearer test-viewer-token"},
    )
    assert r.status_code == 403
