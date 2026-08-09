"""Tests for on-demand non-zero (fidx usage) size estimates."""

from __future__ import annotations

import hashlib
import json
import struct
from typing import Any

import pytest
from starlette.testclient import TestClient

from pbs_wire import _parse_fidx, usage_from_digests


def test_usage_from_digests_counts_zero_and_nonzero() -> None:
    chunk = 4 * 1024 * 1024
    zero = hashlib.sha256(b"\x00" * chunk).digest()
    other = hashlib.sha256(b"\x01" * chunk).digest()
    other2 = hashlib.sha256(b"\x02" * chunk).digest()
    est = usage_from_digests(chunk, [zero, other, other, other2, zero], fidx_files=2)
    assert est.total_positions == 5
    assert est.nonzero_positions == 3
    assert est.unique_nonzero == 2
    assert est.fidx_files == 2
    assert est.virtual_bytes == 5 * chunk
    assert est.nonzero_bytes == 3 * chunk
    assert est.zero_bytes == 2 * chunk
    assert abs(est.sparsity_ratio - 0.6) < 1e-9
    d = est.as_dict()
    assert d["nonzero_bytes"] == 3 * chunk
    assert d["virtual_bytes"] == 5 * chunk


def test_usage_from_parsed_fidx() -> None:
    chunk = 1024 * 1024
    zero = hashlib.sha256(b"\x00" * chunk).digest()
    other = hashlib.sha256(b"\xaa" * chunk).digest()
    header = bytearray(4096)
    struct.pack_into("<Q", header, 64, chunk * 4)
    struct.pack_into("<Q", header, 72, chunk)
    body = bytes(header) + zero + zero + other + zero
    chunk_size, digests = _parse_fidx(body)
    est = usage_from_digests(chunk_size, digests)
    assert est.nonzero_positions == 1
    assert est.nonzero_bytes == chunk


@pytest.fixture
def client(main_module: Any) -> TestClient:
    return TestClient(main_module.app)


def _login(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"password": "test-dashboard-secret"}).status_code == 200


def test_estimate_size_endpoint(
    client: TestClient, main_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    bid = "main/ds/root|vm/100/2026-05-01T00:00:00Z"
    monkeypatch.setattr(
        main_module,
        "list_vm_backups",
        lambda cfg: [{"backup_id": bid, "size_bytes": 50_000_000_000, "vmid": 100}],
    )

    def fake_est(r, cfg, backup_id):
        assert backup_id == bid
        return {
            "backup_id": backup_id,
            "cached": False,
            "chunk_size": 4 * 1024 * 1024,
            "fidx_files": 1,
            "total_positions": 10,
            "nonzero_positions": 2,
            "unique_nonzero": 2,
            "sparsity_ratio": 0.2,
            "virtual_bytes": 40 * 1024 * 1024,
            "nonzero_bytes": 8 * 1024 * 1024,
            "zero_bytes": 32 * 1024 * 1024,
        }

    monkeypatch.setattr(main_module, "estimate_fidx_usage_cached", fake_est)
    monkeypatch.setattr(main_module, "redis_client", lambda: object())

    res = client.post("/api/backups/estimate-size", json={"backup_ids": [bid]})
    assert res.status_code == 200
    body = res.json()
    assert bid in body["estimates"]
    assert body["estimates"][bid]["nonzero_bytes"] == 8 * 1024 * 1024
    assert body["estimates"][bid]["size_bytes"] == 50_000_000_000
    assert body["errors"] == {}


def test_estimate_size_requires_ids(client: TestClient) -> None:
    _login(client)
    assert client.post("/api/backups/estimate-size", json={"backup_ids": []}).status_code == 400


def test_estimate_size_caps_batch(
    client: TestClient, main_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    monkeypatch.setattr(main_module, "redis_client", lambda: object())
    ids = [f"main/ds/root|vm/{i}/2026-05-01T00:00:00Z" for i in range(21)]
    res = client.post("/api/backups/estimate-size", json={"backup_ids": ids})
    assert res.status_code == 400


def test_estimate_fidx_usage_cached_hits_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    from pbs_wire import estimate_fidx_usage_cached, fidx_usage_cache_key

    bid = "main/ds/root|vm/100/2026-05-01T00:00:00Z"
    key = fidx_usage_cache_key("main/ds/root", "100", 1777593600)
    payload = {
        "backup_id": bid,
        "nonzero_bytes": 123,
        "virtual_bytes": 456,
        "sparsity_ratio": 0.5,
    }

    class FakeRedis:
        def get(self, k):
            assert k == key
            return json.dumps(payload)

        def set(self, *a, **k):
            raise AssertionError("should not write on cache hit")

    out = estimate_fidx_usage_cached(FakeRedis(), {}, bid)
    assert out["cached"] is True
    assert out["nonzero_bytes"] == 123
