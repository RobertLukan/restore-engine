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


def qemu_vmids_in_use_cluster(proxmox: ProxmoxAPI) -> set[int]:
    """Cluster-wide QEMU VMIDs (all nodes). Falls back to empty on API shape surprises."""
    raw: Any = proxmox.cluster.resources.get(type="vm")
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
        typ = str(row.get("type") or "").strip().lower()
        if typ and typ != "qemu":
            continue
        vid = row.get("vmid")
        if vid is None:
            continue
        try:
            out.add(int(vid))
        except (TypeError, ValueError):
            continue
    return out


def find_qemu_node(proxmox: ProxmoxAPI, vmid: int) -> str | None:
    """Return the node hosting QEMU ``vmid``, or None if not found."""
    try:
        raw: Any = proxmox.cluster.resources.get(type="vm")
    except Exception:
        return None
    rows: list[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        rows = raw["data"]
    else:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        typ = str(row.get("type") or "").strip().lower()
        if typ and typ != "qemu":
            continue
        try:
            if int(row.get("vmid")) != int(vmid):
                continue
        except (TypeError, ValueError):
            continue
        node = str(row.get("node") or "").strip()
        return node or None
    return None


def stop_qemu_vm(proxmox: ProxmoxAPI, node: str, vmid: int, *, timeout: int = 30) -> None:
    """Best-effort stop of a QEMU guest (no-op if already stopped / missing)."""
    try:
        proxmox.nodes(node).qemu(int(vmid)).status.stop.post(timeout=int(timeout))
    except Exception:
        pass


def start_qemu_vm(proxmox: ProxmoxAPI, node: str, vmid: int) -> str | None:
    """Start a QEMU guest. Returns UPID when Proxmox returns one, else None."""
    result: Any = proxmox.nodes(node).qemu(int(vmid)).status.start.post()
    if isinstance(result, str) and result.strip():
        return result.strip()
    if isinstance(result, dict):
        upid = result.get("data") or result.get("upid")
        if upid:
            return str(upid)
    return None


def qemu_agent_ping(proxmox: ProxmoxAPI, node: str, vmid: int) -> bool:
    """Return True when the QEMU guest agent responds to ping."""
    try:
        proxmox.nodes(node).qemu(int(vmid)).agent.ping.get()
        return True
    except Exception:
        try:
            # Some proxmoxer versions expose ping as POST.
            proxmox.nodes(node).qemu(int(vmid)).agent("ping").post()
            return True
        except Exception:
            return False


def wait_for_qemu_agent(
    proxmox: ProxmoxAPI,
    node: str,
    vmid: int,
    *,
    timeout_sec: float = 120.0,
    poll_interval_sec: float = 3.0,
    should_cancel: Callable[[], bool] | None = None,
) -> float:
    """Poll guest-agent ping until success.

    Returns seconds waited. Raises ``TimeoutError`` on timeout, ``TaskCancelled``
    when ``should_cancel`` returns true.
    """
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    started = time.monotonic()
    while True:
        if should_cancel is not None and should_cancel():
            raise TaskCancelled(f"QGA wait cancelled for VMID {vmid}")
        if qemu_agent_ping(proxmox, node, vmid):
            return max(0.0, time.monotonic() - started)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"QEMU guest agent did not respond within {int(timeout_sec)}s "
                f"(VMID {vmid} on {node})"
            )
        time.sleep(max(0.5, float(poll_interval_sec)))


def destroy_qemu_vm(
    proxmox: ProxmoxAPI,
    node: str,
    vmid: int,
    *,
    purge: bool = True,
    destroy_unreferenced_disks: bool = True,
) -> None:
    """Destroy a QEMU VM. Stops first when needed; raises on hard API failures."""
    vmid = int(vmid)
    node = str(node).strip()
    if not node:
        raise ValueError("node is required to destroy a VM")
    stop_qemu_vm(proxmox, node, vmid)
    params: dict[str, Any] = {}
    if purge:
        params["purge"] = 1
    if destroy_unreferenced_disks:
        params["destroy-unreferenced-disks"] = 1
    try:
        proxmox.nodes(node).qemu(vmid).delete(**params)
    except TypeError:
        # Older proxmoxer / API may not accept destroy-unreferenced-disks.
        params.pop("destroy-unreferenced-disks", None)
        proxmox.nodes(node).qemu(vmid).delete(**params)


def list_cluster_nodes(proxmox: ProxmoxAPI) -> list[dict[str, Any]]:
    """Return online-ish cluster nodes for restore placement UI."""
    raw: Any = proxmox.nodes.get()
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
        name = str(row.get("node") or "").strip()
        if not name:
            continue
        status = str(row.get("status") or "").strip().lower()
        out.append(
            {
                "node": name,
                "status": status or "unknown",
                "online": status in ("", "online"),
            }
        )
    return sorted(out, key=lambda item: item["node"].lower())


