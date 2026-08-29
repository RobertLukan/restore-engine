"""Estimate PBS restore wire throughput via reader-protocol chunk sampling.

PVE task logs report logical ``read − zeroes`` (uncompressed). PBS transfers
compressed DataBlobs. We open a short-lived reader session, download each
``.fidx``, sample non-zero chunk digests, and measure raw download sizes to
get ``wire_bytes / logical_chunk_size``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import socket
import ssl
import struct
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from client_errors import public_error_message, tls_client_context
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, ResponseReceived, StreamEnded, StreamReset

from pbs_client import _fetch_ticket, _has_password, _has_token, _token_headers
from sources import Source, load_sources, source_by_id

log = logging.getLogger("pbs-wire")

READER_PROTO = "proxmox-backup-reader-protocol-v1"
FIDX_HEADER = 4096
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True)
class WireCompressionEstimate:
    """``ratio`` multiplies logical non-zero bytes/s to approximate wire bytes/s."""

    ratio: float
    samples: int
    chunk_size: int
    nonzero_positions: int
    unique_nonzero: int
    fidx_files: int
    total_positions: int = 0

    @property
    def sparsity_ratio(self) -> float:
        """Fraction of virtual disk chunks that are non-zero (0..1)."""
        if self.total_positions <= 0:
            return 1.0 if self.nonzero_positions > 0 else 0.0
        return max(0.0, min(1.0, self.nonzero_positions / float(self.total_positions)))


@dataclass(frozen=True)
class FidxUsageEstimate:
    """Logical disk usage from PBS fixed indexes (no chunk download)."""

    chunk_size: int
    fidx_files: int
    total_positions: int
    nonzero_positions: int
    unique_nonzero: int

    @property
    def sparsity_ratio(self) -> float:
        if self.total_positions <= 0:
            return 1.0 if self.nonzero_positions > 0 else 0.0
        return max(0.0, min(1.0, self.nonzero_positions / float(self.total_positions)))

    @property
    def virtual_bytes(self) -> int:
        return int(self.total_positions) * int(self.chunk_size)

    @property
    def nonzero_bytes(self) -> int:
        return int(self.nonzero_positions) * int(self.chunk_size)

    @property
    def zero_bytes(self) -> int:
        return max(0, self.virtual_bytes - self.nonzero_bytes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_size": self.chunk_size,
            "fidx_files": self.fidx_files,
            "total_positions": self.total_positions,
            "nonzero_positions": self.nonzero_positions,
            "unique_nonzero": self.unique_nonzero,
            "sparsity_ratio": self.sparsity_ratio,
            "virtual_bytes": self.virtual_bytes,
            "nonzero_bytes": self.nonzero_bytes,
            "zero_bytes": self.zero_bytes,
        }


def parse_job_backup_ref(backup_id: str) -> tuple[str, str, int]:
    """Return ``(source_id, pbs_backup_id, backup_time_epoch)`` from a job backup_id.

    Job ids look like ``main/idrija4tb/root|vm/109/2026-08-07T09:06:06Z``.
    """
    raw = (backup_id or "").strip()
    if "|" not in raw:
        raise ValueError(f"backup_id missing source|voltail separator: {backup_id!r}")
    source_id, voltail = raw.split("|", 1)
    source_id = source_id.strip()
    parts = [p for p in voltail.strip().strip("/").split("/") if p]
    if len(parts) < 3 or parts[0].lower() != "vm":
        raise ValueError(f"unexpected voltail in backup_id: {voltail!r}")
    pbs_backup_id = parts[1]
    iso = parts[2]
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = int(dt.timestamp())
    return source_id, pbs_backup_id, epoch


def _zero_digest(chunk_size: int) -> bytes:
    return hashlib.sha256(b"\x00" * chunk_size).digest()


def usage_from_digests(
    chunk_size: int,
    digests: list[bytes],
    *,
    fidx_files: int = 1,
) -> FidxUsageEstimate:
    """Build a usage estimate from already-parsed fidx digests (no I/O)."""
    zero = _zero_digest(chunk_size)
    total_positions = len(digests)
    nonzero = [d for d in digests if d != zero]
    unique = set(nonzero)
    return FidxUsageEstimate(
        chunk_size=chunk_size,
        fidx_files=fidx_files,
        total_positions=total_positions,
        nonzero_positions=len(nonzero),
        unique_nonzero=len(unique),
    )


def _parse_fidx(data: bytes) -> tuple[int, list[bytes]]:
    """Return ``(chunk_size, digests)`` from a fixed-index file body."""
    if len(data) < FIDX_HEADER + 32:
        raise ValueError(f"fidx too small ({len(data)} bytes)")
    # Layout (PBS FixedIndex header, 4096 bytes): digests follow.
    # Prefer inferring chunk size from image size field when present.
    # Common header stores u64 size at offset 64 and u64 chunk_size at 72 (LE),
    # but older/newer builds vary — fall back to image_size / n_chunks.
    n = (len(data) - FIDX_HEADER) // 32
    digests = [data[FIDX_HEADER + i * 32 : FIDX_HEADER + (i + 1) * 32] for i in range(n)]
    chunk_size = DEFAULT_CHUNK_SIZE
    try:
        # Try offset 72 (seen in practice: little-endian 0x400000).
        cand = struct.unpack_from("<Q", data, 72)[0]
        if cand in (64 * 1024, 256 * 1024, 1024 * 1024, 2 * 1024 * 1024, 4 * 1024 * 1024):
            chunk_size = int(cand)
        else:
            size64 = struct.unpack_from("<Q", data, 64)[0]
            if n > 0 and size64 % n == 0:
                inferred = size64 // n
                if inferred > 0:
                    chunk_size = int(inferred)
    except struct.error:
        pass
    return chunk_size, digests


def _auth_headers_cookies(source: Source) -> tuple[dict[str, str], dict[str, str]]:
    verify = bool(source.verify_ssl)
    with httpx.Client(timeout=30.0, verify=verify) as client:
        if _has_token(source):
            return _token_headers(source), {}
        if _has_password(source):
            return _fetch_ticket(client, source)
        raise ValueError(f"PBS source {source.source_id!r} has no auth configured")


def _list_fidx_names(source: Source, backup_id: str, backup_time: int, headers: dict[str, str], cookies: dict[str, str]) -> list[str]:
    params = {
        "backup-type": "vm",
        "backup-id": backup_id,
        "backup-time": str(backup_time),
    }
    # PBS API uses ``ns`` (same as snapshots list); ``namespace`` is rejected.
    ns = (source.namespace or "").strip().strip("/")
    if ns and ns.lower() != "root":
        params["ns"] = ns
    url = f"https://{source.host}:{int(source.port)}/api2/json/admin/datastore/{source.datastore}/files"
    with httpx.Client(timeout=60.0, verify=bool(source.verify_ssl)) as client:
        resp = client.get(url, headers=headers, cookies=cookies, params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"PBS files list failed: HTTP {resp.status_code} {resp.text[:200]}")
        rows = (resp.json() or {}).get("data") or []
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("filename") or row.get("file") or "").strip()
        if name.endswith(".img.fidx") or name.endswith(".fidx"):
            names.append(name)
    return names


class _ReaderSession:
    def __init__(self, sock: ssl.SSLSocket, leftover: bytes = b"") -> None:
        self.sock = sock
        self.conn = H2Connection(config=H2Configuration(client_side=True, header_encoding="utf-8"))
        self.conn.initiate_connection()
        self.sock.sendall(self.conn.data_to_send())
        if leftover:
            self.conn.receive_data(leftover)
            out = self.conn.data_to_send()
            if out:
                self.sock.sendall(out)

    def get(self, path: str, timeout_sec: float = 120.0) -> tuple[int, bytes]:
        sid = self.conn.get_next_available_stream_id()
        self.conn.send_headers(
            sid,
            [
                (":method", "GET"),
                (":path", path),
                (":scheme", "https"),
                (":authority", "localhost"),
                ("user-agent", "restore-engine/1.0"),
            ],
            end_stream=True,
        )
        self.sock.sendall(self.conn.data_to_send())
        body = bytearray()
        status: int | None = None
        done = False
        self.sock.settimeout(timeout_sec)
        while not done:
            data = self.sock.recv(256 * 1024)
            if not data:
                raise RuntimeError("PBS reader connection closed")
            for ev in self.conn.receive_data(data):
                if isinstance(ev, ResponseReceived) and ev.stream_id == sid:
                    hdrs = {
                        (k.decode() if isinstance(k, bytes) else k): (
                            v.decode() if isinstance(v, bytes) else v
                        )
                        for k, v in ev.headers
                    }
                    status = int(hdrs.get(":status", "0"))
                elif isinstance(ev, DataReceived) and ev.stream_id == sid:
                    body.extend(ev.data)
                    self.conn.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
                elif isinstance(ev, StreamEnded) and ev.stream_id == sid:
                    done = True
                elif isinstance(ev, StreamReset) and ev.stream_id == sid:
                    raise RuntimeError(f"PBS reader stream reset ({ev.error_code})")
            out = self.conn.data_to_send()
            if out:
                self.sock.sendall(out)
        return int(status or 0), bytes(body)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _open_reader(
    source: Source,
    backup_id: str,
    backup_time: int,
    headers: dict[str, str],
    cookies: dict[str, str],
) -> _ReaderSession:
    verify = bool(source.verify_ssl)
    ctx = tls_client_context(verify=verify)
    raw = socket.create_connection((source.host, int(source.port)), timeout=30)
    sock = ctx.wrap_socket(raw, server_hostname=source.host if verify else None)
    params: dict[str, str] = {
        "store": source.datastore,
        "backup-type": "vm",
        "backup-id": backup_id,
        "backup-time": str(backup_time),
    }
    # Reader upgrade uses ``ns`` like the REST snapshots/files APIs.
    ns = (source.namespace or "").strip().strip("/")
    if ns and ns.lower() != "root":
        params["ns"] = ns
    path_q = f"/api2/json/reader?{urllib.parse.urlencode(params)}"
    lines = [
        f"GET {path_q} HTTP/1.1",
        f"Host: {source.host}",
        "Connection: Upgrade",
        f"Upgrade: {READER_PROTO}",
    ]
    if "Authorization" in headers:
        lines.append(f"Authorization: {headers['Authorization']}")
    if cookies.get("PBSAuthCookie"):
        lines.append(f"Cookie: PBSAuthCookie={cookies['PBSAuthCookie']}")
    elif headers.get("Cookie"):
        lines.append(f"Cookie: {headers['Cookie']}")
    csrf = headers.get("CSRFPreventionToken")
    if csrf:
        lines.append(f"CSRFPreventionToken: {csrf}")
    lines.extend(["", ""])
    sock.sendall("\r\n".join(lines).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("EOF during PBS reader upgrade")
        buf += chunk
        if len(buf) > 65536:
            raise RuntimeError("PBS reader upgrade response too large")
    head, rest = buf.split(b"\r\n\r\n", 1)
    status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if "101" not in status_line:
        raise RuntimeError(f"PBS reader upgrade failed: {status_line}")
    return _ReaderSession(sock, leftover=rest)


def _load_all_fidx_digests(
    source: Source,
    *,
    pbs_backup_id: str,
    backup_time: int,
) -> tuple[int, list[bytes], int]:
    """Return ``(chunk_size, all_digests, fidx_file_count)`` via reader protocol."""
    headers, cookies = _auth_headers_cookies(source)
    fidx_names = _list_fidx_names(source, pbs_backup_id, backup_time, headers, cookies)
    if not fidx_names:
        raise RuntimeError("No .fidx files found for snapshot")
    session = _open_reader(source, pbs_backup_id, backup_time, headers, cookies)
    try:
        all_digests: list[bytes] = []
        chunk_size = DEFAULT_CHUNK_SIZE
        for name in fidx_names:
            path = f"/download?file-name={urllib.parse.quote(name)}"
            status, body = session.get(path)
            if status != 200 or len(body) < FIDX_HEADER + 32:
                raise RuntimeError(f"Failed to download {name} via reader: HTTP {status}")
            chunk_size, digests = _parse_fidx(body)
            all_digests.extend(digests)
        return chunk_size, all_digests, len(fidx_names)
    finally:
        session.close()


def estimate_fidx_usage(
    source: Source,
    *,
    pbs_backup_id: str,
    backup_time: int,
) -> FidxUsageEstimate:
    """Count zero vs non-zero fidx digests (no chunk payload downloads)."""
    chunk_size, all_digests, fidx_files = _load_all_fidx_digests(
        source, pbs_backup_id=pbs_backup_id, backup_time=backup_time
    )
    return usage_from_digests(chunk_size, all_digests, fidx_files=fidx_files)


def fidx_usage_cache_key(source_id: str, pbs_backup_id: str, backup_time: int) -> str:
    return f"restore:fidxusage:{source_id}|{pbs_backup_id}|{int(backup_time)}"


def estimate_fidx_usage_cached(
    r: Any,
    cfg: dict[str, Any],
    backup_id: str,
) -> dict[str, Any]:
    """Return usage dict for a job-style ``backup_id``, using Redis cache when possible."""
    source_id, pbs_id, epoch = parse_job_backup_ref(backup_id)
    key = fidx_usage_cache_key(source_id, pbs_id, epoch)
    try:
        cached = r.get(key)
        if cached:
            data = json.loads(cached)
            if isinstance(data, dict) and "nonzero_bytes" in data:
                return {**data, "cached": True, "backup_id": backup_id}
    except Exception:
        pass

    source = source_by_id(cfg, source_id)
    if source is None:
        for src in load_sources(cfg):
            if backup_id.startswith(src.source_id + "|"):
                source = src
                break
    if source is None:
        raise RuntimeError(f"No PBS source for backup_id={backup_id}")

    est = estimate_fidx_usage(source, pbs_backup_id=pbs_id, backup_time=epoch)
    payload = est.as_dict()
    payload["backup_id"] = backup_id
    payload["cached"] = False
    try:
        # Snapshots are immutable — keep indefinitely (no TTL).
        r.set(key, json.dumps(payload, separators=(",", ":")))
    except Exception:
        log.warning("Failed to cache fidx usage for %s", backup_id)
    return payload


def estimate_wire_compression(
    source: Source,
    *,
    pbs_backup_id: str,
    backup_time: int,
    sample_size: int = 24,
    rng_seed: int | None = 1,
) -> WireCompressionEstimate:
    """Sample chunk downloads and return wire/logical compression ratio.

    Uses one reader session for both ``.fidx`` download and chunk samples
    (PBS may only allow a single reader per snapshot).
    """
    headers, cookies = _auth_headers_cookies(source)
    fidx_names = _list_fidx_names(source, pbs_backup_id, backup_time, headers, cookies)
    if not fidx_names:
        raise RuntimeError("No .fidx files found for snapshot")

    session = _open_reader(source, pbs_backup_id, backup_time, headers, cookies)
    try:
        all_digests: list[bytes] = []
        chunk_size = DEFAULT_CHUNK_SIZE
        for name in fidx_names:
            path = f"/download?file-name={urllib.parse.quote(name)}"
            status, body = session.get(path)
            if status != 200 or len(body) < FIDX_HEADER + 32:
                raise RuntimeError(f"Failed to download {name} via reader: HTTP {status}")
            chunk_size, digests = _parse_fidx(body)
            all_digests.extend(digests)

        zero = _zero_digest(chunk_size)
        total_positions = len(all_digests)
        nonzero = [d for d in all_digests if d != zero]
        unique = list(set(nonzero))
        fidx_files = len(fidx_names)
        if not unique:
            return WireCompressionEstimate(
                ratio=0.0,
                samples=0,
                chunk_size=chunk_size,
                nonzero_positions=0,
                unique_nonzero=0,
                fidx_files=fidx_files,
                total_positions=total_positions,
            )

        rng = random.Random(rng_seed)
        sample = unique if len(unique) <= sample_size else rng.sample(unique, sample_size)
        ratios: list[float] = []
        for digest in sample:
            status, body = session.get(f"/chunk?digest={digest.hex()}")
            if status != 200 or not body:
                log.warning("chunk sample HTTP %s len=%s", status, len(body))
                continue
            ratios.append(len(body) / float(chunk_size))
        if not ratios:
            raise RuntimeError("No successful chunk samples for compression estimate")

        mean_comp = sum(ratios) / len(ratios)
        dedup = len(unique) / max(1, len(nonzero))
        ratio = max(0.0, min(1.0, mean_comp * dedup))
        return WireCompressionEstimate(
            ratio=ratio,
            samples=len(ratios),
            chunk_size=chunk_size,
            nonzero_positions=len(nonzero),
            unique_nonzero=len(unique),
            fidx_files=fidx_files,
            total_positions=total_positions,
        )
    finally:
        session.close()


def estimate_wire_compression_for_job(
    cfg: dict[str, Any],
    backup_id: str,
    *,
    sample_size: int = 24,
) -> WireCompressionEstimate | None:
    """Best-effort estimate; returns None on failure (caller keeps logical rates)."""
    try:
        source_id, pbs_id, epoch = parse_job_backup_ref(backup_id)
        source = source_by_id(cfg, source_id)
        if source is None:
            # Fallback: match by prefix if ids drifted.
            for src in load_sources(cfg):
                if backup_id.startswith(src.source_id + "|"):
                    source = src
                    break
        if source is None:
            log.warning("No PBS source for backup_id=%s", backup_id)
            return None
        return estimate_wire_compression(
            source,
            pbs_backup_id=pbs_id,
            backup_time=epoch,
            sample_size=sample_size,
        )
    except Exception:
        log.exception("Wire compression estimate failed for %s", backup_id)
        return None
