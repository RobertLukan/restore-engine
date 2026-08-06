"""Proxmox Backup Server API helpers (multi-server, multi-datastore, namespaces)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from sources import Source, load_sources


def _epoch_to_iso_z(value: Any) -> str:
    """Convert a PBS backup-time (unix epoch) to an ISO-8601 UTC ``...Z`` string.

    PVE PBS-backed restore volids identify a snapshot by this timestamp, e.g.
    ``pbs-main:backup/vm/100/2026-05-01T01:00:00Z``.
    """
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        return str(value or "")
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _base_url(source: Source) -> str:
    return f"https://{source.host}:{int(source.port)}/api2/json"


def _headers(source: Source) -> dict[str, str]:
    token_id = (source.api_token_id or "").strip()
    token_secret = (source.api_token_secret or "").strip()
    if not token_id or not token_secret:
        raise ValueError(f"PBS source {source.source_id!r} is missing api_token_id/api_token_secret")
    return {"Authorization": f"PBSAPIToken={token_id}={token_secret}"}


def test_pbs_source(source: Source) -> tuple[bool, str]:
    try:
        with httpx.Client(timeout=12.0, verify=bool(source.verify_ssl)) as client:
            response = client.get(f"{_base_url(source)}/version", headers=_headers(source))
            if response.status_code == 200:
                return True, "PBS connection successful."
            return False, f"PBS connection failed: HTTP {response.status_code} {response.text}"
    except Exception as exc:
        return False, f"PBS connection failed: {exc}"


def test_all_sources(cfg: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Test connectivity to every configured PBS server (deduped by host+token)."""
    sources = load_sources(cfg)
    if not sources:
        return False, [{"ok": False, "detail": "no PBS sources configured"}]
    seen: dict[str, tuple[bool, str]] = {}
    results: list[dict[str, Any]] = []
    for src in sources:
        server_key = f"{src.server_id}@{src.host}:{src.port}"
        if server_key not in seen:
            seen[server_key] = test_pbs_source(src)
        ok, msg = seen[server_key]
        results.append({"source_id": src.source_id, "label": src.label, "ok": ok, "detail": msg})
    overall = all(r["ok"] for r in results)
    return overall, results


def test_pbs_connection(cfg: dict[str, Any]) -> tuple[bool, str]:
    """Aggregate connectivity check used by /health and the saved-credentials verify."""
    overall, results = test_all_sources(cfg)
    if overall:
        return True, f"{len(results)} PBS source(s) reachable."
    failed = [f"{r['label']}: {r['detail']}" for r in results if not r["ok"]]
    return False, "; ".join(failed) or "no PBS sources reachable"


def _list_source_backups(source: Source) -> list[dict[str, Any]]:
    if not source.datastore:
        raise ValueError(f"PBS source {source.source_id!r} has no datastore")
    url = f"{_base_url(source)}/admin/datastore/{source.datastore}/snapshots"
    params: dict[str, str] = {}
    if source.namespace:
        params["ns"] = source.namespace
    with httpx.Client(timeout=30.0, verify=bool(source.verify_ssl)) as client:
        response = client.get(url, headers=_headers(source), params=params or None)
        if response.status_code != 200:
            raise RuntimeError(
                f"PBS list snapshots failed for {source.label}: HTTP {response.status_code} {response.text}"
            )
        payload = response.json().get("data") or []
    out: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        if str(row.get("backup-type") or row.get("backup_type") or "").lower() != "vm":
            continue
        raw_id = str(row.get("backup-id") or row.get("backup_id") or "").strip()
        if not raw_id:
            continue
        try:
            vmid = int(row.get("vmid") or raw_id)
        except (TypeError, ValueError):
            continue
        raw_time = row.get("backup-time")
        if raw_time is None:
            raw_time = row.get("backup_time")
        iso_time = _epoch_to_iso_z(raw_time)
        if not iso_time:
            continue
        voltail = f"vm/{vmid}/{iso_time}"
        out.append(
            {
                # Globally unique across servers/datastores/namespaces.
                "backup_id": f"{source.source_id}|{voltail}",
                "voltail": voltail,
                "vmid": vmid,
                "name": str(row.get("comment") or row.get("hostname") or f"vm-{vmid}"),
                "timestamp": iso_time,
                "datastore": source.datastore,
                "namespace": source.namespace,
                "source_id": source.source_id,
                "source_label": source.label,
                "pve_storage": source.pve_storage,
            }
        )
    return out


def list_vm_backups(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """List VM backups across every configured source."""
    sources = load_sources(cfg)
    if not sources:
        raise ValueError("No PBS sources configured (set pbs_servers in config)")
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for src in sources:
        try:
            out.extend(_list_source_backups(src))
        except Exception as exc:  # one bad source should not hide the others
            errors.append(str(exc))
    if not out and errors:
        raise RuntimeError("; ".join(errors))
    out.sort(key=lambda item: (item["source_label"].lower(), item["name"].lower(), item["timestamp"]))
    out.sort(key=lambda item: item["timestamp"], reverse=True)
    return out
