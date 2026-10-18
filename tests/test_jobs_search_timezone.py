"""Jobs search filter and UI timezone helpers."""

from __future__ import annotations

import pytest

from job_hygiene import filter_jobs, job_matches_query
from ui import normalize_ui_timezone, view_ui


def test_job_matches_query_by_name_and_vmid() -> None:
    job = {"vm_name": "web-frontend", "proxmox_vmid": 105, "source_vmid": 100}
    assert job_matches_query(job, "web") is True
    assert job_matches_query(job, "105") is True
    assert job_matches_query(job, "100") is True
    assert job_matches_query(job, "db") is False
    assert job_matches_query(job, "") is True
    assert job_matches_query(job, "  ") is True


def test_filter_jobs() -> None:
    jobs = [
        {"vm_name": "alpha", "proxmox_vmid": 10, "source_vmid": 1},
        {"vm_name": "beta", "proxmox_vmid": 20, "source_vmid": 2},
    ]
    assert [j["vm_name"] for j in filter_jobs(jobs, "alp")] == ["alpha"]
    assert [j["vm_name"] for j in filter_jobs(jobs, "20")] == ["beta"]
    assert len(filter_jobs(jobs, None)) == 2


def test_normalize_ui_timezone() -> None:
    assert normalize_ui_timezone("UTC") == "UTC"
    assert normalize_ui_timezone("Europe/Ljubljana") == "Europe/Ljubljana"
    assert normalize_ui_timezone("") == "UTC"
    with pytest.raises(ValueError, match="Invalid timezone"):
        normalize_ui_timezone("Not/AZone")


def test_view_ui_defaults_bad_timezone() -> None:
    assert view_ui({})["timezone"] == "UTC"
    assert view_ui({"timezone": "Europe/Berlin"})["timezone"] == "Europe/Berlin"
    assert view_ui({"timezone": "bogus"})["timezone"] == "UTC"
