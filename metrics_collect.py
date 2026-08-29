"""Collect lightweight infra snapshots from PVE/PBS APIs (Grafana fallback)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from pbs_client import _authenticated_get
from pve_client import connect_proxmox, list_cluster_nodes
from sources import load_sources


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def monitoring_config(cfg: dict[str, Any]) -> dict[str, Any]:
    mon = cfg.get("monitoring")
    if not isinstance(mon, dict):
        return {
            "enabled": True,
            "api_snapshot": True,
            "grafana": {},
        }
    out = {
        "enabled": bool(mon.get("enabled", True)),
        "api_snapshot": bool(mon.get("api_snapshot", True)),
        "grafana": {},
    }
    g = mon.get("grafana")
    if isinstance(g, dict):
        base = str(g.get("base_url") or "").strip().rstrip("/")
        dashboards: list[dict[str, str]] = []
        raw_list = g.get("dashboards")
        if isinstance(raw_list, list):
            for row in raw_list:
                if not isinstance(row, dict):
                    continue
                uid = str(row.get("uid") or "").strip()
                path = str(row.get("path") or "").strip()
                title = str(row.get("title") or uid or "Dashboard").strip()
                dash_id = str(row.get("id") or uid or title).strip()
                if not path and uid:
                    path = f"/d/{uid}/{uid}?orgId=1&kiosk&theme=dark"
                if base and path:
                    dashboards.append(
                        {
                            "id": dash_id,
                            "title": title,
                            "uid": uid,
                            "path": path,
                            "embed_url": base + (path if path.startswith("/") else "/" + path),
                        }
                    )
        # base_url alone is enough: default to the provisioned Restore infra dashboard.
        if base and not dashboards:
            path = "/d/restore-infra/restore-infra?orgId=1&kiosk&theme=dark"
            dashboards.append(
                {
                    "id": "infra",
                    "title": "Infra",
                    "uid": "restore-infra",
                    "path": path,
                    "embed_url": base + path,
                }
            )
        out["grafana"] = {
            "base_url": base,
            "dashboards": dashboards,
        }
    return out


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def collect_pve_nodes(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (nodes, errors) from PVE node status."""
    nodes_out: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        px = connect_proxmox(cfg)
        listed = list_cluster_nodes(px)
    except Exception as exc:
        return [], [f"pve connect: {exc}"]

    if not listed:
        # Fallback to configured default node only.
        default = str((cfg.get("proxmox") or {}).get("default_node") or "").strip()
        listed = [{"node": default, "status": "unknown", "online": True}] if default else []

    for row in listed:
        name = str(row.get("node") or "").strip()
        if not name:
            continue
        try:
            st = px.nodes(name).status.get()
            if not isinstance(st, dict):
                errors.append(f"pve {name}: unexpected status payload")
                continue
            mem_total = _safe_int(st.get("memory", {}).get("total") if isinstance(st.get("memory"), dict) else st.get("maxmem"))
            mem_used = _safe_int(st.get("memory", {}).get("used") if isinstance(st.get("memory"), dict) else st.get("mem"))
            if mem_total <= 0:
                mem_total = _safe_int(st.get("maxmem"))
                mem_used = _safe_int(st.get("memory"))
            cpu = _safe_float(st.get("cpu"))
            # cpu is fraction 0..1 on PVE status
            if cpu > 1.0:
                cpu = cpu / 100.0
            nodes_out.append(
                {
                    "id": name,
                    "source": "pve",
                    "online": bool(row.get("online", True)),
                    "cpu": cpu,
                    "mem_used": mem_used,
                    "mem_total": mem_total,
                    "loadavg": st.get("loadavg"),
                    "uptime": _safe_int(st.get("uptime")),
                    "net_in_bps": None,
                    "net_out_bps": None,
                }
            )
            # Best-effort RRD latest net rates (bytes/s).
            try:
                rrd = px.nodes(name).rrddata.get(timeframe="hour", cf="AVERAGE")
                if isinstance(rrd, list) and rrd:
                    for sample in reversed(rrd):
                        if not isinstance(sample, dict):
                            continue
                        nin = sample.get("netin")
                        nout = sample.get("netout")
                        if nin is not None or nout is not None:
                            nodes_out[-1]["net_in_bps"] = _safe_float(nin) if nin is not None else None
                            nodes_out[-1]["net_out_bps"] = _safe_float(nout) if nout is not None else None
                            break
            except Exception:
                pass
        except Exception as exc:
            errors.append(f"pve {name}: {exc}")
    return nodes_out, errors


def collect_pbs_sources(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (pbs hosts, errors) from PBS node status when available."""
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for source in load_sources(cfg):
        try:
            with httpx.Client(timeout=10.0, verify=bool(source.verify_ssl)) as client:
                # Prefer host status; fall back to /version only.
                resp = _authenticated_get(client, source, "/nodes/localhost/status")
                if resp.status_code != 200:
                    resp = _authenticated_get(client, source, "/status/metrics")
                data: Any = None
                if resp.status_code == 200:
                    payload = resp.json() if resp.content else {}
                    data = payload.get("data", payload)
                entry: dict[str, Any] = {
                    "id": source.source_id,
                    "label": source.label,
                    "host": source.host,
                    "source": "pbs",
                    "ok": resp.status_code == 200,
                    "cpu": None,
                    "mem_used": None,
                    "mem_total": None,
                    "net_in_bps": None,
                    "net_out_bps": None,
                }
                if isinstance(data, dict):
                    cpu = data.get("cpu")
                    if cpu is not None:
                        c = _safe_float(cpu)
                        entry["cpu"] = c / 100.0 if c > 1.0 else c
                    mem = data.get("memory")
                    if isinstance(mem, dict):
                        entry["mem_used"] = _safe_int(mem.get("used"))
                        entry["mem_total"] = _safe_int(mem.get("total"))
                    elif data.get("mem") is not None:
                        entry["mem_used"] = _safe_int(data.get("mem"))
                        entry["mem_total"] = _safe_int(data.get("maxmem"))
                elif isinstance(data, list):
                    # /status/metrics style list — keep connectivity ok only.
                    pass
                if resp.status_code != 200:
                    # Still try /version for reachability.
                    ver = _authenticated_get(client, source, "/version")
                    entry["ok"] = ver.status_code == 200
                    if not entry["ok"]:
                        errors.append(f"pbs {source.source_id}: HTTP {resp.status_code}")
                out.append(entry)
        except Exception as exc:
            errors.append(f"pbs {source.source_id}: {exc}")
            out.append(
                {
                    "id": source.source_id,
                    "label": source.label,
                    "host": source.host,
                    "source": "pbs",
                    "ok": False,
                    "cpu": None,
                    "mem_used": None,
                    "mem_total": None,
                    "net_in_bps": None,
                    "net_out_bps": None,
                }
            )
    return out, errors


def collect_infra_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    """Full snapshot for GET /api/infra/metrics."""
    mon = monitoring_config(cfg)
    nodes: list[dict[str, Any]] = []
    pbs: list[dict[str, Any]] = []
    errors: list[str] = []
    if mon.get("api_snapshot", True):
        n, e1 = collect_pve_nodes(cfg)
        nodes, errors = n, list(e1)
        p, e2 = collect_pbs_sources(cfg)
        pbs = p
        errors.extend(e2)
    return {
        "ts": utc_now_iso(),
        "monitoring": {
            "enabled": mon.get("enabled", True),
            "grafana": mon.get("grafana") or {},
            "api_snapshot": mon.get("api_snapshot", True),
        },
        "nodes": nodes,
        "pbs": pbs,
        "interfaces": [],
        "errors": errors,
    }
