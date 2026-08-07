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


def _has_token(source: Source) -> bool:
    return bool((source.api_token_id or "").strip() and (source.api_token_secret or "").strip())


def _has_password(source: Source) -> bool:
    return bool((source.user or "").strip() and (source.password or "").strip())


def _token_headers(source: Source) -> dict[str, str]:
    token_id = (source.api_token_id or "").strip()
    token_secret = (source.api_token_secret or "").strip()
    return {"Authorization": f"PBSAPIToken={token_id}={token_secret}"}


def _fetch_ticket(client: httpx.Client, source: Source) -> tuple[dict[str, str], dict[str, str]]:
    """Login with username/password; return (headers, cookies) for subsequent calls."""
    user = (source.user or "").strip()
    password = (source.password or "").strip()
    response = client.post(
        f"{_base_url(source)}/access/ticket",
        data={"username": user, "password": password},
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"PBS login failed for {source.label}: HTTP {response.status_code} {response.text}"
        )
    data = (response.json() or {}).get("data") or {}
    ticket = str(data.get("ticket") or "").strip()
    csrf = str(data.get("CSRFPreventionToken") or "").strip()
    if not ticket:
        raise RuntimeError(f"PBS login for {source.label} returned no ticket")
    headers: dict[str, str] = {}
    if csrf:
        headers["CSRFPreventionToken"] = csrf
    cookies = {"PBSAuthCookie": ticket}
    return headers, cookies


def _authenticated_get(
    client: httpx.Client,
    source: Source,
    path: str,
    *,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """GET a PBS API path using token (preferred) or username/password ticket auth."""
    url = f"{_base_url(source)}{path}"
    if _has_token(source):
        return client.get(url, headers=_token_headers(source), params=params)
    if _has_password(source):
        headers, cookies = _fetch_ticket(client, source)
        return client.get(url, headers=headers, cookies=cookies, params=params)
    raise ValueError(
        f"PBS source {source.source_id!r} needs api_token_id/api_token_secret or user/password"
    )


def probe_pbs_source(source: Source) -> tuple[bool, str]:
    try:
        with httpx.Client(timeout=12.0, verify=bool(source.verify_ssl)) as client:
            response = _authenticated_get(client, source, "/version")
            if response.status_code == 200:
                mode = "token" if _has_token(source) else "password"
                return True, f"PBS connection successful ({mode} auth)."
            return False, f"PBS connection failed: HTTP {response.status_code} {response.text}"
    except Exception as exc:
        return False, f"PBS connection failed: {exc}"


def probe_all_sources(cfg: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Test connectivity to every configured PBS server (deduped by host+auth)."""
    sources = load_sources(cfg)
    if not sources:
        return False, [{"ok": False, "detail": "no PBS sources configured"}]
    seen: dict[str, tuple[bool, str]] = {}
    results: list[dict[str, Any]] = []
    for src in sources:
        auth_key = (
            f"tok:{src.api_token_id}"
            if _has_token(src)
            else f"pwd:{src.user}"
        )
        server_key = f"{src.server_id}@{src.host}:{src.port}|{auth_key}"
        if server_key not in seen:
            seen[server_key] = probe_pbs_source(src)
        ok, msg = seen[server_key]
        results.append({"source_id": src.source_id, "label": src.label, "ok": ok, "detail": msg})
    overall = all(r["ok"] for r in results)
    return overall, results


def probe_pbs_connection(cfg: dict[str, Any]) -> tuple[bool, str]:
    """Aggregate connectivity check used by /health and the saved-credentials verify."""
    overall, results = probe_all_sources(cfg)
    if overall:
        return True, f"{len(results)} PBS source(s) reachable."
    failed = [f"{r['label']}: {r['detail']}" for r in results if not r["ok"]]
    return False, "; ".join(failed) or "no PBS sources reachable"


# Back-compat aliases (avoid pytest collecting names that start with test_).
test_pbs_connection = probe_pbs_connection



def _list_source_backups(source: Source) -> list[dict[str, Any]]:
    if not source.datastore:
        raise ValueError(f"PBS source {source.source_id!r} has no datastore")
    params: dict[str, str] = {}
    if source.namespace:
        params["ns"] = source.namespace
    with httpx.Client(timeout=30.0, verify=bool(source.verify_ssl)) as client:
        response = _authenticated_get(
            client,
            source,
            f"/admin/datastore/{source.datastore}/snapshots",
            params=params or None,
        )
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
        size_bytes = 0
        raw_size = row.get("size")
        if raw_size is not None:
            try:
                size_bytes = max(0, int(raw_size))
            except (TypeError, ValueError):
                size_bytes = 0
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
                # PBS snapshot ``size`` = sum of archive sizes from the manifest (gross).
                "size_bytes": size_bytes,
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
