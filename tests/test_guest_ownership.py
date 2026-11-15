"""Ownership safeguards: never destroy foreign QEMU/LXC guests."""

from __future__ import annotations

from typing import Any

import pytest

from pve_client import (
    GuestOwnershipError,
    MANAGED_TAG,
    batch_id_from_plan_run_id,
    batch_tag_for_id,
    destroy_owned_qemu_vm,
    mark_qemu_managed_by_tool,
    qemu_is_managed_by_tool,
    qemu_matches_batch,
)


class _CfgApi:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = dict(cfg)
        self.puts: list[dict[str, Any]] = []

    def get(self) -> dict[str, Any]:
        return dict(self.cfg)

    def put(self, **kwargs: Any) -> None:
        self.puts.append(dict(kwargs))
        self.cfg.update(kwargs)


class _QemuApi:
    def __init__(self, px: "_FakePx", node: str, vmid: int, cfg: dict[str, Any]) -> None:
        self._px = px
        self._node = node
        self._vmid = vmid
        self.config = _CfgApi(cfg)
        self.deleted = False
        self.status = type(
            "S",
            (),
            {
                "stop": type("Stop", (), {"post": lambda *a, **k: None})(),
            },
        )()

    def delete(self, **_kwargs: Any) -> str:
        self.deleted = True
        self._px.remove_guest(self._vmid)
        return f"UPID:{self._node}:000001:000001:000001:qmdestroy:{self._vmid}:root@pam:"


class _NodeApi:
    def __init__(self, px: "_FakePx", node: str, guests: dict[int, tuple[str, dict[str, Any]]]) -> None:
        self._px = px
        self._node = node
        self._guests = guests
        self._qemu: dict[int, _QemuApi] = {}

    def qemu(self, vmid: int) -> _QemuApi:
        vmid = int(vmid)
        if vmid not in self._qemu:
            typ, cfg = self._guests[vmid]
            assert typ == "qemu"
            self._qemu[vmid] = _QemuApi(self._px, self._node, vmid, cfg)
        return self._qemu[vmid]

    def tasks(self, upid: str) -> Any:
        return type(
            "T",
            (),
            {
                "status": type(
                    "St",
                    (),
                    {"get": lambda *a, **k: {"status": "stopped", "exitstatus": "OK"}},
                )(),
                "log": type("L", (), {"get": lambda *a, **k: []})(),
            },
        )()


class _Resources:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def get(self, type: str = "vm") -> list[dict[str, Any]]:  # noqa: A002
        assert type == "vm"
        return list(self._rows)


class _FakePx:
    def __init__(self, rows: list[dict[str, Any]], configs: dict[int, dict[str, Any]]) -> None:
        self.cluster = type("C", (), {"resources": _Resources(list(rows))})()
        self._configs = configs
        self._nodes: dict[str, _NodeApi] = {}

    def remove_guest(self, vmid: int) -> None:
        self.cluster.resources._rows = [
            r for r in self.cluster.resources._rows if int(r.get("vmid", -1)) != int(vmid)
        ]

    def nodes(self, node: str) -> _NodeApi:
        if node not in self._nodes:
            guests = {
                int(r["vmid"]): (str(r["type"]), self._configs[int(r["vmid"])])
                for r in self.cluster.resources._rows
                if r.get("node") == node
            }
            self._nodes[node] = _NodeApi(self, node, guests)
        return self._nodes[node]


def test_qemu_is_managed_by_tag() -> None:
    px = _FakePx(
        [{"type": "qemu", "vmid": 200, "node": "pve"}],
        {200: {"tags": f"prod;{MANAGED_TAG}", "description": ""}},
    )
    assert qemu_is_managed_by_tool(px, "pve", 200) is True  # type: ignore[arg-type]


def test_destroy_owned_refuses_lxc() -> None:
    px = _FakePx(
        [{"type": "lxc", "vmid": 100, "node": "pve"}],
        {100: {}},
    )
    with pytest.raises(GuestOwnershipError, match="LXC"):
        destroy_owned_qemu_vm(px, "pve", 100)  # type: ignore[arg-type]


def test_destroy_owned_refuses_foreign_qemu() -> None:
    px = _FakePx(
        [{"type": "qemu", "vmid": 200, "node": "pve"}],
        {200: {"tags": "prod", "description": "manual vm"}},
    )
    with pytest.raises(GuestOwnershipError, match="not provisioned"):
        destroy_owned_qemu_vm(px, "pve", 200)  # type: ignore[arg-type]


