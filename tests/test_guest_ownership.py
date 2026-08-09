"""Ownership safeguards: never destroy foreign QEMU/LXC guests."""

from __future__ import annotations

from typing import Any

import pytest

from pve_client import (
    GuestOwnershipError,
    MANAGED_TAG,
    destroy_owned_qemu_vm,
    qemu_is_managed_by_tool,
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
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.config = _CfgApi(cfg)
        self.deleted = False
        self.status = type(
            "S",
            (),
            {
                "stop": type("Stop", (), {"post": lambda *a, **k: None})(),
            },
        )()

    def delete(self, **_kwargs: Any) -> None:
        self.deleted = True


class _NodeApi:
    def __init__(self, guests: dict[int, tuple[str, dict[str, Any]]]) -> None:
        self._guests = guests
        self._qemu: dict[int, _QemuApi] = {}

    def qemu(self, vmid: int) -> _QemuApi:
        vmid = int(vmid)
        if vmid not in self._qemu:
            typ, cfg = self._guests[vmid]
            assert typ == "qemu"
            self._qemu[vmid] = _QemuApi(cfg)
        return self._qemu[vmid]


class _Resources:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def get(self, type: str = "vm") -> list[dict[str, Any]]:  # noqa: A002
        assert type == "vm"
        return self._rows


class _FakePx:
    def __init__(self, rows: list[dict[str, Any]], configs: dict[int, dict[str, Any]]) -> None:
        self.cluster = type("C", (), {"resources": _Resources(rows)})()
        self._configs = configs
        self._nodes: dict[str, _NodeApi] = {}

    def nodes(self, node: str) -> _NodeApi:
        if node not in self._nodes:
            guests = {
                int(r["vmid"]): (str(r["type"]), self._configs[int(r["vmid"])])
                for r in self.cluster.resources._rows
                if r.get("node") == node
            }
            self._nodes[node] = _NodeApi(guests)
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


def test_destroy_owned_allows_run_provenance_for_unmarked() -> None:
    px = _FakePx(
        [{"type": "qemu", "vmid": 201, "node": "pve"}],
        {201: {"tags": "", "description": ""}},
    )
    reason = destroy_owned_qemu_vm(px, "pve", 201, allow_run_provenance=True)  # type: ignore[arg-type]
    assert reason == "run_provenance"
