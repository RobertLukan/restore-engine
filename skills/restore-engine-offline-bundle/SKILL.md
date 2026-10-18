---
name: restore-engine-offline-bundle
description: Build and deploy restore-engine air-gap Docker bundles (linux/amd64). Use when packaging offline tarballs, lab install without GHCR, or air-gapped compose up.
compatibility: Cursor Agent Skills + OpenClaw. Needs Docker on build host; offline host needs only docker load + compose.
metadata:
  openclaw:
    emoji: "📦"
    requires:
      bins: ["docker"]
    os: ["darwin", "linux"]
---

# restore-engine offline bundle

Package restore-engine + Redis (+ optional observability images) for hosts **without** registry access. No secrets in the tarball.

## When to use

- User asks for offline / air-gap / lab package / transfer bundle
- Deploy where `docker compose pull` / GHCR is unavailable
- Cut a release candidate **before** publishing to GitHub (lab-test first)

## Build (online host)

From repo root:

```bash
# Optional: pin version + git rev embedded in image labels / GET /version
export RESTORE_ENGINE_VERSION=0.2.0          # match intended tag (current line is 0.2.0 until next release)
export RESTORE_ENGINE_GIT_REVISION="$(git rev-parse --short HEAD)"

./scripts/build-offline-bundle.sh
# → restore-engine-offline-full-amd64-YYYYMMDD.tar
```

Script behavior (`scripts/build-offline-bundle.sh`):

1. Builds `ghcr.io/robertlukan/restore-engine:${RESTORE_ENGINE_VERSION}` for **linux/amd64** (buildx `--load` when available).
2. Exports app image + `redis:7-alpine` + observability images (Prometheus / OTel / Grafana) as `images/*.tar.gz`.
3. Copies compose tree into `restore-engine/` (**excludes** `config.yaml`, `config.docker.yaml`, `.git`, prior `*.tar`).
4. Writes `OFFLINE-README.txt` inside the archive.

Minimal core-only alternative (no observability images):

```bash
docker compose build
docker pull redis:7-alpine
docker save ghcr.io/robertlukan/restore-engine:${RESTORE_ENGINE_VERSION:-0.2.0} redis:7-alpine \
  -o restore-engine-bundle-amd64.tar
```

Prefer the full script for lab/prod air-gap parity.

## Transfer

Copy to the offline host:

- The `.tar` bundle
- Never include real `config.docker.yaml` in the same share if the channel is untrusted — create it on the host from the example

## Install (offline host)

```bash
tar -xf restore-engine-offline-full-amd64-YYYYMMDD.tar
cd images && for f in *.tar.gz; do gunzip -c "$f" | docker load; done
cd ../restore-engine
cp config.docker.example.yaml config.docker.yaml
# Edit config.docker.yaml:
#   - pve_clusters[] + pbs_servers[].mounts (pve_cluster_id / pve_storage)
#   - ui.password, ui.session_secret (≥32)
#   - redis.url: redis://127.0.0.1:6380/0  (host-network default)
export RESTORE_ENGINE_IMAGE_TAG=0.2.0   # must match loaded app image tag
docker compose up -d
# Optional metrics stack:
docker compose --profile observability up -d
```

### Networking modes (match DOCKER.md)

| Mode | How | UI | Redis config |
| --- | --- | --- | --- |
| **Host network (default)** | `docker compose up -d` | `http://127.0.0.1:8000` | `redis://127.0.0.1:6380/0` |
| **Bridge (isolated)** | `docker compose -f docker-compose.yml -f docker-compose.bridge.yml up -d` | `http://127.0.0.1:8001` | `redis://redis:6379/0` |
| **Mac + VPN lab** | `./scripts/lab-mac.sh` (Redis in Docker; api/worker on Mac) | `http://127.0.0.1:8000` | same as host Redis publish |

On Linux utility hosts, prefer host network so api/worker share VPN/LAN routes to PBS/PVE. Docker Desktop Mac host-network is **not** the Mac VPN — use `lab-mac.sh` for that lab pattern.

## Checklist

```
Offline deploy:
- [ ] Built with RESTORE_ENGINE_VERSION set to the intended tag
- [ ] Bundle SHA256 recorded for the transfer
- [ ] images/*.tar.gz loaded on offline host
- [ ] config.docker.yaml created from example (not committed)
- [ ] RESTORE_ENGINE_IMAGE_TAG matches loaded image
- [ ] redis.url matches networking mode (6380 host vs redis:6379 bridge)
- [ ] GET /version (or UI) shows expected version/revision
- [ ] Worker can reach PBS + PVE networks (host-net or lab-mac)
- [ ] Multi-PVE: pve_clusters + mount pve_cluster_id set if needed
```

## Hard rules

1. **No secrets in bundles or git** — exclude real config; example only.
2. **linux/amd64** — utility hosts are typically Intel/AMD; keep `platform: linux/amd64` in compose.
3. **Image tag sync** — `RESTORE_ENGINE_IMAGE_TAG` / compose `image:` must match what `docker load` imported.
4. **Single worker** unless shared-queue behavior is understood (`DOCKER.md`).
5. Do not treat offline packaging as a substitute for CI/GitHub publish approval — lab-test the tarball first when the user asked for that order.

## Related

- Operator docs: `DOCKER.md` (networking + offline), `README.md` production checklist
- Domain behavior while testing the lab: skill `restore-engine-domain`
- Drive restores via API after up: skill `restore-engine-operate`
- Extra steps / troubleshooting: [reference.md](reference.md)
