#!/usr/bin/env python3
"""Download fidx via reader H2, then sample chunks."""

from __future__ import annotations

import hashlib
import http.client
import json
import random
import socket
import ssl
import urllib.parse
from pathlib import Path

import yaml
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, ResponseReceived, StreamEnded, StreamReset

CHUNK = 4 * 1024 * 1024
ZERO = hashlib.sha256(b"\x00" * CHUNK).digest()
PROTO = "proxmox-backup-reader-protocol-v1"
FIDX_HEADER = 4096


def h2_get(conn: H2Connection, sock: ssl.SSLSocket, path: str, extra_headers: list | None = None) -> tuple[int, bytes]:
    sid = conn.get_next_available_stream_id()
    headers = [
        (":method", "GET"),
        (":path", path),
        (":scheme", "https"),
        (":authority", "localhost"),
        ("user-agent", "restore-engine/1.0"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    conn.send_headers(sid, headers, end_stream=True)
    sock.sendall(conn.data_to_send())
    body = bytearray()
    status = None
    done = False
    while not done:
        data = sock.recv(65536)
        if not data:
            raise RuntimeError("EOF")
        for ev in conn.receive_data(data):
            if isinstance(ev, ResponseReceived) and ev.stream_id == sid:
                h = {
                    (k.decode() if isinstance(k, bytes) else k): (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in ev.headers
                }
                status = int(h.get(":status", "0"))
            elif isinstance(ev, DataReceived) and ev.stream_id == sid:
                body.extend(ev.data)
                conn.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
            elif isinstance(ev, StreamEnded) and ev.stream_id == sid:
                done = True
            elif isinstance(ev, StreamReset) and ev.stream_id == sid:
                raise RuntimeError(f"reset {ev.error_code}")
        out = conn.data_to_send()
        if out:
            sock.sendall(out)
    return int(status or 0), bytes(body)


def main() -> None:
    cfg = yaml.safe_load(Path("/app/config.docker.yaml").read_text())
    pbs = cfg["pbs_servers"][0]
    host = pbs["host"]
    port = int(pbs.get("port") or 8007)
    user = pbs["user"]
    pw = pbs["password"]
    datastore = pbs["mounts"][0]["datastore"]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    bt = 1786093566
    file_name = "drive-scsi0.img.fidx"

    c = http.client.HTTPSConnection(host, port, context=ctx, timeout=60)
    c.request(
        "POST",
        "/api2/json/access/ticket",
        urllib.parse.urlencode({"username": user, "password": pw}),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    ticket = json.loads(c.getresponse().read())["data"]
    cookie = f"PBSAuthCookie={ticket['ticket']}"
    csrf = ticket["CSRFPreventionToken"]

    raw = socket.create_connection((host, port), timeout=60)
    sock = ctx.wrap_socket(raw, server_hostname=None)
    rq = urllib.parse.urlencode(
        {
            "store": datastore,
            "backup-type": "vm",
            "backup-id": "109",
            "backup-time": str(bt),
        }
    )
    req = (
        f"GET /api2/json/reader?{rq} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Connection: Upgrade\r\n"
        f"Upgrade: {PROTO}\r\n"
        f"Cookie: {cookie}\r\n"
        f"CSRFPreventionToken: {csrf}\r\n"
        f"\r\n"
    ).encode()
    sock.sendall(req)
    data = b""
    while b"\r\n\r\n" not in data:
        data += sock.recv(4096)
    head, rest = data.split(b"\r\n\r\n", 1)
    print("upgrade", head.split(b"\r\n")[0].decode())

    conn = H2Connection(config=H2Configuration(client_side=True, header_encoding="utf-8"))
    conn.initiate_connection()
    # match PBS large frames
    sock.sendall(conn.data_to_send())
    if rest:
        conn.receive_data(rest)
        out = conn.data_to_send()
        if out:
            sock.sendall(out)

    # Download index via reader so digests are allowed
    for path in [
        f"/download?file-name={urllib.parse.quote(file_name)}",
        f"/download?file-name={file_name}",
    ]:
        st, body = h2_get(conn, sock, path)
        print(f"download {path}: status={st} len={len(body)}")
        if st == 200 and len(body) > FIDX_HEADER:
            fidx = body
            break
    else:
        raise SystemExit("could not download fidx via reader")

    digests = [
        fidx[FIDX_HEADER + i * 32 : FIDX_HEADER + (i + 1) * 32]
        for i in range((len(fidx) - FIDX_HEADER) // 32)
    ]
    uniq = [d for d in set(digests) if d != ZERO]
    print("unique nonzero", len(uniq))
    random.seed(2)
    sample = random.sample(uniq, min(15, len(uniq)))

    ratios = []
    for d in sample:
        hx = d.hex()
        st, body = h2_get(conn, sock, f"/chunk?digest={hx}")
        print(f"chunk {hx[:12]} status={st} wire={len(body)}")
        if st == 200 and len(body) > 0:
            ratios.append(len(body) / CHUNK)
        else:
            print("  body", body[:120])

    sock.close()
    if ratios:
        mean = sum(ratios) / len(ratios)
        print(f"mean wire/raw ratio={mean:.4f} n={len(ratios)}")
        useful = sum(1 for d in digests if d != ZERO) * CHUNK
        print(
            f"est @249.58s: {useful * mean / 249.58 / 1e6:.2f} MB/s "
            f"({useful * mean / 249.58 * 8 / 1e6:.1f} Mbit/s)"
        )


if __name__ == "__main__":
    main()
