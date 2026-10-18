# Development / lab scripts

Lab scripts read **`config.yaml`** (or `RESTORE_ENGINE_CONFIG`) from your working directory. They are **not** used by the Docker API/worker at runtime. `cleanup.py` and `add-ha.py` are the exceptions: copy them to a Proxmox node and run them there. They do not read the app config.

| Script | Purpose |
|--------|---------|
| `pbs-host-proxy.py` | Host TCP proxy so Docker can reach VPN/LAN PBS via `host.docker.internal` |
| `probe_pbs_wire.py` | Sample PBS chunk compression over the wire |
| `probe_pbs_wire_auth.py` | Auth + reader smoke test |
| `probe_pbs_reader_fidx.py` | Download `.fidx` and inspect |
| `build-offline-bundle.sh` | Build amd64 offline Docker bundle (see DOCKER.md) |
| `smoke-ui.sh` / `smoke-ui.cjs` | Headless Chromium UI smoke on the lab VM (Docker; does not change the app) |
| `capture-ui-shots.sh` / `capture-ui-shots.cjs` | Blurred UI screenshots for docs (private fields CSS-blurred) |
| `cleanup.py` | On a Proxmox node: list, then destroy cluster QEMU guests tagged `restore-engine` |
| `add-ha.py` | On a Proxmox node: list, then add every cluster QEMU VM to HA |

### Lab networking (PBS on VPN)

**Linux (default):** `docker compose up -d` — api/worker use host networking.  
`redis.url: redis://127.0.0.1:6380/0` · UI http://127.0.0.1:8000

**Mac (Docker Desktop):** host networking cannot see the Mac VPN. Use:

```bash
./scripts/lab-mac.sh
# UI: http://127.0.0.1:8000
```

Keep `pbs_servers[].host` as the real PBS IP (e.g. `192.168.65.78`), not `host.docker.internal` (that was a proxy workaround and causes **connection refused** when the proxy is not running).

### Cleanup restored VMs (on a Proxmox node)

Copy `cleanup.py` to any node and run it as root with `python3`. It scans the cluster (`/etc/pve/nodes/*/qemu-server`) for the `restore-engine` tag, prints every match, and destroys those QEMU guests only after you type `yes`. LXC containers are listed and skipped. PBS backups are not touched.

```bash
python3 cleanup.py --dry-run   # print the list
python3 cleanup.py             # print the list, then ask
python3 cleanup.py --yes       # print the list, then delete
```

### Add cluster VMs to HA (on a Proxmox node)

Copy `add-ha.py` to any node and run it as root with `python3`. It prints every QEMU guest it will add. Running guests are added as HA state `started`. Stopped guests are added as `stopped`, so they stay off. Templates, guests already in HA, and LXC containers are skipped. No HA group is set.

```bash
python3 add-ha.py --dry-run   # print the list
python3 add-ha.py             # print the list, then ask
python3 add-ha.py --yes       # print the list, then add
```

### UI smoke (lab VM)

Runs Chromium inside `zenika/alpine-chrome:with-puppeteer` on the host network against `http://127.0.0.1:8006`. Reads `ui.password` from `config.docker.yaml` only on the VM; does not deploy or edit app files.

```bash
cd /opt/restore-engine   # or your checkout
chmod +x scripts/smoke-ui.sh
./scripts/smoke-ui.sh
```

### UI screenshots (blurred)

```bash
chmod +x scripts/capture-ui-shots.sh
./scripts/capture-ui-shots.sh
# writes docs/screenshots/ui/*.png (names/IDs/inputs blurred)
```

**Never commit** a real `config.yaml` or `config.docker.yaml`. Use `config.docker.example.yaml` as a template only.
