"""Outbound notifications: email (SMTP) and optional webhooks."""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any
from urllib import error, request

log = logging.getLogger("restore-notifications")


def _notify_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("notifications") or {}


def email_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return _notify_cfg(cfg).get("email") or {}


def webhook_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return _notify_cfg(cfg).get("webhook") or {}


def events_enabled(cfg: dict[str, Any]) -> dict[str, bool]:
    ev = _notify_cfg(cfg).get("events") or {}
    return {
        "check_failed": bool(ev.get("check_failed", True)),
        "plan_run_terminal": bool(ev.get("plan_run_terminal", True)),
        "job_failed": bool(ev.get("job_failed", False)),
    }


def email_enabled(cfg: dict[str, Any]) -> bool:
    e = email_cfg(cfg)
    return bool(e.get("enabled")) and bool((e.get("host") or "").strip()) and bool(e.get("to"))


def webhook_enabled(cfg: dict[str, Any]) -> bool:
    w = webhook_cfg(cfg)
    return bool(w.get("enabled")) and bool((w.get("url") or "").strip())


def _parse_recipients(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    parts: list[str] = []
    for chunk in text.replace(";", ",").split(","):
        addr = chunk.strip()
        if addr:
            parts.append(addr)
    return parts


def send_email(
    cfg: dict[str, Any],
    *,
    subject: str,
    body: str,
    to: list[str] | None = None,
) -> tuple[bool, str]:
    """Send a plain-text email via SMTP. Returns (ok, detail)."""
    e = email_cfg(cfg)
    if not bool(e.get("enabled", True)) and to is None:
        # When called from notify_event, enabled is checked upstream.
        pass
    host = str(e.get("host") or "").strip()
    if not host:
        return False, "SMTP host is not configured"
    recipients = to if to is not None else _parse_recipients(e.get("to"))
    if not recipients:
        return False, "No email recipients configured"
    from_addr = str(e.get("from") or e.get("username") or "restore-engine@localhost").strip()
    try:
        port = int(e.get("port") or 587)
    except (TypeError, ValueError):
        port = 587
    use_tls = bool(e.get("tls", True))
    use_ssl = bool(e.get("ssl", False))
    username = str(e.get("username") or "").strip()
    password = str(e.get("password") or "")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    try:
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                if use_tls:
                    context = ssl.create_default_context()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
        return True, f"Sent to {', '.join(recipients)}"
    except Exception as exc:
        log.warning("SMTP send failed: %s", exc)
        return False, str(exc)


def post_webhook(cfg: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, str]:
    w = webhook_cfg(cfg)
    url = str(w.get("url") or "").strip()
    if not url:
        return False, "Webhook URL is not configured"
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "restore-engine/notify"}
    secret = str(w.get("secret") or "").strip()
    if secret:
        headers["X-Restore-Engine-Secret"] = secret
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=15) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            if int(code) >= 400:
                return False, f"HTTP {code}"
            return True, f"HTTP {code}"
    except error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        log.warning("Webhook post failed: %s", exc)
        return False, str(exc)


