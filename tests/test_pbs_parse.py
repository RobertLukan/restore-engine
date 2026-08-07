from __future__ import annotations

from typing import Any

import pytest

import pbs_client


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, calls: list[dict[str, Any]], payload: dict[str, Any]) -> None:
        self._calls = calls
        self._payload = payload

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def get(self, url: str, headers: dict[str, str] | None = None, params: Any = None) -> _FakeResponse:
        self._calls.append({"url": url, "params": params})
        return _FakeResponse(200, self._payload)


def _cfg_two_sources() -> dict[str, Any]:
    return {
        "pbs_servers": [
            {
                "id": "main",
                "host": "127.0.0.1",
                "port": 8007,
                "verify_ssl": False,
                "api_token_id": "root@pam!restore",
                "api_token_secret": "secret",
                "mounts": [
                    {"datastore": "main", "namespace": "", "pve_storage": "pbs-main"},
                    {"datastore": "main", "namespace": "team-a", "pve_storage": "pbs-main-teamA"},
                ],
            }
        ]
    }


def _install_fake(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    monkeypatch.setattr(pbs_client.httpx, "Client", lambda *a, **k: _FakeClient(calls, payload))


def test_list_vm_backups_tags_source_and_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "data": [
            {"backup-type": "vm", "backup-id": "100", "backup-time": 0, "comment": "web01", "size": 123456789},
            {"backup-type": "ct", "backup-id": "200", "backup-time": 0},
        ]
    }
    calls: list[dict[str, Any]] = []
    _install_fake(monkeypatch, calls, payload)

    rows = pbs_client.list_vm_backups(_cfg_two_sources())

    # One vm snapshot per source (root + team-a); ct filtered out.
    assert len(rows) == 2
    by_storage = {r["pve_storage"]: r for r in rows}
    assert set(by_storage) == {"pbs-main", "pbs-main-teamA"}
    root = by_storage["pbs-main"]
    assert root["voltail"] == "vm/100/1970-01-01T00:00:00Z"
    assert root["backup_id"] == "main/main/root|vm/100/1970-01-01T00:00:00Z"
    assert root["namespace"] == ""
    assert root["size_bytes"] == 123456789
    teama = by_storage["pbs-main-teamA"]
    assert teama["namespace"] == "team-a"
    assert teama["backup_id"].startswith("main/main/team-a|")

    # The namespaced mount must pass ns=team-a; the root one must not.
    ns_values = {c["params"].get("ns") if c["params"] else None for c in calls}
    assert "team-a" in ns_values
    assert None in ns_values


def test_list_vm_backups_requires_sources() -> None:
    with pytest.raises(ValueError, match="No PBS sources"):
        pbs_client.list_vm_backups({"proxmox": {}})
