# Development / lab scripts

Lab scripts read **`config.yaml`** (or `RESTORE_ENGINE_CONFIG`) from your working directory. They are **not** used by the Docker API/worker at runtime. `cleanup.py`, `tag-batch.py`, and `add-ha.py` are the exceptions: copy them to a Proxmox node and run them there. They do not read the app config.

| Script | Purpose |
|--------|---------|
| `pbs-host-proxy.py` | Host TCP proxy so Docker can reach VPN/LAN PBS via `host.docker.internal` |
| `probe_pbs_wire.py` | Sample PBS chunk compression over the wire |
| `probe_pbs_wire_auth.py` | Auth + reader smoke test |
| `probe_pbs_reader_fidx.py` | Download `.fidx` and inspect |
| `build-offline-bundle.sh` | Build amd64 offline Docker bundle (see DOCKER.md) |
| `smoke-ui.sh` / `smoke-ui.cjs` | Headless Chromium UI smoke on the lab VM (Docker; does not change the app) |
| `capture-ui-shots.sh` / `capture-ui-shots.cjs` | Blurred UI screenshots for docs (private fields CSS-blurred) |
| `lab-mac.sh` | Mac lab: Redis in Docker, api/worker on the Mac (VPN-reachable PBS) |
| `cleanup.py` | On a Proxmox node: destroy QEMU guests for a **batch** (`--batch re-<id>`); dual-tag gate |
| `tag-batch.py` | On a Proxmox node: add `re-<batch>` to guests that already have `restore-engine` (for Cleanup) |
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

### Tag existing restore-engine VMs with a batch id (on a Proxmox node)

Cleanup only destroys guests that have **both** `restore-engine` and `re-<batch>`. Older restores may only have the ownership tag. Copy `tag-batch.py` to a node and run as root:

```bash
python3 tag-batch.py --batch re-a1b2c3d4e5f6 --dry-run
python3 tag-batch.py --new-batch --dry-run          # generate a random batch id
python3 tag-batch.py --batch re-a1b2c3d4e5f6 --yes
python3 tag-batch.py --batch re-a1b2c3d4e5f6 --force-retag --yes  # replace other re-* tags
```

Only guests with `restore-engine` are tagged. Guests that already have a different `re-*` are skipped unless `--force-retag`. Tags are set via `pvesh` on each guest’s owning node (works across the cluster). Then use Cleanup / `cleanup.py` with the same batch id.

### Cleanup restored VMs (on a Proxmox node)

Prefer the UI **Cleanup** tab or `/api/cleanup/*` when `ui.plan_run_cleanup_enabled` is on (uses the plan run `batch_id`).

Node-side helper: copy `cleanup.py` to any node and run as root. **Requires `--batch`** (`re-<12hex>` or bare hex). Guests must carry both `restore-engine` and that batch tag. LXC skipped; PBS untouched.

```bash
python3 cleanup.py --batch re-a1b2c3d4e5f6 --dry-run
python3 cleanup.py --batch a1b2c3d4e5f6          # ask before destroy
python3 cleanup.py --batch re-a1b2c3d4e5f6 --yes
```

### Add cluster VMs to HA (on a Proxmox node)

Copy `add-ha.py` to any node and run it as root with `python3`. It prints every QEMU guest it will change. A running guest is always HA state `started` and is never switched to `stopped`. Stopped guests are added as `stopped`, so they stay off. Run it again after powering a guest on: if that guest is on but its HA state is still `stopped`, the state is changed to `started`. `--start-all` sets guests that are still powered off to `started`, which powers them on. Templates and LXC containers are skipped. No HA group is set.

```bash
python3 add-ha.py --dry-run            # print the list
python3 add-ha.py                      # print the list, then ask
python3 add-ha.py --yes                # print the list, then add
python3 add-ha.py --start-all          # start guests that are stopped
python3 add-ha.py --start-all --yes
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