def notify_event(
    cfg: dict[str, Any],
    *,
    event: str,
    subject: str,
    body: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Best-effort fan-out. Never raises."""
    results: dict[str, Any] = {"event": event, "email": None, "webhook": None}
    try:
        if email_enabled(cfg):
            ok, detail = send_email(cfg, subject=subject, body=body)
            results["email"] = {"ok": ok, "detail": detail}
        if webhook_enabled(cfg):
            payload = {
                "event": event,
                "subject": subject,
                "body": body,
                **(extra or {}),
            }
            ok, detail = post_webhook(cfg, payload)
            results["webhook"] = {"ok": ok, "detail": detail}
    except Exception as exc:
        log.exception("notify_event failed")
        results["error"] = str(exc)
    return results


def notify_check_result(cfg: dict[str, Any], *, plan: dict[str, Any], check: dict[str, Any]) -> None:
    if not events_enabled(cfg).get("check_failed"):
        return
    if bool(check.get("ok")):
        return
    name = plan.get("name") or plan.get("id") or "plan"
    subject = f"[restore-engine] Readiness FAILED — {name}"
    summary = str(check.get("summary") or "Readiness check failed")
    lines = [
        f"Plan: {name}",
        f"Plan ID: {plan.get('id', '')}",
        f"Verification: {plan.get('verification', '')}",
        f"Checked at: {check.get('checked_at', '')}",
        f"Summary: {summary}",
        "",
        "Items:",
    ]
    for item in check.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("level") or "") not in {"error", "warn", "warning"}:
            continue
        lines.append(
            f"- [{item.get('level')}] {item.get('code')}: {item.get('message')} "
            f"{item.get('detail') or ''}".rstrip()
        )
    report_id = plan.get("last_check_report_id") or ""
    if report_id:
        lines.extend(["", f"Report ID: {report_id}"])
    notify_event(
        cfg,
        event="check_failed",
        subject=subject,
        body="\n".join(lines) + "\n",
        extra={"plan_id": plan.get("id"), "report_id": report_id, "ok": False},
    )


def notify_plan_run_terminal(cfg: dict[str, Any], *, run: dict[str, Any]) -> None:
    if not events_enabled(cfg).get("plan_run_terminal"):
        return
    status = str(run.get("status") or "")
    name = run.get("plan_name") or run.get("plan_id") or "plan"
    kind = "drill" if run.get("drill") else "plan run"
    subject = f"[restore-engine] {kind} {status} — {name}"
    lines = [
        f"Kind: {kind}",
        f"Status: {status}",
        f"Plan: {name}",
        f"Run ID: {run.get('id', '')}",
        f"Location: {run.get('location_name') or run.get('location_id') or ''}",
        f"Jobs: {run.get('completed_jobs', 0)}/{run.get('job_count', 0)} completed "
        f"({run.get('failed_jobs', 0)} failed)",
        f"RTO: {run.get('rto') or run.get('rto_sec') or '—'}",
        f"Teardown: {run.get('teardown_status') or '—'}",
        f"Started: {run.get('started_at') or '—'}",
        f"Finished: {run.get('finished_at') or '—'}",
    ]
    if run.get("error"):
        lines.append(f"Error: {run.get('error')}")
    if run.get("report_id"):
        lines.append(f"Report ID: {run.get('report_id')}")
    notify_event(
        cfg,
        event="plan_run_terminal",
        subject=subject,
        body="\n".join(lines) + "\n",
        extra={
            "plan_id": run.get("plan_id"),
            "run_id": run.get("id"),
            "status": status,
            "drill": bool(run.get("drill")),
            "report_id": run.get("report_id"),
        },
    )


def notify_job_failed(cfg: dict[str, Any], *, job: dict[str, Any]) -> None:
    if not events_enabled(cfg).get("job_failed"):
        return
    # Skip plan-run jobs (covered by plan_run_terminal) unless explicitly wanted —
    # still notify; operators can disable the event.
    name = job.get("vm_name") or job.get("backup_id") or job.get("job_id") or "job"
    subject = f"[restore-engine] Restore FAILED — {name}"
    lines = [
        f"Job ID: {job.get('job_id', '')}",
        f"VM: {job.get('vm_name', '')}",
        f"Source VMID: {job.get('source_vmid', '')}",
        f"Target VMID: {job.get('proxmox_vmid', '')}",
        f"Node: {job.get('proxmox_node', '')}",
        f"Mode: {job.get('restore_mode', '')}",
        f"Error: {job.get('error', '')}",
    ]
    if job.get("plan_run_id"):
        lines.append(f"Plan run: {job.get('plan_run_id')}")
    notify_event(
        cfg,
        event="job_failed",
        subject=subject,
        body="\n".join(lines) + "\n",
        extra={"job_id": job.get("job_id"), "plan_run_id": job.get("plan_run_id")},
    )
