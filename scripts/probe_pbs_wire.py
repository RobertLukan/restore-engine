#!/usr/bin/env python3
"""Probe PBS reader protocol: sample chunk sizes → compression ratio."""

from __future__ import annotations

import hashlib
import http.client
import json
import random
import socket
import ssl
import sys
import urllib.parse
from pathlib import Path

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    ResponseReceived,
    SettingsAcknowledged,
    StreamEnded,
    StreamReset,
)

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


CHUNK_SIZE = 4 * 1024 * 1024
ZERO_DIGEST = hashlib.sha256(b"\x00" * CHUNK_SIZE).digest()
READER_PROTO = "proxmox-backup-reader-protocol-v1"
FIDX_HEADER = 4096


def _recv_exact(sock: ssl.SSLSocket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("socket closed")
        buf.extend(chunk)
    return bytes(buf)


def _http11_upgrade(
    sock: ssl.SSLSocket,
    *,
    host: str,
    path_q: str,
    headers: dict[str, str],
) -> bytes:
    lines = [
        f"GET {path_q} HTTP/1.1",
        f"Host: {host}",
        "Connection: Upgrade",
        f"Upgrade: {READER_PROTO}",
    ]
    for k, v in headers.items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("")
    sock.sendall("\r\n".join(lines).encode())

    # Read status line + headers until blank line
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("EOF during upgrade")
        data += chunk
        if len(data) > 65536:
            raise RuntimeError("upgrade response too large")
    head, rest = data.split(b"\r\n\r\n", 1)
    status = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if "101" not in status:
        raise RuntimeError(f"upgrade failed: {status} / {head[:300]!r}")
    return rest


def _h2_download(sock: ssl.SSLSocket, path: str, leftover: bytes = b"") -> tuple[int, bytes]:
    config = H2Configuration(client_side=True, header_encoding="utf-8")
    conn = H2Connection(config=config)
    conn.initiate_connection()
    # Large windows like PBS client
    conn.update_settings({
        0x4: 4 * 1024 * 1024,  # MAX_FRAME_SIZE
    })
    to_send = conn.data_to_send()
    if to_send:
        sock.sendall(to_send)

    stream_id = conn.get_next_available_stream_id()
    conn.send_headers(
        stream_id,
        [
            (":method", "GET"),
            (":path", path),
            (":scheme", "https"),
            (":authority", "localhost"),
            ("user-agent", "restore-engine-wire-probe/1.0"),
        ],
        end_stream=True,
    )
    sock.sendall(conn.data_to_send())

    body = bytearray()
    status = None
    pending = leftover

    while True:
        if pending:
            data, pending = pending, b""
        else:
            data = sock.recv(65536)
            if not data:
                break
        events = conn.receive_data(data)
        for event in events:
            if isinstance(event, ResponseReceived):
                headers = dict(event.headers)
                status = int(headers.get(b":status", headers.get(":status", b"0")))
            elif isinstance(event, DataReceived):
                body.extend(event.data)
                conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, StreamEnded):
                sock.sendall(conn.data_to_send())
                return int(status or 0), bytes(body)
            elif isinstance(event, StreamReset):
                raise RuntimeError(f"stream reset: {event.error_code}")
            elif isinstance(event, ConnectionTerminated):
                raise RuntimeError(f"connection terminated: {event.error_code}")
        out = conn.data_to_send()
        if out:
            sock.sendall(out)

    raise RuntimeError(f"incomplete response status={status} body={len(body)}")


def login(host: str, port: int, user: str, password: str, verify: bool) -> dict[str, str]:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=60)
    body = urllib.parse.urlencode({"username": user, "password": password})
    conn.request(
        "POST",
        "/api2/json/access/ticket",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = conn.getresponse()
    raw = resp.read()
    if resp.status != 200:
        raise RuntimeError(f"login HTTP {resp.status}: {raw[:200]!r}")
    data = json.loads(raw)["data"]
    return {
        "Cookie": f"PBSAuthCookie={data['ticket']}",
        "CSRFPreventionToken": data["CSRFPreventionToken"],
    }


def download_fidx(
    host: str,
    port: int,
    datastore: str,
    backup_id: str,
    backup_time: int,
    file_name: str,
    headers: dict[str, str],
    verify: bool,
) -> bytes:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    q = urllib.parse.urlencode(
        {
            "backup-type": "vm",
            "backup-id": backup_id,
            "backup-time": str(backup_time),
            "file-name": file_name,
        }
    )
    path = f"/api2/json/admin/datastore/{datastore}/download?{q}"
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=120)
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    if resp.status != 200:
        raise RuntimeError(f"fidx download HTTP {resp.status}: {data[:200]!r}")
    return data


