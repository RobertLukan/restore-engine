#!/usr/bin/env python3
"""TCP proxy: Docker containers → host → VPN/LAN PBS.

Docker Desktop containers are not on the host VPN. Point ``pbs_servers[].host``
at ``host.docker.internal`` and ``port`` at the listen port, then run this on
the Mac/host that *can* reach PBS:

    PBS_PROXY_TARGET=192.168.65.78:8007 ./scripts/pbs-host-proxy.py
    # listens on 0.0.0.0:18007 by default (TLS is passed through untouched)

Env:
  PBS_PROXY_LISTEN   default 18007 (host:port or port)
  PBS_PROXY_TARGET   required, e.g. 192.168.65.78:8007
  PBS_PROXY_BIND     default 0.0.0.0
"""

from __future__ import annotations

import os
import select
import socket
import sys
import threading


def _parse_endpoint(raw: str, *, default_host: str = "127.0.0.1") -> tuple[str, int]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty endpoint")
    if ":" in text:
        host, _, port_s = text.rpartition(":")
        host = host.strip() or default_host
        return host, int(port_s)
    return default_host, int(text)


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client: socket.socket, target: tuple[str, int]) -> None:
    upstream: socket.socket | None = None
    try:
        upstream = socket.create_connection(target, timeout=15)
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        t1 = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
        t2 = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except OSError as exc:
        print(f"proxy session error: {exc}", file=sys.stderr)
    finally:
        for s in (client, upstream):
            if s is None:
                continue
            try:
                s.close()
            except OSError:
                pass


def main() -> int:
    target_raw = (os.environ.get("PBS_PROXY_TARGET") or "").strip()
    if not target_raw:
        print(
            "Set PBS_PROXY_TARGET to the real PBS endpoint, e.g.\n"
            "  PBS_PROXY_TARGET=192.168.65.78:8007 ./scripts/pbs-host-proxy.py",
            file=sys.stderr,
        )
        return 2
    target = _parse_endpoint(target_raw)
    listen_raw = (os.environ.get("PBS_PROXY_LISTEN") or "18007").strip()
    bind = (os.environ.get("PBS_PROXY_BIND") or "0.0.0.0").strip()
    _, listen_port = _parse_endpoint(listen_raw, default_host=bind)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((bind, listen_port))
    server.listen(64)
    print(
        f"PBS host proxy listening on {bind}:{listen_port} → {target[0]}:{target[1]} "
        f"(point Docker config at host.docker.internal:{listen_port})",
        flush=True,
    )
    try:
        while True:
            # Wake periodically so Ctrl+C works promptly on some platforms.
            ready, _, _ = select.select([server], [], [], 1.0)
            if not ready:
                continue
            client, addr = server.accept()
            print(f"connect from {addr[0]}:{addr[1]}", flush=True)
            threading.Thread(target=_handle, args=(client, target), daemon=True).start()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
