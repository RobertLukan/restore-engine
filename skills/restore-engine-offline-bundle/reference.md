# Offline bundle reference

## Bundle layout

```text
restore-engine-offline-full-amd64-YYYYMMDD.tar
├── OFFLINE-README.txt
├── images/
│   ├── ghcr.io_robertlukan_restore-engine_<ver>.tar.gz
│   ├── redis_7-alpine.tar.gz
│   ├── prom_prometheus_*.tar.gz
│   ├── otel_collector_*.tar.gz
│   └── grafana_grafana_*.tar.gz
└── restore-engine/          # compose tree (no secrets)
    ├── docker-compose.yml
    ├── config.docker.example.yaml
    ├── Dockerfile
    ├── DOCKER.md
    └── ...
```

## Environment variables

| Variable | Where | Purpose |
| --- | --- | --- |
| `RESTORE_ENGINE_VERSION` | build | Image tag + compose default |
| `RESTORE_ENGINE_GIT_REVISION` | build | Short rev in image / `/version` |
| `RESTORE_ENGINE_IMAGE_TAG` | offline `compose up` | Select loaded app image tag |
| `RESTORE_ENGINE_CONFIG` | containers | Path to YAML (default `/app/config.docker.yaml`) |

## Common failures

| Symptom | Likely cause |
| --- | --- |
| Compose pulls from GHCR offline | Forgot `docker load` or wrong tag |
| API/worker crash on start | Missing/short `ui.session_secret` with password set |
| Empty backup list | PBS unreachable from container network / bad tokens |
| Restore fails hostpci | Non-root token on PCI-passthrough guests |
| Redis conflict on host | Host Redis vs compose Redis — leave compose Redis unpublished; see `DOCKER.md` |

## Verify after up

```bash
docker compose ps
curl -sS http://127.0.0.1:8001/version
docker compose logs --tail=80 api worker
```

## Observability profile

`docker compose --profile observability up -d` — Grafana typically on host port **3002** (see compose comments). Point PVE/PBS Metric Server OTLP at host `:4318` when using the bundled collector.
