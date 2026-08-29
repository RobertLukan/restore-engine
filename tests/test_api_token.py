"""API token auth and audit log."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.test_plans import FakeRedis


@pytest.fixture
def client(main_module: Any) -> TestClient:
    return TestClient(main_module.app)


def _patch_token_config(
    ui_module: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, tokens: list[dict[str, str]]
) -> None:
    import yaml

    cfg = ui_module.load_yaml()
    cfg.setdefault("ui", {})["api_tokens"] = tokens
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setattr(ui_module, "CONFIG_PATH", path)


def test_bearer_operator_can_read_jobs(
    client: TestClient,
    main_module: Any,
    ui_module: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_token_config(
        ui_module,
        tmp_path,
        monkeypatch,
        [{"name": "ci", "token": "test-operator-token", "role": "operator"}],
    )
    monkeypatch.setattr(main_module, "redis_client", lambda: FakeRedis())
    r = client.get("/api/jobs", headers={"Authorization": "Bearer test-operator-token"})
    assert r.status_code == 200
    body = r.json()
    assert "items" in body


def test_viewer_token_cannot_post(
    client: TestClient, ui_module: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_token_config(
        ui_module,
        tmp_path,
        monkeypatch,
        [{"name": "ro", "token": "test-viewer-token", "role": "viewer"}],
    )
    r = client.post(
        "/api/jobs/queue/pause",
        headers={"Authorization": "Bearer test-viewer-token"},
    )
    assert r.status_code == 403
