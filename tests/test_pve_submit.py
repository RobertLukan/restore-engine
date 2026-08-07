from __future__ import annotations

from typing import Any

from pve_client import submit_restore


class _FakeQemu:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def post(self, **params: Any) -> str:
        self._captured.update(params)
        return "UPID:pve:0001:restore::"


class _FakeNode:
    def __init__(self, captured: dict[str, Any]) -> None:
        self.qemu = _FakeQemu(captured)


class _FakeProxmox:
    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    def nodes(self, node: str) -> _FakeNode:
        self.captured["_node"] = node
        return _FakeNode(self.captured)


def test_submit_restore_includes_bwlimit_when_positive() -> None:
    px = _FakeProxmox()
    upid = submit_restore(
        px,
        node="pve",
        target_vmid=100,
        archive="pbs-main:backup/vm/100/2026-05-01T01:00:00Z",
        target_storage="local-lvm",
        live_restore=True,
        bwlimit=51200,
    )
    assert upid.startswith("UPID:")
    assert px.captured["vmid"] == 100
    assert px.captured["archive"] == "pbs-main:backup/vm/100/2026-05-01T01:00:00Z"
    assert px.captured["storage"] == "local-lvm"
    assert px.captured["live-restore"] == 1
    assert px.captured["unique"] == 1
    assert px.captured["force"] == 1
    assert px.captured["bwlimit"] == 51200


def test_submit_restore_omits_bwlimit_when_zero_or_none() -> None:
    for value in (0, None):
        px = _FakeProxmox()
        submit_restore(
            px,
            node="pve",
            target_vmid=101,
            archive="pbs-main:backup/vm/101/2026-05-01T01:00:00Z",
            target_storage="local-lvm",
            live_restore=False,
            bwlimit=value,
        )
        assert "bwlimit" not in px.captured
        assert px.captured["live-restore"] == 0
        assert px.captured["unique"] == 1
        assert px.captured["force"] == 1


def test_submit_restore_dr_keeps_identity_and_no_force() -> None:
    px = _FakeProxmox()
    submit_restore(
        px,
        node="pve",
        target_vmid=109,
        archive="pbs-main:backup/vm/109/2026-05-01T01:00:00Z",
        target_storage="local-lvm",
        live_restore=True,
        unique=False,
        force=False,
    )
    assert px.captured["vmid"] == 109
    assert px.captured["unique"] == 0
    assert px.captured["force"] == 0
    assert px.captured["live-restore"] == 1
