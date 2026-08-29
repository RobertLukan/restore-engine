"""Compliance-lite reports for readiness checks and plan runs."""

from __future__ import annotations

import html
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import redis

from states import PlanRunStatus


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_bytes(n: int | float | None) -> str:
    from plans import _format_bytes as fmt

    return fmt(n)


def _check_size_lines(check: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (markdown bullets, html <li> fragments) for size_summary if present."""
    ss = check.get("size_summary")
    if not isinstance(ss, dict):
        return [], []
    gross = _format_bytes(ss.get("gross_bytes"))
    est_n = int(ss.get("nonzero_estimated_count") or 0)
    miss_n = int(ss.get("nonzero_missing_count") or 0)
    nz = ss.get("nonzero_bytes")
    if est_n > 0 and nz is not None:
        net = f"~{_format_bytes(int(nz))} approx net ({est_n}/{est_n + miss_n} estimated)"
    else:
        net = "approx net unavailable"
    md = [
        f"- **Size (gross):** {gross}",
        f"- **Size (approx net):** {net}",
    ]
    html_lis = [
        f"<li>Size (gross): {html.escape(gross)}</li>",
        f"<li>Size (approx net): {html.escape(net)}</li>",
    ]
    return md, html_lis


def _redis_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("redis") or {}


def report_key(cfg: dict[str, Any], report_id: str) -> str:
    prefix = _redis_cfg(cfg).get("report_key_prefix", "restore:report:")
    return f"{prefix}{report_id}"


def reports_index(cfg: dict[str, Any]) -> str:
    return _redis_cfg(cfg).get("reports_index", "restore:reports")


def report_retain_limit(cfg: dict[str, Any]) -> int:
    worker = cfg.get("worker") or {}
    try:
        n = int(worker.get("report_retain", _redis_cfg(cfg).get("report_retain", 20)))
    except (TypeError, ValueError):
        n = 20
    return max(1, n)


def _parse_iso(ts: str) -> datetime | None:
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def wall_clock_rto_sec(started_at: str, finished_at: str) -> int | None:
    start = _parse_iso(started_at)
    end = _parse_iso(finished_at)
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds()))


def format_duration(sec: int | None) -> str:
    if sec is None:
        return "—"
    if sec < 60:
        return f"{sec}s"
    mins, s = divmod(sec, 60)
    if mins < 60:
        return f"{mins}m {s}s"
    hours, m = divmod(mins, 60)
    return f"{hours}h {m}m {s}s"


def _md_escape(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def _html_page(title: str, body_inner: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"/>"
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "body{font-family:ui-sans-serif,system-ui,sans-serif;margin:2rem;line-height:1.45;color:#111}"
        "h1{font-size:1.35rem}h2{font-size:1.1rem;margin-top:1.5rem}"
        "table{border-collapse:collapse;width:100%;font-size:0.9rem}"
        "th,td{border:1px solid #ccc;padding:0.4rem 0.55rem;text-align:left}"
        "th{background:#f4f4f4}.ok{color:#0a7} .err{color:#c33} .muted{color:#666}"
        "pre{background:#f7f7f7;padding:0.75rem;overflow:auto}"
        "</style></head><body>"
        f"{body_inner}</body></html>"
    )


def render_check_report(*, plan: dict[str, Any], check: dict[str, Any]) -> dict[str, str]:
    """Return ``{title, markdown, html}`` for a readiness check."""
    name = plan.get("name") or plan.get("id") or "plan"
    title = f"Readiness check — {name}"
    ok = bool(check.get("ok"))
    status = "PASSED" if ok else "FAILED"
    size_md, size_html = _check_size_lines(check)
    lines = [
        f"# {title}",
        "",
        f"- **Status:** {status}",
        f"- **Plan ID:** `{plan.get('id', '')}`",
        f"- **Verification:** `{plan.get('verification', '')}`",
        f"- **Checked at:** {check.get('checked_at') or '—'}",
        f"- **Cutoff:** `{check.get('cutoff') or '—'}`",
        f"- **Members resolved:** {check.get('member_count', 0)}",
        *size_md,
        f"- **Summary:** {_md_escape(str(check.get('summary') or ''))}",
        "",
        "## Check items",
        "",
        "| Level | Code | Message | Detail |",
        "| --- | --- | --- | --- |",
    ]
    html_rows = []
    for item in check.get("items") or []:
        level = str(item.get("level") or "")
        code = str(item.get("code") or "")
        msg = str(item.get("message") or "")
        detail = str(item.get("detail") or "")
        lines.append(
            f"| {_md_escape(level)} | `{_md_escape(code)}` | {_md_escape(msg)} | {_md_escape(detail)} |"
        )
        cls = "err" if level == "error" else ("ok" if level == "ok" else "muted")
        html_rows.append(
            "<tr>"
            f"<td class=\"{cls}\">{html.escape(level)}</td>"
            f"<td><code>{html.escape(code)}</code></td>"
            f"<td>{html.escape(msg)}</td>"
            f"<td class=\"muted\">{html.escape(detail)}</td>"
            "</tr>"
        )
    markdown = "\n".join(lines) + "\n"
    body = (
        f"<h1>{html.escape(title)}</h1>"
        f"<p><strong>Status:</strong> "
        f"<span class=\"{'ok' if ok else 'err'}\">{html.escape(status)}</span></p>"
        "<ul>"
        f"<li>Plan ID: <code>{html.escape(str(plan.get('id') or ''))}</code></li>"
        f"<li>Verification: <code>{html.escape(str(plan.get('verification') or ''))}</code></li>"
        f"<li>Checked at: {html.escape(str(check.get('checked_at') or '—'))}</li>"
        f"<li>Cutoff: <code>{html.escape(str(check.get('cutoff') or '—'))}</code></li>"
        f"<li>Members resolved: {int(check.get('member_count') or 0)}</li>"
        f"{''.join(size_html)}"
        f"<li>Summary: {html.escape(str(check.get('summary') or ''))}</li>"
        "</ul>"
        "<h2>Check items</h2>"
        "<table><thead><tr><th>Level</th><th>Code</th><th>Message</th><th>Detail</th></tr></thead>"
        f"<tbody>{''.join(html_rows) or '<tr><td colspan=\"4\" class=\"muted\">No items</td></tr>'}</tbody></table>"
    )
    return {"title": title, "markdown": markdown, "html": _html_page(title, body)}


def render_run_report(*, plan: dict[str, Any] | None, run: dict[str, Any]) -> dict[str, str]:
    """Return ``{title, markdown, html}`` for a plan run (expects aggregated jobs)."""
    name = (plan or {}).get("name") or run.get("plan_name") or run.get("plan_id") or "plan"
    is_drill = bool(run.get("drill"))
    title = f"Drill run — {name}" if is_drill else f"Plan run — {name}"
    status = str(run.get("status") or "")
    rto = wall_clock_rto_sec(str(run.get("started_at") or ""), str(run.get("finished_at") or ""))
    lines = [
        f"# {title}",
        "",
        f"- **Status:** {status}",
        f"- **Kind:** {'drill' if is_drill else 'recovery'}",
        f"- **Run ID:** `{run.get('id', '')}`",
        f"- **Plan ID:** `{run.get('plan_id', '')}`",
        f"- **Location:** {_md_escape(str(run.get('location_name') or run.get('location_id') or ''))}",
        f"- **Nodes:** {_md_escape(', '.join(str(n) for n in (run.get('nodes') or ([run.get('node')] if run.get('node') else [])) if n))}",
        f"- **Storage:** `{_md_escape(str(run.get('storage') or ''))}`",
        f"- **Restore mode:** `{run.get('restore_mode') or 'normal'}`",
        f"- **Powered off:** {'yes' if run.get('powered_off') or (is_drill and not run.get('power_on')) else ('yes' if not run.get('live_restore') and not run.get('power_on') else 'no')}",
        f"- **Power on:** {'yes' if run.get('power_on') else 'no'}",
        f"- **QGA wait:** {int(run.get('qga_wait_sec') or 0)}s",
        f"- **Live restore:** {'yes' if run.get('live_restore') else 'no'}",
        f"- **Teardown:** {_md_escape(str(run.get('teardown_status') or ('auto' if run.get('auto_teardown') else '—')))}",
        f"- **Cutoff:** `{run.get('cutoff') or '—'}`",
        f"- **Started:** {run.get('started_at') or '—'}",
        f"- **Finished:** {run.get('finished_at') or '—'}",
        f"- **Wall-clock RTO:** {format_duration(rto)}",
        f"- **Jobs:** {run.get('completed_jobs', 0)}/{run.get('job_count', 0)} completed"
        f" ({run.get('failed_jobs', 0)} failed)",
    ]
    if run.get("error"):
        lines.append(f"- **Error:** {_md_escape(str(run.get('error')))}")
    lines.extend(
        [
            "",
            "## Jobs",
            "",
            "| Group | VM | Source VMID | Target VMID | State | Archive / volid | Error |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    html_rows = []
    for job in run.get("jobs") or []:
        archive = str(job.get("archive") or job.get("backup_id") or "")
        lines.append(
            "| {g} | {vm} | {src} | {tgt} | {st} | `{arc}` | {err} |".format(
                g=job.get("group_index", ""),
                vm=_md_escape(str(job.get("vm_name") or "")),
                src=job.get("source_vmid", ""),
                tgt=job.get("proxmox_vmid", ""),
                st=_md_escape(str(job.get("state") or "")),
                arc=_md_escape(archive),
                err=_md_escape(str(job.get("error") or "")),
            )
        )
        st = str(job.get("state") or "")
        cls = "ok" if st == "COMPLETED" else ("err" if st == "FAILED" else "muted")
        html_rows.append(
            "<tr>"
            f"<td>{html.escape(str(job.get('group_index', '')))}</td>"
            f"<td>{html.escape(str(job.get('vm_name') or ''))}</td>"
            f"<td>{html.escape(str(job.get('source_vmid') or ''))}</td>"
            f"<td>{html.escape(str(job.get('proxmox_vmid') or ''))}</td>"
            f"<td class=\"{cls}\">{html.escape(st)}</td>"
            f"<td><code>{html.escape(archive)}</code></td>"
            f"<td class=\"err\">{html.escape(str(job.get('error') or ''))}</td>"
            "</tr>"
        )
    markdown = "\n".join(lines) + "\n"
    nodes = ", ".join(
        str(n) for n in (run.get("nodes") or ([run.get("node")] if run.get("node") else [])) if n
    )
    err_li = (
        f"<li class=\"err\">Error: {html.escape(str(run.get('error')))}</li>"
        if run.get("error")
        else ""
    )
    body = (
        f"<h1>{html.escape(title)}</h1>"
        f"<p><strong>Status:</strong> {html.escape(status)}</p>"
        "<ul>"
        f"<li>Run ID: <code>{html.escape(str(run.get('id') or ''))}</code></li>"
        f"<li>Kind: <code>{'drill' if is_drill else 'recovery'}</code></li>"
        f"<li>Plan ID: <code>{html.escape(str(run.get('plan_id') or ''))}</code></li>"
        f"<li>Location: {html.escape(str(run.get('location_name') or run.get('location_id') or ''))}</li>"
        f"<li>Nodes: {html.escape(nodes)}</li>"
        f"<li>Storage: <code>{html.escape(str(run.get('storage') or ''))}</code></li>"
        f"<li>Restore mode: <code>{html.escape(str(run.get('restore_mode') or 'normal'))}</code></li>"
        f"<li>Powered off: {'yes' if run.get('powered_off') or is_drill or not run.get('live_restore') else 'no'}</li>"
        f"<li>Teardown: {html.escape(str(run.get('teardown_status') or ('auto' if run.get('auto_teardown') else '—')))}</li>"
        f"<li>Cutoff: <code>{html.escape(str(run.get('cutoff') or '—'))}</code></li>"
        f"<li>Started: {html.escape(str(run.get('started_at') or '—'))}</li>"
        f"<li>Finished: {html.escape(str(run.get('finished_at') or '—'))}</li>"
        f"<li>Wall-clock RTO: <strong>{html.escape(format_duration(rto))}</strong></li>"
        f"<li>Jobs: {int(run.get('completed_jobs') or 0)}/{int(run.get('job_count') or 0)} completed "
        f"({int(run.get('failed_jobs') or 0)} failed)</li>"
        f"{err_li}"
        "</ul>"
        "<h2>Jobs</h2>"
        "<table><thead><tr>"
        "<th>Group</th><th>VM</th><th>Source</th><th>Target</th><th>State</th><th>Archive</th><th>Error</th>"
        "</tr></thead>"
        f"<tbody>{''.join(html_rows) or '<tr><td colspan=\"7\" class=\"muted\">No jobs</td></tr>'}</tbody></table>"
    )
    return {"title": title, "markdown": markdown, "html": _html_page(title, body)}


def _dump(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def _load(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return json.loads(raw)


def save_report(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    kind: str,
    plan_id: str,
    plan_name: str,
    rendered: dict[str, str],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a report and trim to retain limit (global, newest kept)."""
    report_id = str(uuid.uuid4())
    now = utc_now_iso()
    record = {
        "id": report_id,
        "kind": kind,
        "plan_id": plan_id,
        "plan_name": plan_name,
        "title": rendered.get("title") or "",
        "created_at": now,
        "markdown": rendered.get("markdown") or "",
        "html": rendered.get("html") or "",
        "meta": meta or {},
    }
    key = report_key(cfg, report_id)
    index = reports_index(cfg)
    pipe = r.pipeline(transaction=True)
    pipe.set(key, _dump(record))
    score = datetime.now(timezone.utc).timestamp()
    pipe.zadd(index, {report_id: score})
    pipe.execute()
    _trim_reports(r, cfg)
    return record


def _trim_reports(r: redis.Redis, cfg: dict[str, Any]) -> None:
    index = reports_index(cfg)
    limit = report_retain_limit(cfg)
    try:
        total = int(r.zcard(index) or 0)
    except Exception:
        return
    if total <= limit:
        return
    remove_count = total - limit
    oldest = r.zrange(index, 0, remove_count - 1) or []
    if not oldest:
        return
    pipe = r.pipeline(transaction=True)
    for rid in oldest:
        pipe.delete(report_key(cfg, rid))
        pipe.zrem(index, rid)
    pipe.execute()


def get_report(r: redis.Redis, cfg: dict[str, Any], report_id: str) -> dict[str, Any] | None:
    return _load(r.get(report_key(cfg, report_id)))


def list_reports(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    plan_id: str | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    index = reports_index(cfg)
    limit = max(1, min(200, int(limit)))
    try:
        ids = list(r.zrevrange(index, 0, max(limit * 4, limit) - 1) or [])
    except Exception:
        ids = sorted(r.smembers(index) or [], reverse=True)
    out: list[dict[str, Any]] = []
    for rid in ids:
        data = get_report(r, cfg, rid)
        if not data:
            continue
        if plan_id and data.get("plan_id") != plan_id:
            continue
        if kind and data.get("kind") != kind:
            continue
        out.append(
            {
                "id": data["id"],
                "kind": data.get("kind"),
                "plan_id": data.get("plan_id"),
                "plan_name": data.get("plan_name"),
                "title": data.get("title"),
                "created_at": data.get("created_at"),
                "meta": data.get("meta") or {},
            }
        )
        if len(out) >= limit:
            break
    return out


def save_check_report(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    plan: dict[str, Any],
    check: dict[str, Any],
) -> dict[str, Any]:
    rendered = render_check_report(plan=plan, check=check)
    return save_report(
        r,
        cfg,
        kind="check",
        plan_id=str(plan.get("id") or ""),
        plan_name=str(plan.get("name") or ""),
        rendered=rendered,
        meta={
            "ok": bool(check.get("ok")),
            "verification": plan.get("verification"),
            "checked_at": check.get("checked_at"),
            "member_count": check.get("member_count", 0),
            "summary": check.get("summary"),
            "size_summary": check.get("size_summary"),
        },
    )


def save_run_report(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    plan: dict[str, Any] | None,
    run: dict[str, Any],
) -> dict[str, Any]:
    rendered = render_run_report(plan=plan, run=run)
    rto = wall_clock_rto_sec(str(run.get("started_at") or ""), str(run.get("finished_at") or ""))
    return save_report(
        r,
        cfg,
        kind="run",
        plan_id=str(run.get("plan_id") or (plan or {}).get("id") or ""),
        plan_name=str((plan or {}).get("name") or run.get("plan_name") or ""),
        rendered=rendered,
        meta={
            "run_id": run.get("id"),
            "status": run.get("status"),
            "rto_sec": rto,
            "job_count": run.get("job_count", 0),
            "completed_jobs": run.get("completed_jobs", 0),
            "failed_jobs": run.get("failed_jobs", 0),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "drill": bool(run.get("drill")),
            "powered_off": bool(run.get("powered_off")),
            "teardown_status": run.get("teardown_status") or "",
        },
    )


def compliance_dashboard(
    r: redis.Redis,
    cfg: dict[str, Any],
    *,
    list_plans_fn: Callable[..., list[dict[str, Any]]],
    list_plan_runs_fn: Callable[..., list[dict[str, Any]]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Cross-plan posture rollup: readiness + assurance + schedule + evidence links."""
    import plans as plans_module
    from states import PlanAssurance, PlanVerification

    clock = now or datetime.now(timezone.utc)
    plans = list_plans_fn(r, cfg)
    require_verified = plans_module.require_verified_to_run(cfg)

    active_by_plan: dict[str, dict[str, Any]] = {}
    try:
        for rid in r.smembers(plans_module.active_plan_runs_key(cfg)) or []:
            run = plans_module.get_plan_run(r, cfg, str(rid))
            if not run or run.get("status") != PlanRunStatus.RUNNING.value:
                continue
            if not run.get("drill"):
                continue
            pid = str(run.get("plan_id") or "")
            if pid:
                active_by_plan[pid] = run
    except Exception:
        active_by_plan = {}

    verification_counts = {
        PlanVerification.VERIFIED.value: 0,
        PlanVerification.NEEDS_VERIFIED.value: 0,
        PlanVerification.NOT_VERIFIED.value: 0,
    }
    assurance_counts = {
        "ASSURED": 0,
        "FAILED": 0,
        "UNKNOWN": 0,
        "IN_PROGRESS": 0,
        "disabled": 0,
    }
    schedule_counts = {"enabled": 0, "overdue": 0, "due_soon": 0}
    disabled_plans = 0

    items: list[dict[str, Any]] = []
    for plan in plans:
        pid = str(plan.get("id") or "")
        enabled = bool(plan.get("enabled", True))
        if not enabled:
            disabled_plans += 1

        ver = str(plan.get("verification") or PlanVerification.NOT_VERIFIED.value)
        if ver not in verification_counts:
            ver = PlanVerification.NOT_VERIFIED.value
        if enabled:
            verification_counts[ver] = verification_counts.get(ver, 0) + 1

        runs = list_plan_runs_fn(r, cfg, plan_id=pid, limit=20)
        last_any: dict[str, Any] | None = None
        last_prod: dict[str, Any] | None = None
        last_drill: dict[str, Any] | None = None
        for run in runs:
            if run.get("status") not in {
                PlanRunStatus.COMPLETED.value,
                PlanRunStatus.FAILED.value,
                PlanRunStatus.CANCELLED.value,
            } or not run.get("finished_at"):
                continue
            if last_any is None:
                last_any = run
            if run.get("drill"):
                if last_drill is None:
                    last_drill = run
            elif last_prod is None:
                last_prod = run
            if last_prod is not None and last_drill is not None and last_any is not None:
                break

        def _rto(run: dict[str, Any] | None) -> int | None:
            if not run:
                return None
            return wall_clock_rto_sec(
                str(run.get("started_at") or ""), str(run.get("finished_at") or "")
            )

        check = plan.get("last_check") if isinstance(plan.get("last_check"), dict) else {}
        active = active_by_plan.get(pid)
        assurance_enabled = bool(plan.get("assurance_enabled", False))
        if not assurance_enabled:
            assurance_counts["disabled"] += 1
            assurance_status = "DISABLED"
            assurance_detail = str(plan.get("assurance_detail") or "")
        elif active:
            assurance_counts["IN_PROGRESS"] += 1
            assurance_status = PlanAssurance.IN_PROGRESS.value
            assurance_detail = f"assurance drill running ({active.get('id')})"
        else:
            assurance_status = str(plan.get("assurance_status") or PlanAssurance.UNKNOWN.value)
            if assurance_status not in {"ASSURED", "FAILED", "UNKNOWN"}:
                assurance_status = PlanAssurance.UNKNOWN.value
            assurance_counts[assurance_status] = assurance_counts.get(assurance_status, 0) + 1
            assurance_detail = str(plan.get("assurance_detail") or "")

        try:
            assured_rto = int(plan.get("assurance_last_rto_sec")) if plan.get("assurance_last_rto_sec") not in (None, "") else None
        except (TypeError, ValueError):
            assured_rto = None

        next_sched = plans_module.next_scheduled_iso(plan, now=clock)
        schedule_enabled = bool(plan.get("schedule_enabled", False))
        schedule_overdue = False
        schedule_due_soon = False
        if schedule_enabled and enabled:
            schedule_counts["enabled"] += 1
            if next_sched:
                try:
                    nxt = datetime.fromisoformat(next_sched.replace("Z", "+00:00"))
                    if nxt.tzinfo is None:
                        nxt = nxt.replace(tzinfo=timezone.utc)
                    delta = (nxt - clock).total_seconds()
                    if delta <= 0:
                        schedule_overdue = True
                        schedule_counts["overdue"] += 1
                    elif delta <= 3600:
                        schedule_due_soon = True
                        schedule_counts["due_soon"] += 1
                except ValueError:
                    pass
            else:
                # No next computed but schedule on → treat as due.
                schedule_overdue = True
                schedule_counts["overdue"] += 1

        uses_tag_groups = False
        try:
            for gid in plan.get("group_ids") or []:
                group = plans_module.get_group(r, cfg, str(gid))
                if group and group.get("tags"):
                    uses_tag_groups = True
                    break
        except Exception:
            uses_tag_groups = False

        risks: list[str] = []
        if not assurance_enabled:
            risks.append("assurance_policy_off")
        if ver != PlanVerification.VERIFIED.value:
            risks.append("not_verified")
        if require_verified and ver != PlanVerification.VERIFIED.value:
            risks.append("verify_gate_blocks_run")
        if active:
            risks.append("drill_in_progress")
        tear = str((last_drill or {}).get("teardown_status") or "")
        if last_drill and tear and tear not in {"", "completed", "skipped"}:
            risks.append("teardown_failed")
        if schedule_overdue:
            risks.append("schedule_overdue")
        if uses_tag_groups and schedule_enabled:
            risks.append("uses_tag_groups")

        prod_rto = _rto(last_prod)
        drill_rto = _rto(last_drill)
        items.append(
            {
                "plan_id": plan.get("id"),
                "plan_name": plan.get("name"),
                "enabled": enabled,
                "verification": ver,
                "last_check_at": plan.get("last_check_at") or "",
                "last_check_ok": bool(check.get("ok")) if check else None,
                "last_check_summary": check.get("summary") or "",
                "last_check_report_id": plan.get("last_check_report_id") or "",
                "assurance_enabled": assurance_enabled,
                "assurance_status": assurance_status,
                "assurance_detail": assurance_detail,
                "assurance_updated_at": plan.get("assurance_updated_at") or "",
                "assurance_require_qga": bool(plan.get("assurance_require_qga", False)),
                "assurance_require_http": bool(plan.get("assurance_require_http", False)),
                "assurance_max_rto_sec": int(plan.get("assurance_max_rto_sec") or 0),
                "assurance_last_rto_sec": assured_rto,
                "assurance_last_rto": format_duration(assured_rto),
                "last_run_id": (last_any or {}).get("id") or "",
                "last_run_status": (last_any or {}).get("status") or "",
                "last_run_finished_at": (last_any or {}).get("finished_at") or "",
                "last_run_rto_sec": _rto(last_any),
                "last_run_rto": format_duration(_rto(last_any)),
                "last_run_report_id": plan.get("last_run_report_id") or "",
                "last_prod_run_id": (last_prod or {}).get("id") or "",
                "last_prod_run_status": (last_prod or {}).get("status") or "",
                "last_prod_run_finished_at": (last_prod or {}).get("finished_at") or "",
                "last_prod_run_rto_sec": prod_rto,
                "last_prod_run_rto": format_duration(prod_rto),
                "last_drill_id": (last_drill or {}).get("id") or "",
                "last_drill_status": (last_drill or {}).get("status") or "",
                "last_drill_finished_at": (last_drill or {}).get("finished_at") or "",
                "last_drill_rto_sec": drill_rto,
                "last_drill_rto": format_duration(drill_rto),
                "last_drill_teardown_status": (last_drill or {}).get("teardown_status") or "",
                "schedule_enabled": schedule_enabled,
                "schedule_interval_hours": int(plan.get("schedule_interval_hours") or 0),
                "next_scheduled_at": next_sched,
                "last_scheduled_run_at": plan.get("last_scheduled_run_at") or "",
                "schedule_overdue": schedule_overdue,
                "schedule_due_soon": schedule_due_soon,
                "uses_tag_groups": uses_tag_groups,
                "active_run_id": (active or {}).get("id") or "",
                "risks": risks,
                "location_id": plan.get("location_id") or "",
            }
        )

    return {
        "plans": items,
        "counts": {
            "verification": verification_counts,
            "assurance": assurance_counts,
            "schedule": schedule_counts,
            "disabled_plans": disabled_plans,
            "require_verified_to_run": require_verified,
        },
        "generated_at": utc_now_iso(),
    }
