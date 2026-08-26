"""Tests for infra metrics config and API snapshot endpoint."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from metrics_collect import monitoring_config


def test_monitoring_config_grafana_embed_urls() -> None:
    cfg = {
        "monitoring": {
            "enabled": True,
            "api_snapshot": True,
            "grafana": {
                "base_url": "http://lab:3001/",
                "dashboards": [
                    {
                        "id": "infra",
                        "title": "Infra",
                        "uid": "restore-infra",
                        "path": "/d/restore-infra/restore-infra?orgId=1&kiosk&theme=dark",
                    }
                ],
            },
        }
    }
    mon = monitoring_config(cfg)
    assert mon["enabled"] is True
    assert mon["grafana"]["base_url"] == "http://lab:3001"
    assert mon["grafana"]["dashboards"][0]["embed_url"].startswith(
        "http://lab:3001/d/restore-infra/"
    )


def test_monitoring_config_defaults_without_section() -> None:
    mon = monitoring_config({})
    assert mon["api_snapshot"] is True
    assert mon["grafana"] == {}


@pytest.fixture
def client(main_module: Any) -> TestClient:
    return TestClient(main_module.app)


def _login(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"password": "test-dashboard-secret"}).status_code == 200


def test_infra_metrics_endpoint(
    client: TestClient, main_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)

    def fake_snapshot(cfg):
        return {
            "ts": "2026-01-01T00:00:00+00:00",
            "monitoring": {"enabled": True, "grafana": {}, "api_snapshot": True},
            "nodes": [
                {
                    "id": "pve1",
                    "source": "pve",
                    "online": True,
                    "cpu": 0.25,
                    "mem_used": 8 * 1024**3,
                    "mem_total": 32 * 1024**3,
                    "net_in_bps": 1e6,
                    "net_out_bps": 2e6,
                }
            ],
            "pbs": [],
            "interfaces": [],
            "errors": [],
        }

    monkeypatch.setattr(main_module, "collect_infra_snapshot", fake_snapshot)
    res = client.get("/api/infra/metrics")
    assert res.status_code == 200
    body = res.json()
    assert body["nodes"][0]["id"] == "pve1"
    assert body["nodes"][0]["cpu"] == 0.25


def test_infra_monitoring_config_endpoint(client: TestClient, main_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _login(client)
    monkeypatch.setattr(
        main_module,
        "monitoring_config",
        lambda cfg: {
            "enabled": True,
            "api_snapshot": True,
            "grafana": {
                "base_url": "http://x:3001",
                "dashboards": [
                    {
                        "id": "infra",
                        "title": "Infra",
                        "uid": "restore-infra",
                        "path": "/d/restore-infra/x",
                        "embed_url": "http://x:3001/d/restore-infra/x",
                    }
                ],
            },
        },
    )
    res = client.get("/api/infra/monitoring")
    assert res.status_code == 200
    assert res.json()["grafana"]["dashboards"][0]["embed_url"].startswith("http://x:3001/")
