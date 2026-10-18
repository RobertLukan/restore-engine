"""Proxmox VE cluster resolution: ``pve_clusters`` with legacy ``proxmox:`` shim.

New schema::

    pve_clusters:
      - id: default
        host: 10.0.0.20
        port: 8006
        ...

Legacy singular ``proxmox:`` is treated as one cluster with ``id: default``
without rewriting the file until Settings/wizard save.
"""

from __future__ import annotations

from typing import Any

DEFAULT_CLUSTER_ID = "default"


def _slugify_cluster_id(value: str, fallback: str = DEFAULT_CLUSTER_ID) -> str:
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in (value or "").strip().lower()).strip("-")
    return slug or fallback


def _copy_cluster_fields(raw: dict[str, Any], *, cluster_id: str) -> dict[str, Any]:
    """Normalize one cluster dict (id + proxmox-shaped fields)."""
    out = {
        "id": cluster_id,
        "host": str(raw.get("host") or "").strip(),
        "port": int(raw.get("port", 8006) or 8006),
        "verify_ssl": bool(raw.get("verify_ssl", True)),
        "default_node": str(raw.get("default_node") or "").strip(),
        "api_token_id": str(raw.get("api_token_id") or "").strip(),
        "api_token_secret": str(raw.get("api_token_secret") or "").strip(),
        "user": str(raw.get("user") or "").strip(),
        "password": str(raw.get("password") or "").strip(),
        "storage": str(raw.get("storage") or "").strip(),
        "restore_bwlimit": int(raw.get("restore_bwlimit", 0) or 0),
        "live_restore_default": bool(raw.get("live_restore_default", False)),
    }
    try:
        timeout = float(raw.get("timeout_sec", 30) or 30)
    except (TypeError, ValueError):
        timeout = 30.0
    out["timeout_sec"] = max(5.0, timeout)
    return out


def load_pve_clusters(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Return configured PVE clusters (new list first, else shim from ``proxmox:``)."""
    raw_list = cfg.get("pve_clusters")
    if isinstance(raw_list, list) and raw_list:
        out: list[dict[str, Any]] = []
        used: set[str] = set()
        for index, item in enumerate(raw_list):
            if not isinstance(item, dict):
                continue
            cid = _slugify_cluster_id(str(item.get("id") or ""), f"pve{index + 1}")
            base, n = cid, 2
            while cid in used:
                cid = f"{base}-{n}"
                n += 1
            used.add(cid)
            out.append(_copy_cluster_fields(item, cluster_id=cid))
        if out:
            return out

    legacy = cfg.get("proxmox")
    if isinstance(legacy, dict) and (
        str(legacy.get("host") or "").strip()
        or str(legacy.get("api_token_id") or "").strip()
        or str(legacy.get("user") or "").strip()
    ):
        return [_copy_cluster_fields(legacy, cluster_id=DEFAULT_CLUSTER_ID)]
    return []


def get_pve_cluster(cfg: dict[str, Any], cluster_id: str | None = None) -> dict[str, Any]:
    """Resolve one cluster by id (default: first / ``default``).

    Raises ``KeyError`` when no clusters are configured or id is unknown.
    """
    clusters = load_pve_clusters(cfg)
    if not clusters:
        raise KeyError("No Proxmox VE clusters configured (pve_clusters or proxmox)")
    want = (cluster_id or "").strip()
    if not want:
        # Prefer explicit "default", else first entry.
        for c in clusters:
            if c["id"] == DEFAULT_CLUSTER_ID:
                return c
        return clusters[0]
    for c in clusters:
        if c["id"] == want:
            return c
    raise KeyError(f"Unknown pve_cluster_id: {want}")


def default_pve_cluster_id(cfg: dict[str, Any]) -> str:
    clusters = load_pve_clusters(cfg)
    if not clusters:
        return DEFAULT_CLUSTER_ID
    for c in clusters:
        if c["id"] == DEFAULT_CLUSTER_ID:
            return DEFAULT_CLUSTER_ID
    return str(clusters[0]["id"])


def mirror_proxmox_from_clusters(cfg: dict[str, Any]) -> None:
    """Keep legacy ``proxmox:`` in sync with the default/first cluster (in-place)."""
    clusters = load_pve_clusters(cfg)
    if not clusters:
        return
    primary = None
    for c in clusters:
        if c["id"] == DEFAULT_CLUSTER_ID:
            primary = c
            break
    if primary is None:
        primary = clusters[0]
    # Drop id from the mirrored block — legacy shape has no id.
    mirrored = {k: v for k, v in primary.items() if k != "id"}
    cfg["proxmox"] = mirrored


def normalize_mount_cluster_id(raw: Any, *, default: str = DEFAULT_CLUSTER_ID) -> str:
    text = str(raw or "").strip()
    return text or default
