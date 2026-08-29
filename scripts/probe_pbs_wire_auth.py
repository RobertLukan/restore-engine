#!/usr/bin/env python3
"""Debug PBS H2 chunk auth."""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import urllib.parse
from pathlib import Path

import yaml
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, ResponseReceived, StreamEnded

from client_errors import tls_client_context

CHUNK = 4 * 1024 * 1024
ZERO = hashlib.sha256(b"\x00" * CHUNK).digest()
PROTO = "proxmox-backup-reader-protocol-v1"


def main() -> None:
    cfg = yaml.safe_load(Path("/app/config.docker.yaml").read_text())
    pbs = cfg["pbs_servers"][0]
    host = pbs["host"]
    port = int(pbs.get("port") or 8007)
    user = pbs["user"]
    pw = pbs["password"]
    datastore = pbs["mounts"][0]["datastore"]
    ctx = tls_client_context(verify=False)

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
    bt = 1786093566

    q = urllib.parse.urlencode(
        {
            "backup-type": "vm",
            "backup-id": "109",
            "backup-time": str(bt),
            "file-name": "drive-scsi0.img.fidx",
        }
    )
    c = http.client.HTTPSConnection(host, port, context=ctx, timeout=120)
    c.request(
        "GET",
        f"/api2/json/admin/datastore/{datastore}/download?{q}",
        headers={"Cookie": cookie, "CSRFPreventionToken": csrf},
    )
    fidx = c.getresponse().read()
    digests = [
        fidx[4096 + i * 32 : 4096 + (i + 1) * 32]
        for i in range((len(fidx) - 4096) // 32)
    ]
    uniq = [d for d in set(digests) if d != ZERO]
    hx = uniq[0].hex()
    print("digest", hx[:24])

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
    sock.sendall(conn.data_to_send())
    if rest:
        conn.receive_data(rest)
        out = conn.data_to_send()
        if out:
            sock.sendall(out)

    variants = [
        [
            (":method", "GET"),
            (":path", f"/chunk?digest={hx}"),
            (":scheme", "https"),
            (":authority", "localhost"),
        ],
        [
            (":method", "GET"),
            (":path", f"/chunk?digest={hx}"),
            (":scheme", "https"),
            (":authority", "localhost"),
            ("cookie", cookie),
            ("csrfpreventiontoken", csrf),
        ],
        [
            (":method", "GET"),
            (":path", f"/chunk?digest={hx}"),
            (":scheme", "https"),
            (":authority", host),
            ("cookie", cookie),
        ],
        [
            (":method", "GET"),
            (":path", f"/api2/json/chunk?digest={hx}"),
            (":scheme", "https"),
            (":authority", "localhost"),
            ("cookie", cookie),
        ],
    ]
    for i, headers in enumerate(variants):
        sid = conn.get_next_available_stream_id()
        conn.send_headers(sid, headers, end_stream=True)
        sock.sendall(conn.data_to_send())
        body = bytearray()
        status = None
        done = False
        while not done:
            d = sock.recv(65536)
            if not d:
                break
            for ev in conn.receive_data(d):
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
            out = conn.data_to_send()
            if out:
                sock.sendall(out)
        print(f"variant {i}: status={status} len={len(body)} body={bytes(body)[:120]!r}")
        if status == 200 and len(body) > 1000:
            print("SUCCESS ratio", len(body) / CHUNK)
            break
    sock.close()


if __name__ == "__main__":
    main()
