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