def test_destroy_owned_allows_managed() -> None:
    px = _FakePx(
        [{"type": "qemu", "vmid": 200, "node": "pve"}],
        {200: {"tags": MANAGED_TAG, "description": "restore-engine:job=abc"}},
    )
    reason = destroy_owned_qemu_vm(px, "pve", 200)  # type: ignore[arg-type]
    assert reason == "managed"
    assert px.nodes("pve").qemu(200).deleted is True


def test_destroy_owned_refuses_provenance_for_unmarked() -> None:
    """RE-01: historical provenance must not delete unmarked / reused VMIDs."""
    px = _FakePx(
        [{"type": "qemu", "vmid": 201, "node": "pve"}],
        {201: {"tags": "", "description": ""}},
    )
    with pytest.raises(GuestOwnershipError, match="not provisioned"):
        destroy_owned_qemu_vm(px, "pve", 201, allow_run_provenance=True)  # type: ignore[arg-type]


def test_destroy_owned_refuses_provenance_wrong_batch() -> None:
    """RE-01: provenance must not delete a managed guest from another batch."""
    px = _FakePx(
        [{"type": "qemu", "vmid": 200, "node": "pve"}],
        {200: {"tags": f"{MANAGED_TAG};re-bbbbbbbbbbbb", "description": ""}},
    )
    with pytest.raises(GuestOwnershipError, match="batch"):
        destroy_owned_qemu_vm(  # type: ignore[arg-type]
            px,
            "pve",
            200,
            batch_id="aaaaaaaaaaaa",
            require_batch=True,
            allow_run_provenance=True,
        )


def test_destroy_owned_refuses_foreign_reuse_with_require_batch() -> None:
    px = _FakePx(
        [{"type": "qemu", "vmid": 200, "node": "pve"}],
        {200: {"tags": "prod", "description": ""}},
    )
    with pytest.raises(GuestOwnershipError, match="not provisioned"):
        destroy_owned_qemu_vm(  # type: ignore[arg-type]
            px,
            "pve",
            200,
            batch_id="aaaaaaaaaaaa",
            require_batch=True,
            allow_run_provenance=True,
        )


def test_batch_id_from_plan_run_id() -> None:
    assert batch_id_from_plan_run_id("a1b2c3d4-e5f6-7890-abcd-ef1234567890") == "a1b2c3d4e5f6"
    assert batch_tag_for_id("a1b2c3d4e5f6") == "re-a1b2c3d4e5f6"


def test_mark_adds_batch_tag() -> None:
    px = _FakePx(
        [{"type": "qemu", "vmid": 200, "node": "pve"}],
        {200: {"tags": "", "description": ""}},
    )
    mark_qemu_managed_by_tool(  # type: ignore[arg-type]
        px,
        "pve",
        200,
        job_id="job-1",
        plan_run_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    )
    cfg = px.nodes("pve").qemu(200).config.cfg
    tags = {t.lower() for t in str(cfg.get("tags") or "").replace(",", ";").split(";") if t}
    assert MANAGED_TAG.lower() in tags
    assert "re-a1b2c3d4e5f6" in tags
    assert "run=a1b2c3d4-e5f6-7890-abcd-ef1234567890" in str(cfg.get("description") or "")


def test_qemu_matches_batch_tag() -> None:
    px = _FakePx(
        [{"type": "qemu", "vmid": 200, "node": "pve"}],
        {200: {"tags": f"{MANAGED_TAG};re-a1b2c3d4e5f6", "description": ""}},
    )
    assert qemu_matches_batch(px, "pve", 200, batch_id="a1b2c3d4e5f6") is True  # type: ignore[arg-type]
    assert qemu_matches_batch(px, "pve", 200, batch_id="ffffffffffff") is False  # type: ignore[arg-type]


def test_destroy_owned_refuses_wrong_batch() -> None:
    px = _FakePx(
        [{"type": "qemu", "vmid": 200, "node": "pve"}],
        {200: {"tags": f"{MANAGED_TAG};re-aaaaaaaaaaaa", "description": "restore-engine:job=x;run=aaaaaaaaaaaa"}},
    )
    with pytest.raises(GuestOwnershipError, match="batch"):
        destroy_owned_qemu_vm(  # type: ignore[arg-type]
            px,
            "pve",
            200,
            batch_id="bbbbbbbbbbbb",
            require_batch=True,
        )


def test_destroy_owned_allows_matching_batch() -> None:
    px = _FakePx(
        [{"type": "qemu", "vmid": 200, "node": "pve"}],
        {200: {"tags": f"{MANAGED_TAG};re-a1b2c3d4e5f6", "description": ""}},
    )
    reason = destroy_owned_qemu_vm(  # type: ignore[arg-type]
        px,
        "pve",
        200,
        batch_id="a1b2c3d4e5f6",
        require_batch=True,
    )
    assert reason == "managed"
    assert px.nodes("pve").qemu(200).deleted is True
