"""Dashboard auth, credential editing, and connectivity tests."""

from __future__ import annotations

import copy
import secrets
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from pbs_client import list_vm_backups, test_pbs_connection
from pve_client import connect_proxmox, list_node_storages, test_proxmox_connection

CONFIG_PATH: Path | None = None
MASK = "********"
SESSION_CREDENTIALS_VERIFIED = "credentials_verified"


def _config_path() -> Path:
    if CONFIG_PATH is None:
        raise RuntimeError("ui.CONFIG_PATH not set")
    return CONFIG_PATH


def load_yaml() -> dict[str, Any]:
    with _config_path().open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_yaml(cfg: dict[str, Any]) -> None:
    with _config_path().open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120)


def require_ui_session(request: Request) -> None:
    if not request.session.get("ui_authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")


def _mount_view(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "datastore": str(m.get("datastore", "")),
        "namespace": str(m.get("namespace", "") or ""),
        "pve_storage": str(m.get("pve_storage", "")),
    }


def mask_pbs_server(s: dict[str, Any]) -> dict[str, Any]:
    tok = bool((s.get("api_token_secret") or "").strip())
    return {
        "id": str(s.get("id", "")),
        "host": s.get("host", ""),
        "port": int(s.get("port", 8007) or 8007),
        "verify_ssl": bool(s.get("verify_ssl", True)),
        "api_token_id": s.get("api_token_id", ""),
        "api_token_secret_set": tok,
        "api_token_secret": MASK if tok else "",
        "mounts": [_mount_view(m) for m in (s.get("mounts") or []) if isinstance(m, dict)],
    }


