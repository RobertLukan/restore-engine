# Docker (isolated / air-gapped testing)

Stack: **Redis**, **FastAPI UI + API**, **restore worker**. All three talk only on the internal Docker network except port **8001** published for the browser (host → container `8000`).

## Ports (do not confuse)

| Port | Service |
|------|---------|
| **8001** | restore-engine dashboard (Compose `ports:` default) |
| **8006** | Proxmox VE API (`proxmox.port` in config) |
| **8007** | Proxmox Backup Server API (`pbs_servers[].port` in config) |

Change the **left** side of `ports:` in `docker-compose.yml` if another host port suits your site (e.g. `8007:8000` locally). Do not use **8007** as the public default — that is PBS’s usual API port.

## Apple Silicon (M1/M2/M3) → Intel / AMD64 servers

By default **`docker-compose.yml` sets `platform: linux/amd64`** so images match typical Linux x86_64 hosts (including Docker LXCs). On a Mac ARM build, Docker uses **QEMU emulation** for that platform (slower builds; normal runtime on the Intel box).

- **Verify what you built:** `docker image inspect restore-engine:latest --format '{{.Architecture}}'` → should print **`amd64`**.
- **CLI without compose:** `docker buildx build --platform linux/amd64 --load -t restore-engine:latest .`

For **native ARM** images when you only run on Mac ARM, comment out the `platform: linux/amd64` lines in `docker-compose.yml`.

## Build (needs internet once)

From this directory you can **pull a release image** from GitHub Container Registry (recommended) or build locally.

### Pull from GHCR (recommended)