def assign_nodes_least_loaded(
    candidates: list[str], count: int, *, active_counts: dict[str, int] | None = None
) -> list[str]:
    """Assign ``count`` jobs to ``candidates`` preferring currently least-loaded nodes.

    Ties break by candidate list order (stable round-robin among equals). Used so
    live restores spread CPU/RAM/network across the cluster instead of pinning one node.
    """
    nodes = [n.strip() for n in candidates if str(n).strip()]
    if not nodes:
        raise ValueError("at least one Proxmox node is required")
    if count < 1:
        return []
    counts = {n: int((active_counts or {}).get(n, 0) or 0) for n in nodes}
    order = {n: i for i, n in enumerate(nodes)}
    assigned: list[str] = []
    for _ in range(count):
        pick = min(nodes, key=lambda n: (counts[n], order[n]))
        assigned.append(pick)
        counts[pick] += 1
    return assigned


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
        sid = str(row.get("storage") or "").strip()
        if not sid:
            continue
        try:
            avail = int(row.get("avail") or 0)
        except (TypeError, ValueError):
            avail = 0
        try:
            total = int(row.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        out.append(
            {
                "id": sid,
                "type": str(row.get("type") or ""),
                "enabled": int(row.get("enabled", 1)) == 1,
                "active": int(row.get("active", 1)),
                "usable_for_vm_disks": supports_images,
                "supports_disk_import": supports_images,
                "avail_bytes": avail,
                "total_bytes": total,
            }
        )
    return sorted(out, key=lambda item: item["id"].lower())


def resolve_storage_for_node(
    node: str,
    *,
    storage_by_node: dict[str, str] | None,
    default_storage: str,
) -> str:
    """Pick target storage for a node (per-node map wins, else default)."""
    mapped = ""
    if storage_by_node:
        mapped = str(storage_by_node.get(node) or "").strip()
    if mapped:
        return mapped
    fallback = (default_storage or "").strip()
    if fallback:
        return fallback
    raise RuntimeError(f"No target storage selected for node {node}")


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
    unique: bool = True,
    force: bool = True,
) -> str:
    """Submit a QEMU restore on ``node``.

    ``unique`` regenerates MAC/SMBIOS UUID (normal/lab restores). DR restores
    pass ``unique=False`` to keep identities from the backup. ``force`` allows
    overwriting an empty VMID slot; DR uses ``force=False`` so an existing VMID fails.
    """
    params: dict[str, Any] = {
        "vmid": int(target_vmid),
        "archive": archive,
        "storage": target_storage,
        "live-restore": 1 if live_restore else 0,
        "unique": 1 if unique else 0,
        "force": 1 if force else 0,
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


def fetch_task_log(
    proxmox: ProxmoxAPI,
    node: str,
    upid: str,
    *,
    start: int = 0,
    limit: int = 50,
) -> tuple[list[str], int]:
    """Return (log lines, next_start_offset) from a PVE task log.

    Proxmox returns either a list of ``{n, t}`` rows or a wrapped ``data`` list.
    ``start`` is the line number to begin from (0-based).
    """
    try:
        raw: Any = proxmox.nodes(node).tasks(upid).log.get(start=int(start), limit=int(limit))
    except Exception:
        return [], start
    rows: list[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        rows = raw["data"]
    else:
        return [], start
    lines: list[str] = []
    max_n = start - 1
    for row in rows:
        if isinstance(row, dict):
            text = str(row.get("t") or row.get("text") or "").rstrip()
            try:
                n = int(row.get("n"))
                max_n = max(max_n, n)
            except (TypeError, ValueError):
                pass
            if text:
                lines.append(text)
        elif isinstance(row, str) and row.strip():
            lines.append(row.strip())
    next_start = max_n + 1 if max_n >= start else start + len(lines)
    return lines, next_start


def wait_for_task(
    proxmox: ProxmoxAPI,
    node: str,
    upid: str,
    *,
    poll_interval_sec: float = 3.0,
    timeout_sec: float = 7200.0,
    should_cancel: Callable[[], bool] | None = None,
    on_tick: Callable[[dict[str, Any], list[str]], None] | None = None,
) -> dict[str, Any]:
    """Poll until the PVE task stops.

    ``on_tick(status, new_log_lines)`` is invoked on each poll while running
    (and once more with final status when stopped, with any remaining log lines).
    """
    deadline = time.time() + timeout_sec
    log_offset = 0
    while time.time() < deadline:
        if should_cancel is not None and should_cancel():
            stop_task(proxmox, node, upid)
            raise TaskCancelled(f"PVE task {upid} cancelled by operator")
        status: Any = proxmox.nodes(node).tasks(upid).status.get()
        if not isinstance(status, dict):
            time.sleep(poll_interval_sec)
            continue
        new_lines, log_offset = fetch_task_log(proxmox, node, upid, start=log_offset, limit=80)
        state = str(status.get("status") or "").lower()
        if on_tick is not None:
            try:
                on_tick(status, new_lines)
            except Exception:
                pass
        if state == "stopped":
            exitstatus = str(status.get("exitstatus") or "")
            if exitstatus and exitstatus.lower() != "ok":
                raise RuntimeError(_explain_pve_task_failure(exitstatus))
            return status
        time.sleep(poll_interval_sec)
    raise TimeoutError(f"PVE task {upid} did not finish within {timeout_sec:.0f}s")


def _explain_pve_task_failure(exitstatus: str) -> str:
    """Add operator context for common Proxmox restore permission failures."""
    msg = f"PVE task failed: {exitstatus}"
    low = exitstatus.lower()
    if "hostpci" in low and "only root" in low:
        msg += (
            " — this backup includes PCI passthrough; Proxmox requires root "
            "(or mapped PCI devices) to restore hostpci* config. Use root "
            "credentials, or remove passthrough from the source before backup."
        )
    return msg
