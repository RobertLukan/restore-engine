"""Backup source model: flatten configured PBS servers into restore sources.

A "source" is one place restores can come from. Because Proxmox VE binds a single
PBS datastore *and* a single namespace per storage definition, the unit that
matters for restore is the triple ``(pbs server, datastore, namespace)`` mapped to
the PVE storage ID that references it. Each such triple is one ``Source``.

Config schema (new)::

    pbs_servers:
      - id: main
        host: 10.0.0.10
        port: 8007
        verify_ssl: false
        # Prefer API token when set; otherwise user/password ticket auth.
        api_token_id: root@pam!restore
        api_token_secret: SECRET
        user: ""
        password: ""
        mounts:
          - datastore: main
            namespace: ""          # root namespace
            pve_storage: pbs-main
          - datastore: main
            namespace: team-a
            pve_storage: pbs-main-teamA

Legacy schema (still supported) is a single ``pbs:`` block plus
``proxmox.pbs_storage``; it is converted into one server with one root mount.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Source:
    source_id: str          # unique: "{server_id}/{datastore}/{namespace-or-root}"
    server_id: str
    label: str              # human label for the UI
    host: str
    port: int
    verify_ssl: bool
    api_token_id: str
    api_token_secret: str
    user: str
    password: str
    datastore: str
    namespace: str          # "" == root namespace
    pve_storage: str        # PVE storage ID bound to (datastore, namespace)


def _norm_ns(value: Any) -> str:
    ns = str(value or "").strip().strip("/")
    return ns


def _make_source_id(server_id: str, datastore: str, namespace: str) -> str:
    return f"{server_id}/{datastore}/{namespace or 'root'}"


def _source_label(server_id: str, datastore: str, namespace: str) -> str:
    ns_part = f" [{namespace}]" if namespace else ""
    return f"{server_id} · {datastore}{ns_part}"


def _sources_from_server(server: dict[str, Any], index: int) -> list[Source]:
    server_id = str(server.get("id") or f"pbs{index + 1}").strip() or f"pbs{index + 1}"
    host = str(server.get("host") or "").strip()
    port = int(server.get("port", 8007) or 8007)
    verify_ssl = bool(server.get("verify_ssl", True))
    token_id = str(server.get("api_token_id") or "").strip()
    token_secret = str(server.get("api_token_secret") or "").strip()
    user = str(server.get("user") or "").strip()
    password = str(server.get("password") or "").strip()
    out: list[Source] = []
    for mount in server.get("mounts") or []:
        if not isinstance(mount, dict):
            continue
        datastore = str(mount.get("datastore") or "").strip()
        pve_storage = str(mount.get("pve_storage") or "").strip()
        if not datastore or not pve_storage:
            continue
        namespace = _norm_ns(mount.get("namespace"))
        out.append(
            Source(
                source_id=_make_source_id(server_id, datastore, namespace),
                server_id=server_id,
                label=_source_label(server_id, datastore, namespace),
                host=host,
                port=port,
                verify_ssl=verify_ssl,
                api_token_id=token_id,
                api_token_secret=token_secret,
                user=user,
                password=password,
                datastore=datastore,
                namespace=namespace,
                pve_storage=pve_storage,
            )
        )
    return out


def _legacy_sources(cfg: dict[str, Any]) -> list[Source]:
    pbs = cfg.get("pbs") or {}
    host = str(pbs.get("host") or "").strip()
    datastore = str(pbs.get("datastore") or "").strip()
    pve_storage = str((cfg.get("proxmox") or {}).get("pbs_storage") or "").strip()
    if not host and not datastore:
        return []
    server = {
        "id": "pbs",
        "host": host,
        "port": int(pbs.get("port", 8007) or 8007),
        "verify_ssl": bool(pbs.get("verify_ssl", True)),
        "api_token_id": pbs.get("api_token_id", ""),
        "api_token_secret": pbs.get("api_token_secret", ""),
        "user": pbs.get("user", ""),
        "password": pbs.get("password", ""),
        "mounts": [{"datastore": datastore, "namespace": "", "pve_storage": pve_storage}],
    }
    return _sources_from_server(server, 0)


def load_sources(cfg: dict[str, Any]) -> list[Source]:
    """Return all configured restore sources (new schema first, else legacy)."""
    servers = cfg.get("pbs_servers")
    if isinstance(servers, list) and servers:
        out: list[Source] = []
        for index, server in enumerate(servers):
            if isinstance(server, dict):
                out.extend(_sources_from_server(server, index))
        if out:
            return out
    return _legacy_sources(cfg)


def source_by_id(cfg: dict[str, Any], source_id: str) -> Source | None:
    for src in load_sources(cfg):
        if src.source_id == source_id:
            return src
    return None
