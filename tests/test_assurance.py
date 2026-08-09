"""Assurance policy evaluation and dashboard."""

from __future__ import annotations

from typing import Any

from plans import assurance_dashboard, evaluate_assurance_policy
from states import PlanAssurance, PlanRunStatus
from tests.test_plans import CFG, FakeRedis


def _plan(**kwargs: Any) -> dict[str, Any]:
    base = {
        "id": "p1",
        "name": "Plan",
        "assurance_enabled": True,
        "assurance_require_qga": False,
        "assurance_require_http": False,
        "assurance_max_rto_sec": 0,
    }
    base.update(kwargs)
    return base


def _run(**kwargs: Any) -> dict[str, Any]:
    base = {
        "id": "r1",
        "drill": True,
        "status": PlanRunStatus.COMPLETED.value,
        "started_at": "2026-08-01T10:00:00+00:00",
        "finished_at": "2026-08-01T10:05:00+00:00",
        "auto_teardown": True,
        "teardown_status": "completed",
    }
    base.update(kwargs)
    return base


def test_assurance_pass_basic() -> None:
    status, detail, rto = evaluate_assurance_policy(_plan(), _run(), [{"qga_ok": "1"}])
    assert status == PlanAssurance.ASSURED.value
    assert rto == 300
    assert "completed" in detail


def test_assurance_fail_run_status() -> None:
    status, detail, _rto = evaluate_assurance_policy(
        _plan(), _run(status=PlanRunStatus.FAILED.value), []
    )
    assert status == PlanAssurance.FAILED.value
    assert "FAILED" in detail


def test_assurance_require_qga() -> None:
    status, detail, _ = evaluate_assurance_policy(
        _plan(assurance_require_qga=True),
        _run(),
        [{"qga_ok": "0"}],
    )
    assert status == PlanAssurance.FAILED.value
    assert "QGA" in detail

    status, _, _ = evaluate_assurance_policy(
        _plan(assurance_require_qga=True),
        _run(),
        [{"qga_ok": "1"}],
    )
    assert status == PlanAssurance.ASSURED.value


def test_assurance_require_http() -> None:
    status, detail, _ = evaluate_assurance_policy(
        _plan(assurance_require_http=True),
        _run(),
        [{"http_check_url": "http://x", "http_check_ok": "0"}],
    )
    assert status == PlanAssurance.FAILED.value
    assert "HTTP" in detail

    status, _, _ = evaluate_assurance_policy(
        _plan(assurance_require_http=True),
        _run(),
        [{"http_check_url": "http://x", "http_check_ok": "1"}],
    )
    assert status == PlanAssurance.ASSURED.value


def test_assurance_max_rto() -> None:
    status, detail, rto = evaluate_assurance_policy(
        _plan(assurance_max_rto_sec=60),
        _run(),
        [],
    )
    assert status == PlanAssurance.FAILED.value
    assert rto == 300
    assert "RTO" in detail


def test_assurance_disabled_returns_unknown() -> None:
    status, detail, _ = evaluate_assurance_policy(
        _plan(assurance_enabled=False), _run(), []
    )
    assert status == PlanAssurance.UNKNOWN.value
    assert "disabled" in detail


def test_assurance_hostname_mismatch_is_warning_not_fail() -> None:
    status, detail, _ = evaluate_assurance_policy(
        _plan(),
        _run(),
        [
            {
                "qga_ok": "1",
                "hostname_match": "0",
                "hostname_warning": "Guest hostname 'old-name' does not match PVE name 'test-clone2'",
                "proxmox_vmid": "153",
            }
        ],
    )
    assert status == PlanAssurance.ASSURED.value
    assert "warnings:" in detail
    assert "old-name" in detail


def test_assurance_dashboard_in_progress_overlay() -> None:
    r = FakeRedis()
    import plans

    plan = plans.normalize_plan(
        {
            "name": "recoveryplan1",
            "group_ids": ["g"],
            "location_id": "l",
            "assurance_enabled": True,
            "assurance_status": PlanAssurance.UNKNOWN.value,
        },
        plan_id="p-active",
    )
    plans._save_entity(
        r,
        CFG,
        key=plans.plan_key(CFG, "p-active"),
        index=plans.plans_index(CFG),
        entity_id="p-active",
        data=plan,
    )
    run = {
        "id": "run-active",
        "plan_id": "p-active",
        "status": PlanRunStatus.RUNNING.value,
        "drill": True,
        "started_at": "2026-08-09T10:00:00+00:00",
    }
    plans.save_plan_run(r, CFG, run)
    dash = assurance_dashboard(r, CFG)
    assert dash["counts"]["IN_PROGRESS"] == 1
    assert dash["counts"]["UNKNOWN"] == 0
    row = next(p for p in dash["plans"] if p["plan_id"] == "p-active")
    assert row["assurance_status"] == PlanAssurance.IN_PROGRESS.value
    assert row["active_run_id"] == "run-active"


def test_assurance_dashboard_counts() -> None:
    r = FakeRedis()
    import plans

    plans._save_entity(
        r,
        CFG,
        key=plans.plan_key(CFG, "a"),
        index=plans.plans_index(CFG),
        entity_id="a",
        data=plans.normalize_plan(
            {
                "name": "A",
                "group_ids": ["g"],
                "location_id": "l",
                "assurance_enabled": True,
                "assurance_status": PlanAssurance.ASSURED.value,
            },
            plan_id="a",
        ),
    )
    # Need a group entity? normalize only needs ids. Save plan B failed, C disabled.
    plans._save_entity(
        r,
        CFG,
        key=plans.plan_key(CFG, "b"),
        index=plans.plans_index(CFG),
        entity_id="b",
        data=plans.normalize_plan(
            {
                "name": "B",
                "group_ids": ["g"],
                "location_id": "l",
                "assurance_enabled": True,
                "assurance_status": PlanAssurance.FAILED.value,
            },
            plan_id="b",
        ),
    )
    plans._save_entity(
        r,
        CFG,
        key=plans.plan_key(CFG, "c"),
        index=plans.plans_index(CFG),
        entity_id="c",
        data=plans.normalize_plan(
            {
                "name": "C",
                "group_ids": ["g"],
                "location_id": "l",
                "assurance_enabled": False,
            },
            plan_id="c",
        ),
    )
    dash = assurance_dashboard(r, CFG)
    assert dash["counts"]["ASSURED"] == 1
    assert dash["counts"]["FAILED"] == 1
    assert dash["counts"]["disabled"] == 1
    assert len(dash["plans"]) == 3
