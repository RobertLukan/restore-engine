# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| `main` branch | yes |
| Older tags / forks | best effort |

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security-sensitive reports.

Email the maintainer privately with:

- Description and impact
- Steps to reproduce
- Affected version / commit

We will acknowledge receipt and aim to respond within a reasonable timeframe.

## Deployment model (read before exposing this app)

restore-engine is an **infrastructure admin tool** with a **trusted-operator** security model. It is designed for private management networks, not the public internet.

### Authentication

- Browser login uses a shared `ui.password` and signed session cookie (`ui.session_secret`).
- Optional API tokens (`ui.api_tokens`) with operator (full) or viewer (read-only) roles.
- **Startup fails** if `ui.password` is set but `ui.session_secret` is missing, too short (<32), or the known dev placeholder.

### Network exposure

- Default Compose publishes the UI on host port **8001** (not PBS **8007** or PVE **8006**).
- Terminate **TLS** at a reverse proxy; do not expose plain HTTP on shared networks.
- **Do not publish Redis** to the host; Compose Redis has no password on the internal Docker network only.

### Destructive capabilities

Authenticated operators can:

- Restore VMs from PBS to PVE (including **DR mode** with source VMIDs)
- Reclaim/overwrite restore-engine-managed VMIDs
- Run plan teardown / destroy restored guests
- Change PBS/PVE credentials via Settings (writes `config.yaml` / mounted config)

Use `worker.require_verified_to_run: true` in production so plan **Run** requires a successful **Check** unless `allow_unverified` is explicitly sent (drills).

### SSRF and outbound requests

- **HTTP check URLs** on restore jobs are fetched by the worker from user-supplied config (assurance/drill). Treat operators as trusted; do not expose the worker to untrusted job authors.
- **Webhooks** POST to configured URLs with optional shared secret header.

### Observability profile

Optional Grafana enables **anonymous Viewer** and iframe embed for the Infra tab. Keep Grafana on a management LAN; set `GRAFANA_ADMIN_PASSWORD`; do not expose anonymously to the internet.

### Debug bundle

The Settings **Collect & download** bundle masks secrets in the UI but may include hostnames, job metadata, and health details. Handle exports as sensitive.

### Secrets in git

Never commit `config.yaml`, `config.docker.yaml`, `.env`, or offline transfer bundles. Example configs use placeholders only (RFC5737 documentation addresses such as `203.0.113.x`).

## Dependency updates

GitHub Dependabot opens PRs for Python and Actions dependencies. CI runs `pytest` and optional `pip-audit` on pull requests.
