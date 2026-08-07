# Docker (isolated / air-gapped testing)

Stack: **Redis**, **FastAPI UI + API**, **restore worker**. All three talk only on the internal Docker network except port **8001** published for the browser.

## Apple Silicon (M1/M2/M3) → Intel / AMD64 servers

By default **`docker-compose.yml` sets `platform: linux/amd64`** so images match typical Linux x86_64 hosts (including Docker LXCs). On a Mac ARM build, Docker uses **QEMU emulation** for that platform (slower builds; normal runtime on the Intel box).

- **Verify what you built:** `docker image inspect restore-engine:latest --format '{{.Architecture}}'` → should print **`amd64`**.
- **CLI without compose:** `docker buildx build --platform linux/amd64 --load -t restore-engine:latest .`

For **native ARM** images when you only run on Mac ARM, comment out the `platform: linux/amd64` lines in `docker-compose.yml`.

## Build (needs internet once)

From this directory:

```bash
docker compose build
```

That downloads base images (`python:3.12-slim-bookworm`, `redis:7-alpine`) and Python wheels, then bakes them into **restore-engine:latest**.

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

Port **8001** avoids colliding with migration-engine on **8000** when both run on the same host.

## Redis durability and workers

Compose mounts a named volume **`redis-data`** on the bundled Redis service and enables AOF (`--appendonly yes`) so plan inventory, jobs, and reports survive container restarts.

**Run a single worker replica.** Concurrent restore slots are counted in-process (`worker.max_concurrent_restores`). Scaling `worker` to multiple containers does not share that counter — each process can open its own max slots against the same Proxmox/PBS. Prefer one worker service; raise `max_concurrent_restores` inside that process if you need more parallelism.

## Redis already on the server (host) vs Redis in Docker

**Short answer:** it is **not** a problem for the host to run Redis and for Compose to also run the **`redis`** service. They are **separate instances** unless you point both at the same socket/port.

- **Default compose file** does **not** publish Redis on the host (`ports:` is only on `api`). The bundled `redis:7-alpine` container listens on **6379 inside the Docker network** only, under the hostname **`redis`**.
- The app and worker use **whatever URL you set** in `redis.url` (e.g. `redis://redis:6379/0` for the **Compose** Redis).

**If you want to use the host’s Redis instead of the Compose `redis` service:**

1. Set **`redis.url`** in your mounted config to a URL the containers can reach (e.g. `redis://host.docker.internal:6379/0` or the host gateway IP on Linux).
2. Ensure host Redis accepts TCP from Docker; prefer firewall rules so only the Docker bridge can reach that port.
3. Remove or disable the **`redis`** service and **`depends_on: redis`** in `docker-compose.yml` (optional but cleaner).

## Move to an air-gapped host

On a machine **with** Docker and internet:

```bash
docker compose build
docker pull redis:7-alpine
docker save restore-engine:latest redis:7-alpine -o restore-engine-bundle-amd64.tar
```

Copy **`restore-engine-bundle-amd64.tar`**, this **`docker-compose.yml`**, and optionally **`config.docker.yaml`** to the offline host, then:

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
- Product roadmap (enterprise-style plans, readiness, reports): see the long-term plan in Cursor plans / project docs when published.
