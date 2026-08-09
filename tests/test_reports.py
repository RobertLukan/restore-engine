"""Tests for compliance-lite reports."""

from __future__ import annotations

from datetime import datetime, timezone

import plans
import reports
from tests.test_plans import CFG, FakeRedis


def test_render_check_and_run_reports() -> None:
    plan = {"id": "p1", "name": "App", "verification": "VERIFIED"}
    check = {
        "ok": True,
        "checked_at": "2026-08-07T12:00:00+00:00",
        "cutoff": "9999-12-31T23:59:59Z",
        "summary": "Readiness OK (1 VM(s))",
        "member_count": 1,
        "items": [{"level": "ok", "code": "pbs.connectivity", "message": "PBS ok"}],
    }
    rendered = reports.render_check_report(plan=plan, check=check)
    assert "PASSED" in rendered["markdown"]
    assert "Readiness OK" in rendered["html"]

    run = {
        "id": "r1",
        "plan_id": "p1",
        "plan_name": "App",
        "status": "COMPLETED",
        "location_name": "lab",
        "nodes": ["pve"],
        "storage": "local-lvm",
        "restore_mode": "normal",
        "cutoff": "9999-12-31T23:59:59Z",
        "started_at": "2026-08-07T12:00:00+00:00",
        "finished_at": "2026-08-07T12:05:30+00:00",
        "job_count": 1,
        "completed_jobs": 1,
        "failed_jobs": 0,
        "jobs": [
            {
                "group_index": 0,
                "vm_name": "web",
                "source_vmid": 109,
                "proxmox_vmid": 3500,
                "state": "COMPLETED",
                "archive": "pbs:backup/vm/109/x",
                "error": "",
            }
        ],
    }
    rendered_run = reports.render_run_report(plan=plan, run=run)
    assert "5m 30s" in rendered_run["markdown"]
    assert "Wall-clock RTO" in rendered_run["html"]
    assert reports.wall_clock_rto_sec(run["started_at"], run["finished_at"]) == 330


def test_report_retention_trims_oldest() -> None:
    r = FakeRedis()
    cfg = {
        **CFG,
        "worker": {"report_retain": 2},
        "redis": {**CFG["redis"], "report_retain": 2},
    }
    plan = {"id": "p1", "name": "App", "verification": "VERIFIED"}
    check = {
        "ok": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "cutoff": "9999-12-31T23:59:59Z",
        "summary": "ok",
        "member_count": 0,
        "items": [],
    }
    a = reports.save_check_report(r, cfg, plan=plan, check=check)
    b = reports.save_check_report(r, cfg, plan=plan, check=check)
    c = reports.save_check_report(r, cfg, plan=plan, check=check)
    listed = reports.list_reports(r, cfg, limit=10)
    assert len(listed) == 2
    ids = {row["id"] for row in listed}
    assert c["id"] in ids
    assert b["id"] in ids
    assert a["id"] not in ids
    assert reports.get_report(r, cfg, a["id"]) is None


def test_apply_check_result_stores_report() -> None:
    r = FakeRedis()
    group = plans.create_group(r, CFG, {"name": "g", "vmids": [1], "tags": []})
    loc = plans.create_location(
        r, CFG, {"name": "l", "node": "pve", "storage": "local-lvm", "vmid_start": 100}
    )
    plan = plans.create_plan(
        r, CFG, {"name": "P", "group_ids": [group["id"]], "location_id": loc["id"]}
    )
    check = {
        "ok": False,
        "checked_at": "2026-08-07T12:00:00+00:00",
        "cutoff": "9999-12-31T23:59:59Z",
        "summary": "failed",
        "member_count": 0,
        "items": [{"level": "error", "code": "x", "message": "nope"}],
    }
    updated = plans.apply_check_result(r, CFG, plan, check)
    assert updated["verification"] == "NOT_VERIFIED"
    assert updated.get("last_check_report_id")
    rep = reports.get_report(r, CFG, updated["last_check_report_id"])
    assert rep and rep["kind"] == "check"
    assert "FAILED" in rep["markdown"]


