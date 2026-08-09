"""Append-only audit log in Redis for operator actions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import redis


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit_key(cfg: dict[str, Any]) -> str:
    return str((cfg.get("redis") or {}).get("audit_key") or "restore:audit")


def _audit_retain(cfg: dict[str, Any]) -> int:
    try:
        return max(100, int((cfg.get("worker") or {}).get("audit_retain", 2000) or 2000))
    except (TypeError, ValueError):
        return 2000


def append_audit(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    action: str,
    actor: str = "ui",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = {
        "ts": utc_now_iso(),
        "action": str(action or "").strip() or "unknown",
        "actor": str(actor or "ui").strip() or "ui",
        "detail": detail or {},
    }
    key = _audit_key(cfg)
    r.lpush(key, json.dumps(entry, separators=(",", ":"), sort_keys=True))
    r.ltrim(key, 0, _audit_retain(cfg) - 1)
    return entry


def list_audit(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    raw = r.lrange(_audit_key(cfg), offset, offset + limit - 1) or []
    out: list[dict[str, Any]] = []
    for line in raw:
        try:
            out.append(json.loads(line))
        except (TypeError, json.JSONDecodeError):
            continue
    return out
