"""Tests for Redis audit log helpers."""

from __future__ import annotations

from typing import Any

import audit
from tests.test_plans import FakeRedis


CFG = {
    "redis": {"audit_key": "restore:test:audit"},
    "worker": {"audit_retain": 3},
}


def test_append_and_list_audit() -> None:
    r = FakeRedis()
    e1 = audit.append_audit(r, CFG, action="restore.enqueue", actor="ui", detail={"n": 1})
    e2 = audit.append_audit(r, CFG, action="plan.run", actor="token:ops", detail={"plan_id": "p1"})
    assert e1["action"] == "restore.enqueue"
    assert e2["actor"] == "token:ops"

    items = audit.list_audit(r, CFG, limit=10)
    assert len(items) == 2
    # Newest first (lpush).
    assert items[0]["action"] == "plan.run"
    assert items[1]["action"] == "restore.enqueue"


def test_audit_retain_trims(monkeypatch: Any) -> None:
    r = FakeRedis()
    monkeypatch.setattr(audit, "_audit_retain", lambda _cfg: 3)
    for i in range(5):
        audit.append_audit(r, CFG, action=f"act.{i}", detail={"i": i})
    assert r.llen(CFG["redis"]["audit_key"]) == 3
    items = audit.list_audit(r, CFG, limit=10)
    assert [x["action"] for x in items] == ["act.4", "act.3", "act.2"]