def test_drill_run_report_tagged() -> None:
    rendered = reports.render_run_report(
        plan={"name": "Demo", "id": "p1"},
        run={
            "id": "r1",
            "plan_id": "p1",
            "status": "COMPLETED",
            "drill": True,
            "powered_off": True,
            "auto_teardown": True,
            "teardown_status": "completed",
            "started_at": "2026-08-07T12:00:00+00:00",
            "finished_at": "2026-08-07T12:05:00+00:00",
            "jobs": [],
            "job_count": 0,
            "completed_jobs": 0,
            "failed_jobs": 0,
        },
    )
    assert rendered["title"].startswith("Drill run")
    assert "**Kind:** drill" in rendered["markdown"]
    assert "drill" in rendered["html"]

    r = FakeRedis()
    group = plans.create_group(r, CFG, {"name": "g", "vmids": [1], "tags": []})
    loc = plans.create_location(
        r, CFG, {"name": "l", "node": "pve", "storage": "local-lvm", "vmid_start": 100}
    )
    plan = plans.create_plan(
        r, CFG, {"name": "P", "group_ids": [group["id"]], "location_id": loc["id"]}
    )
    plan = plans.apply_check_result(
        r,
        CFG,
        plan,
        {
            "ok": True,
            "checked_at": "2026-08-07T11:00:00+00:00",
            "cutoff": "x",
            "summary": "ok",
            "member_count": 1,
            "items": [],
        },
    )
    run = {
        "id": "run-1",
        "plan_id": plan["id"],
        "plan_name": plan["name"],
        "status": "COMPLETED",
        "started_at": "2026-08-07T12:00:00+00:00",
        "finished_at": "2026-08-07T12:10:00+00:00",
        "job_ids_by_group": [],
        "group_ids": [group["id"]],
    }
    plans.save_plan_run(r, CFG, run)
    dash = reports.compliance_dashboard(
        r,
        CFG,
        list_plans_fn=plans.list_plans,
        list_plan_runs_fn=plans.list_plan_runs,
    )
    assert len(dash["plans"]) == 1
    row = dash["plans"][0]
    assert row["verification"] == "VERIFIED"
    assert row["last_run_rto_sec"] == 600
    assert row["last_run_rto"] == "10m 0s"
    assert row["last_prod_run_rto_sec"] == 600
    assert "counts" in dash
    assert dash["counts"]["verification"]["VERIFIED"] == 1
    assert "assurance_policy_off" in row["risks"]


def test_compliance_dashboard_separates_drill_and_prod() -> None:
    r = FakeRedis()
    group = plans.create_group(r, CFG, {"name": "g", "vmids": [1]})
    loc = plans.create_location(
        r, CFG, {"name": "l", "node": "pve", "storage": "local-lvm", "vmid_start": 200}
    )
    plan = plans.create_plan(
        r,
        CFG,
        {
            "name": "P",
            "group_ids": [group["id"]],
            "location_id": loc["id"],
            "assurance_enabled": True,
            "assurance_status": "ASSURED",
        },
    )
    plans.save_plan_run(
        r,
        CFG,
        {
            "id": "drill-1",
            "plan_id": plan["id"],
            "plan_name": plan["name"],
            "status": "COMPLETED",
            "drill": True,
            "teardown_status": "completed",
            "started_at": "2026-08-07T11:00:00+00:00",
            "finished_at": "2026-08-07T11:05:00+00:00",
            "job_ids_by_group": [],
            "group_ids": [group["id"]],
        },
    )
    plans.save_plan_run(
        r,
        CFG,
        {
            "id": "prod-1",
            "plan_id": plan["id"],
            "plan_name": plan["name"],
            "status": "COMPLETED",
            "drill": False,
            "started_at": "2026-08-07T12:00:00+00:00",
            "finished_at": "2026-08-07T12:20:00+00:00",
            "job_ids_by_group": [],
            "group_ids": [group["id"]],
        },
    )
    dash = reports.compliance_dashboard(
        r,
        CFG,
        list_plans_fn=plans.list_plans,
        list_plan_runs_fn=plans.list_plan_runs,
    )
    row = dash["plans"][0]
    assert row["last_prod_run_rto_sec"] == 1200
    assert row["last_drill_rto_sec"] == 300
    assert row["assurance_status"] == "ASSURED"
    assert dash["counts"]["assurance"]["ASSURED"] == 1
    assert "assurance_policy_off" not in row["risks"]