def main() -> int:
    cfg_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/config.docker.yaml")
    cfg = yaml.safe_load(cfg_path.read_text())
    pbs = cfg["pbs_servers"][0]
    host = pbs["host"]
    port = int(pbs.get("port") or 8007)
    user = pbs["user"]
    password = pbs["password"]
    verify = bool(pbs.get("verify_ssl", False))
    mount = pbs["mounts"][0]
    datastore = mount["datastore"]
    backup_id = "109"
    backup_time = 1786093566
    file_name = "drive-scsi0.img.fidx"

    headers = login(host, port, user, password, verify)
    print("login ok")
    fidx = download_fidx(
        host, port, datastore, backup_id, backup_time, file_name, headers, verify
    )
    print("fidx bytes", len(fidx))
    digests = [
        fidx[FIDX_HEADER + i * 32 : FIDX_HEADER + (i + 1) * 32]
        for i in range((len(fidx) - FIDX_HEADER) // 32)
    ]
    nonzero = [d for d in digests if d != ZERO_DIGEST]
    uniq = list(set(nonzero))
    print("nonzero", len(nonzero), "unique", len(uniq))
    random.seed(1)
    sample = random.sample(uniq, min(20, len(uniq)))

    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((host, port), timeout=60)
    sock = ctx.wrap_socket(raw, server_hostname=host if verify else None)

    q = urllib.parse.urlencode(
        {
            "store": datastore,
            "backup-type": "vm",
            "backup-id": backup_id,
            "backup-time": str(backup_time),
        }
    )
    leftover = _http11_upgrade(
        sock,
        host=host,
        path_q=f"/api2/json/reader?{q}",
        headers=headers,
    )
    print("upgraded, leftover", len(leftover))

    # Re-init H2 once and download multiple chunks on same connection
    config = H2Configuration(client_side=True, header_encoding="utf-8")
    conn = H2Connection(config=config)
    conn.initiate_connection()
    sock.sendall(conn.data_to_send())
    if leftover:
        for event in conn.receive_data(leftover):
            pass
        out = conn.data_to_send()
        if out:
            sock.sendall(out)

    ratios = []
    for d in sample:
        hx = d.hex()
        path = f"/chunk?digest={hx}"
        stream_id = conn.get_next_available_stream_id()
        conn.send_headers(
            stream_id,
            [
                (":method", "GET"),
                (":path", path),
                (":scheme", "https"),
                (":authority", "localhost"),
                ("user-agent", "restore-engine-wire-probe/1.0"),
            ],
            end_stream=True,
        )
        sock.sendall(conn.data_to_send())
        body = bytearray()
        status = None
        done = False
        while not done:
            data = sock.recv(65536)
            if not data:
                raise RuntimeError("EOF")
            for event in conn.receive_data(data):
                if isinstance(event, ResponseReceived) and event.stream_id == stream_id:
                    hdrs = { (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in event.headers }
                    status = int(hdrs.get(":status", "0"))
                elif isinstance(event, DataReceived) and event.stream_id == stream_id:
                    body.extend(event.data)
                    conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, StreamEnded) and event.stream_id == stream_id:
                    done = True
                elif isinstance(event, StreamReset) and event.stream_id == stream_id:
                    raise RuntimeError(f"reset {event.error_code}")
            out = conn.data_to_send()
            if out:
                sock.sendall(out)
        # DataBlob has a small header; wire size ≈ len(body)
        ratio = len(body) / CHUNK_SIZE
        ratios.append(ratio)
        print(f"chunk {hx[:12]}… HTTP {status} wire={len(body)} ratio={ratio:.3f}")

    sock.close()
    mean = sum(ratios) / len(ratios)
    print(f"mean compression ratio (wire/raw) = {mean:.4f}")
    useful = len(nonzero) * CHUNK_SIZE
    print(f"est wire total GiB = {useful * mean / 1024**3:.3f}")
    print(f"est wire rate @249.58s = {useful * mean / 249.58 / 1e6:.2f} MB/s ({useful * mean / 249.58 * 8 / 1e6:.1f} Mbit/s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
