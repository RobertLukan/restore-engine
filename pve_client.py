"""Proxmox VE API helpers for restore orchestration."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from proxmoxer import ProxmoxAPI


class TaskCancelled(RuntimeError):
    """Raised when a running PVE task is cancelled at the operator's request."""


_TAGS_LINE_RE = re.compile(r"^tags:\s*(.+?)\s*$", re.MULTILINE)


def extract_vm_config(proxmox: ProxmoxAPI, node: str, volid: str) -> str:
    """Return the stored guest config for a PBS backup volid via PVE extractconfig.

    Works through PVE (which holds the storage's decryption key), so it also
    covers encrypted datastores. The guest ``tags`` live in this config, not in
    the PBS snapshot listing.
    """
    result: Any = proxmox.nodes(node).vzdump.extractconfig.get(volume=volid)
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("data") or "")
    return str(result or "")


def parse_tags(config_text: str) -> list[str]:
    """Parse the ``tags:`` line from a guest config (PVE separates tags with ';')."""
    match = _TAGS_LINE_RE.search(config_text or "")
    if not match:
        return []
    parts = re.split(r"[;,\s]+", match.group(1).strip())
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        tag = part.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


def connect_proxmox(cfg: dict[str, Any]) -> ProxmoxAPI:
    p = cfg["proxmox"]
    token_id = (p.get("api_token_id") or "").strip()
    token_secret = (p.get("api_token_secret") or "").strip()
    if token_id and token_secret:
        user, sep, token_name = token_id.partition("!")
        user = user.strip()
        token_name = token_name.strip()
        if not sep or not user or not token_name:
            raise ValueError("proxmox.api_token_id must look like 'user@realm!token_name'")
        return ProxmoxAPI(
            host=p["host"],
            user=user,
            token_name=token_name,
            token_value=token_secret,
            port=int(p.get("port", 8006)),
            verify_ssl=bool(p.get("verify_ssl", True)),
        )
    user = (p.get("user") or "").strip()
    password = (p.get("password") or "").strip()
    if not user or not password:
        raise ValueError("proxmox: set api_token_id/api_token_secret or user/password in config.yaml")
    return ProxmoxAPI(
        host=p["host"],
        user=user,
        password=password,
        port=int(p.get("port", 8006)),
        verify_ssl=bool(p.get("verify_ssl", True)),
    )


def test_proxmox_connection(cfg: dict[str, Any]) -> tuple[bool, str]:
    try:
        connect_proxmox(cfg).version.get()
        return True, "Proxmox API reachable."
    except Exception as exc:
        return False, str(exc)


def qemu_vmids_in_use_on_node(proxmox: ProxmoxAPI, node: str) -> set[int]:
    raw: Any = proxmox.nodes(node).qemu.get()
    rows: list[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        rows = raw["data"]
    else:
        return set()
    out: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        vid = row.get("vmid")
        if vid is None:
            continue
        try:
            out.add(int(vid))
        except (TypeError, ValueError):
            continue
    return out


def allocate_sequential_free_vmids(
    in_use: set[int], desired_start: int, count: int
) -> tuple[list[int], int]:
    if count < 1:
        return [], int(desired_start)
    out: list[int] = []
    v = max(100, int(desired_start))
    upper = v + max(5000, count * 250)
    while len(out) < int(count) and v <= upper:
        if v not in in_use:
            out.append(v)
            in_use.add(v)
        v += 1
    if len(out) < count:
        raise RuntimeError(
            f"could not allocate {count} free QEMU VMIDs starting near {desired_start} (exhausted scan to {upper})"
        )
    return out, out[-1] + 1


def list_node_storages(proxmox: ProxmoxAPI, node: str) -> list[dict[str, Any]]:
    raw: Any = proxmox.nodes(node).storage.get()
    rows: list[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        rows = raw["data"]
    else:
        rows = []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = str(row.get("content") or "")
        supports_images = "images" in content
        out.append(
            {
                "id": str(row.get("storage") or ""),
                "type": str(row.get("type") or ""),
                "enabled": int(row.get("enabled", 1)) == 1,
                "active": int(row.get("active", 1)),
                "usable_for_vm_disks": supports_images,
                "supports_disk_import": supports_images,
            }
        )
    return sorted(out, key=lambda item: item["id"].lower())


def archive_path(pve_storage: str, voltail: str) -> str:
    """Build a PVE restore volid from a PBS-backed storage ID and the volume tail.

    ``voltail`` is ``vm/{vmid}/{iso_time}``; the namespace (if any) is bound to the
    PVE storage itself, not encoded in the volid.
    """
    pve_storage = (pve_storage or "").strip()
    if not pve_storage:
        raise ValueError("pve_storage is required (PVE storage ID for the PBS datastore)")
    voltail = (voltail or "").strip().lstrip("/")
    if not voltail:
        raise ValueError("voltail is required (e.g. vm/100/2026-05-01T01:00:00Z)")
    return f"{pve_storage}:backup/{voltail}"


def submit_restore(
    proxmox: ProxmoxAPI,
    *,
    node: str,
    target_vmid: int,
    archive: str,
    target_storage: str,
    live_restore: bool,
    bwlimit: int | None = None,
) -> str:
    params: dict[str, Any] = {
        "vmid": int(target_vmid),
        "archive": archive,
        "storage": target_storage,
        "live-restore": 1 if live_restore else 0,
        "force": 1,
    }
    # bwlimit is KiB/s; 0 or None means "no per-job limit" (Proxmox defaults apply).
    if bwlimit and int(bwlimit) > 0:
        params["bwlimit"] = int(bwlimit)
    result: Any = proxmox.nodes(node).qemu.post(**params)
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        upid = result.get("data") or result.get("upid")
        if upid:
            return str(upid)
    raise RuntimeError(f"Unexpected restore response: {result!r}")


def stop_task(proxmox: ProxmoxAPI, node: str, upid: str) -> None:
    """Best-effort stop of a running PVE task (DELETE on the task resource)."""
    try:
        proxmox.nodes(node).tasks(upid).delete()
    except Exception:
        pass


def wait_for_task(
    proxmox: ProxmoxAPI,
    node: str,
    upid: str,
    *,
    poll_interval_sec: float = 3.0,
    timeout_sec: float = 7200.0,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if should_cancel is not None and should_cancel():
            stop_task(proxmox, node, upid)
            raise TaskCancelled(f"PVE task {upid} cancelled by operator")
        status: Any = proxmox.nodes(node).tasks(upid).status.get()
        if not isinstance(status, dict):
            time.sleep(poll_interval_sec)
            continue
        state = str(status.get("status") or "").lower()
        if state == "stopped":
            exitstatus = str(status.get("exitstatus") or "")
            if exitstatus and exitstatus.lower() != "ok":
                raise RuntimeError(f"PVE task failed: {exitstatus}")
            return status
        time.sleep(poll_interval_sec)
    raise TimeoutError(f"PVE task {upid} did not finish within {timeout_sec:.0f}s")