Published on each [GitHub release](https://github.com/RobertLukan/restore-engine/releases):

```bash
docker pull ghcr.io/robertlukan/restore-engine:0.1.0
# or: docker compose pull
```

Image: **`ghcr.io/robertlukan/restore-engine`** — tags match release versions (`0.1.0`, `latest`).

Set `RESTORE_ENGINE_IMAGE_TAG` in the environment or `.env` to pin a version (Compose default: `0.1.0`).

### Build locally

```bash
docker compose build
```

That downloads base images (`python:3.12-slim-bookworm`, `redis:7-alpine`) and Python wheels, then bakes them into the app image tagged for GHCR.

## Run (no internet)

```bash
docker compose up -d
```

Open **http://localhost:8001**. Log in with `ui.password` from the config file inside the image (default baked from `config.docker.example.yaml`: change it for anything beyond a throwaway lab).

Health: **GET http://localhost:8001/health** (checks Redis and config).
Version: **GET http://localhost:8001/version**.

## Use your own config (secrets, PBS / Proxmox IPs)

1. Copy `config.docker.example.yaml` to `config.docker.yaml`.
2. Keep **`redis.url`** as **`redis://redis:6379/0`** (hostname must match the compose service name).
3. Set **`ui.password`** and **`ui.session_secret`** (session secret must be non-empty; long random string).
4. Fill **`pbs_servers`** (or legacy `pbs:`) and **`proxmox`** with real endpoints and tokens.
5. Uncomment the **`volumes`** lines under `api` and `worker` in `docker-compose.yml` so both processes read `./config.docker.yaml`.
6. `docker compose up -d --build` if you changed the compose file only, or restart containers.

**Never commit `config.docker.yaml`** — it is gitignored. Only `config.docker.example.yaml` ships in the repo and image.

## LXC bring-up checklist (utility Docker host)

Same pattern as migration-engine on a Docker-capable LXC:

1. Clone the private repo onto the LXC (or copy the tree).
2. `cp config.docker.example.yaml config.docker.yaml` and edit secrets / PBS / PVE.
3. Uncomment config volume mounts in `docker-compose.yml`.
4. `docker compose up --build -d`
5. Confirm: `curl -sS http://127.0.0.1:8001/version` and open the UI on **:8001**.
6. Sign in with `ui.password`; Settings → verify PBS and Proxmox.

Port **8001** is the default published host port (maps to container 8000). Change the left side of `ports:` in `docker-compose.yml` if you need another host port on your utility box.

## Optional observability (Grafana)

```bash
docker compose --profile observability up -d
```

- Grafana: **http://localhost:3002** (anonymous Viewer for iframe embed; admin password via `GRAFANA_ADMIN_PASSWORD`; host 3002 avoids clashing with another Grafana on 3000/3001)
- Prometheus: **http://localhost:9090**
- OTel Collector: host ports **4317/4318** for PVE/PBS Metric Server

Lab Netdata install script: `deploy/observability/install-netdata-pve.sh`. Full guide: [docs/infra-metrics.md](docs/infra-metrics.md).

## Redis durability and workers

Compose mounts a named volume **`redis-data`** on the bundled Redis service and enables AOF (`--appendonly yes`) so plan inventory, jobs, and reports survive container restarts.

**Run one or more workers safely.** Concurrent restore slots are counted in Redis (`restore:concurrency:slots`), so multiple worker replicas share the same `worker.max_concurrent_restores` cap. Prefer a single worker unless you intentionally scale out; the Compose worker healthcheck watches the Redis heartbeat key.

## Redis already on the server (host) vs Redis in Docker

**Short answer:** it is **not** a problem for the host to run Redis and for Compose to also run the **`redis`** service. They are **separate instances** unless you point both at the same socket/port.

- **Default compose file** does **not** publish Redis on the host (`ports:` is only on `api`). The bundled `redis:7-alpine` container listens on **6379 inside the Docker network** only, under the hostname **`redis`**.
- The app and worker use **whatever URL you set** in `redis.url` (e.g. `redis://redis:6379/0` for the **Compose** Redis).

**If you want to use the host’s Redis instead of the Compose `redis` service:**

1. Set **`redis.url`** in your mounted config to a URL the containers can reach (e.g. `redis://host.docker.internal:6379/0` or the host gateway IP on Linux).
2. Ensure host Redis accepts TCP from Docker; prefer firewall rules so only the Docker bridge can reach that port.
3. Remove or disable the **`redis`** service and **`depends_on: redis`** in `docker-compose.yml` (optional but cleaner).

## Move to an air-gapped host

GHCR and `docker compose pull` need network access. For **offline** utility hosts, use a transfer bundle instead of the registry.

On a machine **with** Docker and internet:

```bash
./scripts/build-offline-bundle.sh
# creates restore-engine-offline-full-amd64-YYYYMMDD.tar
```

Or minimal core only:

```bash
docker compose build
docker pull redis:7-alpine
docker save ghcr.io/robertlukan/restore-engine:0.1.0 redis:7-alpine -o restore-engine-bundle-amd64.tar
```

Copy the **`.tar`** bundle, this **`docker-compose.yml`**, and **`config.docker.yaml`** (create from example; never commit secrets) to the offline host, then:

```bash
docker load -i restore-engine-bundle-amd64.tar
docker compose up -d
```

## Environment

- **`RESTORE_ENGINE_CONFIG`**: path to YAML inside the container (default `/app/config.docker.yaml`). Set in compose for both `api` and `worker`.
- **`RESTORE_ENGINE_GIT_REVISION`** (optional): short commit or build id; exposed in **`GET /version`** when wired the same way as migration-engine.

## Vulnerability scans

1. Re-pull base images and rebuild periodically: `docker pull python:3.12-slim-bookworm` then `docker compose build --no-cache`.
2. The Dockerfile runs `apt-get upgrade` at build time.
3. Upgrade pins in `requirements.txt` when PyPI CVEs matter, then rebuild.
4. Refresh `redis:7-alpine` when re-saving an offline bundle.

## Notes

- **PBS and Proxmox VE** need reachability from the **worker** (and usually the **api**) container to those networks.
- **`config.yaml`** in the repo is **not** copied into the image (`.dockerignore`) to avoid leaking credentials; the image ships **`config.docker.example.yaml`** as `/app/config.docker.yaml`.
- **TLS:** Compose publishes plain HTTP on **8001**. For production, terminate TLS at a reverse proxy; do not rely on lab HTTP.
- **Secrets:** mount a host `config.docker.yaml` (gitignored). Prefer strong `ui.session_secret`, API tokens, and `worker.require_verified_to_run: true` outside the lab.
- **Redis:** default Compose Redis has **no password** and is reachable only on the Docker network — do not add a host `ports:` mapping for Redis in production.
- Product status: plans / readiness / reports / drills / assurance / compliance / notifications are implemented; see README production checklist.