def pbs_servers_view(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Return masked PBS servers, converting a legacy single ``pbs`` block if needed."""
    servers = cfg.get("pbs_servers")
    if isinstance(servers, list) and servers:
        return [mask_pbs_server(s) for s in servers if isinstance(s, dict)]
    legacy = cfg.get("pbs") or {}
    if legacy.get("host") or legacy.get("datastore"):
        pve_storage = str((cfg.get("proxmox") or {}).get("pbs_storage") or "")
        return [
            mask_pbs_server(
                {
                    "id": "pbs",
                    "host": legacy.get("host", ""),
                    "port": legacy.get("port", 8007),
                    "verify_ssl": legacy.get("verify_ssl", True),
                    "api_token_id": legacy.get("api_token_id", ""),
                    "api_token_secret": legacy.get("api_token_secret", ""),
                    "mounts": [
                        {"datastore": legacy.get("datastore", ""), "namespace": "", "pve_storage": pve_storage}
                    ],
                }
            )
        ]
    return []


def mask_proxmox(p: dict[str, Any]) -> dict[str, Any]:
    tok = bool((p.get("api_token_secret") or "").strip())
    pwd = bool((p.get("password") or "").strip())
    return {
        "host": p.get("host", ""),
        "port": int(p.get("port", 8006)),
        "verify_ssl": bool(p.get("verify_ssl", True)),
        "default_node": p.get("default_node", ""),
        "api_token_id": p.get("api_token_id", ""),
        "storage": p.get("storage", ""),
        "restore_bwlimit": int(p.get("restore_bwlimit", 0) or 0),
        "live_restore_default": bool(p.get("live_restore_default", False)),
        "api_token_secret_set": tok,
        "api_token_secret": MASK if tok else "",
        "password_set": pwd,
        "password": MASK if pwd else "",
        "user": p.get("user", ""),
    }


def view_worker(w: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_concurrent_restores": int(w.get("max_concurrent_restores", 2) or 2),
        "task_poll_interval_sec": int(w.get("task_poll_interval_sec", 3) or 3),
    }


class LoginBody(BaseModel):
    password: str = Field(..., min_length=1)


class PBSMountModel(BaseModel):
    datastore: str = ""
    namespace: str = ""
    pve_storage: str = ""


class PBSServerModel(BaseModel):
    id: str = ""
    host: str = ""
    port: int = 8007
    verify_ssl: bool = True
    api_token_id: str = ""
    api_token_secret: str = ""
    mounts: list[PBSMountModel] = Field(default_factory=list)


class ProxmoxPartial(BaseModel):
    host: str | None = None
    port: int | None = None
    verify_ssl: bool | None = None
    default_node: str | None = None
    api_token_id: str | None = None
    api_token_secret: str | None = None
    user: str | None = None
    password: str | None = None
    storage: str | None = None
    restore_bwlimit: int | None = None
    live_restore_default: bool | None = None


class WorkerPartial(BaseModel):
    max_concurrent_restores: int | None = None
    task_poll_interval_sec: int | None = None


class CredentialsPut(BaseModel):
    pbs_servers: list[PBSServerModel] | None = None
    proxmox: ProxmoxPartial | None = None
    worker: WorkerPartial | None = None


class ProxmoxTestOverrides(BaseModel):
    host: str | None = None
    port: int | None = None
    verify_ssl: bool | None = None
    api_token_id: str | None = None
    api_token_secret: str | None = None
    user: str | None = None
    password: str | None = None
    storage: str | None = None


router = APIRouter(tags=["ui"])


def _merge_partial(target: dict[str, Any], partial: BaseModel | None, secret_fields: set[str]) -> None:
    if not partial:
        return
    for key, value in partial.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        if key in secret_fields and value == MASK:
            continue
        target[key] = value


@router.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, bool]:
    authed = bool(request.session.get("ui_authenticated"))
    return {
        "authenticated": authed,
        "credentials_verified": bool(request.session.get(SESSION_CREDENTIALS_VERIFIED)) if authed else False,
    }


@router.post("/api/auth/login")
def auth_login(request: Request, body: LoginBody) -> dict[str, str]:
    cfg = load_yaml()
    expected = (cfg.get("ui") or {}).get("password") or ""
    if not expected:
        raise HTTPException(status_code=503, detail="ui.password is not set in config.yaml")
    ok = secrets.compare_digest(body.password.encode("utf-8"), expected.encode("utf-8"))
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid password")
    request.session["ui_authenticated"] = True
    return {"ok": "true"}


@router.post("/api/auth/logout", dependencies=[Depends(require_ui_session)])
def auth_logout(request: Request) -> dict[str, str]:
    request.session.clear()
    return {"ok": "true"}


@router.get("/api/ui/credentials", dependencies=[Depends(require_ui_session)])
def get_credentials() -> dict[str, Any]:
    cfg = load_yaml()
    return {
        "pbs_servers": pbs_servers_view(cfg),
        "proxmox": mask_proxmox(cfg.get("proxmox") or {}),
        "worker": view_worker(cfg.get("worker") or {}),
    }


def _slugify_server_id(value: str, fallback: str) -> str:
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in value.strip().lower()).strip("-")
    return slug or fallback


def _persist_servers(existing: list[dict[str, Any]], incoming: list[PBSServerModel]) -> list[dict[str, Any]]:
    """Build the servers list to save, preserving secrets that come back masked/blank."""
    by_id = {str(s.get("id", "")): s for s in existing if isinstance(s, dict)}
    used_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, server in enumerate(incoming):
        server_id = _slugify_server_id(server.id or server.host, f"pbs{index + 1}")
        base_id, n = server_id, 2
        while server_id in used_ids:
            server_id = f"{base_id}-{n}"
            n += 1
        used_ids.add(server_id)

        secret = (server.api_token_secret or "").strip()
        if secret in ("", MASK):
            secret = str((by_id.get(server.id) or {}).get("api_token_secret", ""))

        out.append(
            {
                "id": server_id,
                "host": server.host.strip(),
                "port": int(server.port or 8007),
                "verify_ssl": bool(server.verify_ssl),
                "api_token_id": server.api_token_id.strip(),
                "api_token_secret": secret,
                "mounts": [
                    {
                        "datastore": m.datastore.strip(),
                        "namespace": (m.namespace or "").strip().strip("/"),
                        "pve_storage": m.pve_storage.strip(),
                    }
                    for m in server.mounts
                    if m.datastore.strip() and m.pve_storage.strip()
                ],
            }
        )
    return out


@router.put("/api/ui/credentials", dependencies=[Depends(require_ui_session)])
def put_credentials(body: CredentialsPut) -> dict[str, str]:
    cfg = load_yaml()
    if body.pbs_servers is not None:
        existing = cfg.get("pbs_servers") if isinstance(cfg.get("pbs_servers"), list) else []
        cfg["pbs_servers"] = _persist_servers(existing, body.pbs_servers)
        # New schema is now authoritative; drop legacy single-PBS keys.
        cfg.pop("pbs", None)
        if isinstance(cfg.get("proxmox"), dict):
            cfg["proxmox"].pop("pbs_storage", None)
    if body.proxmox:
        px = cfg.setdefault("proxmox", {})
        _merge_partial(px, body.proxmox, {"api_token_secret", "password"})
    if body.worker:
        wk = cfg.setdefault("worker", {})
        _merge_partial(wk, body.worker, set())
        if "max_concurrent_restores" in wk:
            wk["max_concurrent_restores"] = max(1, int(wk["max_concurrent_restores"]))
    save_yaml(cfg)
    return {"ok": "true"}


@router.post("/api/ui/test/pbs", dependencies=[Depends(require_ui_session)])
def test_pbs(body: PBSServerModel) -> dict[str, Any]:
    """Test a single PBS server. A masked/blank secret falls back to the saved one."""
    saved = load_yaml().get("pbs_servers")
    saved_by_id = {str(s.get("id", "")): s for s in saved if isinstance(s, dict)} if isinstance(saved, list) else {}
    secret = (body.api_token_secret or "").strip()
    if secret in ("", MASK):
        secret = str((saved_by_id.get(body.id) or {}).get("api_token_secret", ""))
    probe_cfg = {
        "pbs_servers": [
            {
                "id": body.id or "probe",
                "host": body.host.strip(),
                "port": int(body.port or 8007),
                "verify_ssl": bool(body.verify_ssl),
                "api_token_id": body.api_token_id.strip(),
                "api_token_secret": secret,
                "mounts": [
                    {"datastore": m.datastore.strip(), "namespace": (m.namespace or "").strip().strip("/"),
                     "pve_storage": m.pve_storage.strip()}
                    for m in body.mounts
                ]
                or [{"datastore": "probe", "namespace": "", "pve_storage": "probe"}],
            }
        ]
    }
    ok, message = test_pbs_connection(probe_cfg)
    return {"ok": ok, "message": message}


@router.post("/api/ui/test/proxmox", dependencies=[Depends(require_ui_session)])
def test_proxmox(body: ProxmoxTestOverrides | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(load_yaml())
    px = cfg.setdefault("proxmox", {})
    _merge_partial(px, body, {"api_token_secret", "password"})
    ok, message = test_proxmox_connection(cfg)
    return {"ok": ok, "message": message}


def _verify_saved(cfg: dict[str, Any]) -> tuple[bool, str, str]:
    pbs_ok, pbs_msg = test_pbs_connection(cfg)
    pve_ok, pve_msg = test_proxmox_connection(cfg)
    if pbs_ok and pve_ok:
        return True, pbs_msg, pve_msg
    return False, pbs_msg, pve_msg


@router.post("/api/auth/complete-credentials-check", dependencies=[Depends(require_ui_session)])
def complete_credentials_check(request: Request) -> dict[str, Any]:
    cfg = load_yaml()
    ok, pbs_msg, pve_msg = _verify_saved(cfg)
    if ok:
        request.session[SESSION_CREDENTIALS_VERIFIED] = True
        return {"ok": True, "pbs": pbs_msg, "proxmox": pve_msg}
    request.session.pop(SESSION_CREDENTIALS_VERIFIED, None)
    return {"ok": False, "pbs_message": pbs_msg, "proxmox_message": pve_msg}


@router.post("/api/auth/reopen-credentials", dependencies=[Depends(require_ui_session)])
def reopen_credentials(request: Request) -> dict[str, str]:
    request.session[SESSION_CREDENTIALS_VERIFIED] = False
    return {"ok": "true"}


@router.get("/api/ui/proxmox-storages", dependencies=[Depends(require_ui_session)])
def proxmox_storages(node: str) -> dict[str, Any]:
    cfg = load_yaml()
    proxmox = connect_proxmox(cfg)
    return {"node": node, "storages": list_node_storages(proxmox, node)}


def health_pbs_component(cfg: dict[str, Any]) -> dict[str, Any]:
    ok, msg = test_pbs_connection(cfg)
    return {"ok": ok, "detail": msg}


def health_proxmox_component(cfg: dict[str, Any]) -> dict[str, Any]:
    ok, msg = test_proxmox_connection(cfg)
    return {"ok": ok, "detail": msg}
