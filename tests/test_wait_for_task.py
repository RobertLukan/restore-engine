"""Tests for PVE task wait timeout behaviour."""

from __future__ import annotations

from typing import Any

import pytest

from pve_client import wait_for_task
from worker import task_timeout_sec


class _FakeStatus:
    def __init__(self, task: "_FakeTask") -> None:
        self._task = task

    def get(self) -> dict[str, Any]:
        return self._task.next_status()


class _FakeTask:
    def __init__(self, statuses: list[dict[str, Any]]) -> None:
        self._statuses = list(statuses)
        self.calls = 0
        self.status = _FakeStatus(self)

    def next_status(self) -> dict[str, Any]:
        idx = min(self.calls, len(self._statuses) - 1)
        self.calls += 1
        return self._statuses[idx]


class _FakeNodes:
    def __init__(self, task: _FakeTask) -> None:
        self._task = task

    def __call__(self, _node: str) -> Any:
        return self

    def tasks(self, _upid: str) -> _FakeTask:
        return self._task

    def qemu(self, *_a: Any, **_k: Any) -> Any:
        raise AssertionError("stop_task should not run in these tests")


class _FakeProxmox:
    def __init__(self, task: _FakeTask) -> None:
        self.nodes = _FakeNodes(task)


def test_wait_for_task_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _FakeTask([{"status": "running"}])
    prox = _FakeProxmox(task)
    monkeypatch.setattr("pve_client.fetch_task_log", lambda *a, **k: ([], 0))
    monkeypatch.setattr("pve_client.time.sleep", lambda *_a, **_k: None)

    t = {"now": 1000.0}

    def fake_time() -> float:
        # Advance past deadline after first loop check
        t["now"] += 10.0
        return t["now"]

    monkeypatch.setattr("pve_client.time.time", fake_time)

    with pytest.raises(TimeoutError, match="did not finish within 5s"):
        wait_for_task(prox, "pve", "UPID:x", poll_interval_sec=0.01, timeout_sec=5)  # type: ignore[arg-type]


def test_wait_for_task_zero_timeout_waits_until_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _FakeTask(
        [
            {"status": "running"},
            {"status": "running"},
            {"status": "stopped", "exitstatus": "OK"},
        ]
    )
    prox = _FakeProxmox(task)
    monkeypatch.setattr("pve_client.fetch_task_log", lambda *a, **k: ([], 0))
    monkeypatch.setattr("pve_client.time.sleep", lambda *_a, **_k: None)

    # If a 7200s wall clock were applied incorrectly, this would still finish —
    # the point is timeout_sec=0 must not raise TimeoutError while status is running.
    status = wait_for_task(prox, "pve", "UPID:x", poll_interval_sec=0.01, timeout_sec=0)  # type: ignore[arg-type]
    assert status["status"] == "stopped"
    assert task.calls >= 3


def test_task_timeout_sec_defaults_unlimited() -> None:
    assert task_timeout_sec({}) == 0.0
    assert task_timeout_sec({"worker": {}}) == 0.0
    assert task_timeout_sec({"worker": {"task_timeout_sec": 7200}}) == 7200.0
    assert task_timeout_sec({"worker": {"task_timeout_sec": "bad"}}) == 0.0
