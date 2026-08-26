"""Tests for email / webhook notification helpers."""

from __future__ import annotations

from typing import Any

import notifications


CFG_BASE = {
    "notifications": {
        "email": {
            "enabled": True,
            "host": "smtp.example.com",
            "port": 587,
            "tls": True,
            "ssl": False,
            "username": "u",
            "password": "p",
            "from": "re@example.com",
            "to": ["ops@example.com"],
        },
        "webhook": {"enabled": False, "url": "", "secret": ""},
        "events": {"check_failed": True, "plan_run_terminal": True, "job_failed": True},
    }
}


def test_send_email_success(monkeypatch: Any) -> None:
    sent: dict[str, Any] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int = 30) -> None:
            sent["host"] = host
            sent["port"] = port

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def ehlo(self) -> None:
            return None

        def starttls(self, context: Any = None) -> None:
            sent["tls"] = True

        def login(self, user: str, password: str) -> None:
            sent["user"] = user

        def send_message(self, msg: Any) -> None:
            sent["subject"] = msg["Subject"]
            sent["to"] = msg["To"]

    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)
    ok, detail = notifications.send_email(CFG_BASE, subject="hi", body="body")
    assert ok is True
    assert sent["host"] == "smtp.example.com"
    assert "ops@example.com" in sent["to"]


def test_notify_check_only_on_failure(monkeypatch: Any) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        notifications,
        "notify_event",
        lambda *a, **k: calls.append(k.get("event") or ""),
    )
    notifications.notify_check_result(
        CFG_BASE,
        plan={"id": "p1", "name": "P"},
        check={"ok": True, "summary": "ok"},
    )
    assert calls == []
    notifications.notify_check_result(
        CFG_BASE,
        plan={"id": "p1", "name": "P"},
        check={"ok": False, "summary": "bad", "items": []},
    )
    assert calls == ["check_failed"]


def test_notify_plan_run_terminal(monkeypatch: Any) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        notifications,
        "notify_event",
        lambda *a, **k: calls.append(k.get("event") or ""),
    )
    notifications.notify_plan_run_terminal(
        CFG_BASE,
        run={"id": "r1", "plan_name": "P", "status": "COMPLETED", "drill": True},
    )
    assert calls == ["plan_run_terminal"]


def test_post_webhook_success(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class FakeResp:
        status = 200

        def getcode(self) -> int:
            return 200

        def __enter__(self) -> FakeResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def fake_urlopen(req: Any, timeout: int = 15) -> FakeResp:
        captured["url"] = req.full_url
        try:
            captured["headers"] = list(req.header_items())
        except Exception:
            captured["headers"] = [(k, req.get_header(k)) for k in ("X-Restore-Engine-Secret", "Content-Type")]
        captured["data"] = req.data
        return FakeResp()

    monkeypatch.setattr(notifications.request, "urlopen", fake_urlopen)
    cfg = {
        "notifications": {
            "webhook": {
                "enabled": True,
                "url": "https://hooks.example/restore",
                "secret": "s3cret",
            }
        }
    }
    ok, detail = notifications.post_webhook(cfg, {"event": "job_failed", "x": 1})
    assert ok is True
    assert "200" in detail
    assert captured["url"] == "https://hooks.example/restore"
    secret_hdr = None
    for k, v in captured["headers"]:
        if str(k).lower() == "x-restore-engine-secret":
            secret_hdr = v
            break
    assert secret_hdr == "s3cret"


def test_post_webhook_missing_url() -> None:
    ok, detail = notifications.post_webhook(
        {"notifications": {"webhook": {"url": "", "secret": ""}}},
        {"event": "x"},
    )
    assert ok is False
    assert "not configured" in detail.lower()


def test_post_webhook_http_error_soft(monkeypatch: Any) -> None:
    from urllib import error as urlerror

    def boom(req: Any, timeout: int = 15) -> Any:
        raise urlerror.HTTPError(
            "https://hooks.example/restore",
            500,
            "Server Error",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

    monkeypatch.setattr(notifications.request, "urlopen", boom)
    cfg = {
        "notifications": {
            "webhook": {"enabled": True, "url": "https://hooks.example/restore", "secret": ""}
        }
    }
    ok, detail = notifications.post_webhook(cfg, {"event": "check_failed"})
    assert ok is False
    assert "500" in detail
