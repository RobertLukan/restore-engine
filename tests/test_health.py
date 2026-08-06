from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient


def _base_cfg() -> dict[str, Any]:
    return {
        "pbs": {"host": "x"},
        "proxmox": {"host": "x"},
        "redis": {"url": "redis://unused:6379/0", "job_key_prefix": "x:"},
    }


def test_health_ok(monkeypatch: pytest.MonkeyPatch, main_module: Any, ui_module: Any) -> None:
    monkeypatch.setattr(main_module, "load_config", _base_cfg)

    fake_r = MagicMock()
    fake_r.ping.return_value = True
    monkeypatch.setattr(main_module, "redis_client", lambda: fake_r)
    monkeypatch.setattr(ui_module, "health_pbs_component", lambda c: {"ok": True, "detail": "mock"})
    monkeypatch.setattr(ui_module, "health_proxmox_component", lambda c: {"ok": True, "detail": "mock"})

    client = TestClient(main_module.app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["components"]["config"]["ok"] is True
    assert body["components"]["redis"]["ok"] is True
    assert body["components"]["pbs"]["ok"] is True
    assert body["components"]["proxmox"]["ok"] is True


def test_health_degraded_when_redis_fails(monkeypatch: pytest.MonkeyPatch, main_module: Any, ui_module: Any) -> None:
    monkeypatch.setattr(main_module, "load_config", _base_cfg)

    def boom() -> MagicMock:
        r = MagicMock()

        def ping() -> None:
            raise ConnectionError("redis down")

        r.ping = ping
        return r

    monkeypatch.setattr(main_module, "redis_client", boom)
    monkeypatch.setattr(ui_module, "health_pbs_component", lambda c: {"ok": True, "detail": "mock"})
    monkeypatch.setattr(ui_module, "health_proxmox_component", lambda c: {"ok": True, "detail": "mock"})

    client = TestClient(main_module.app)
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "degraded"
    assert body["components"]["redis"]["ok"] is False


def test_health_degraded_when_config_section_missing(
    monkeypatch: pytest.MonkeyPatch, main_module: Any
) -> None:
    monkeypatch.setattr(main_module, "load_config", lambda: {"redis": {"url": "redis://x"}})
    fake_r = MagicMock()
    fake_r.ping.return_value = True
    monkeypatch.setattr(main_module, "redis_client", lambda: fake_r)

    client = TestClient(main_module.app)
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["components"]["config"]["ok"] is False
