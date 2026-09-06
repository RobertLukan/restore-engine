# Development / lab scripts

These scripts read **`config.yaml`** (or `RESTORE_ENGINE_CONFIG`) from your working directory. They are **not** used by the Docker API/worker at runtime.

| Script | Purpose |
|--------|---------|
| `probe_pbs_wire.py` | Sample PBS chunk compression over the wire |
| `probe_pbs_wire_auth.py` | Auth + reader smoke test |
| `probe_pbs_reader_fidx.py` | Download `.fidx` and inspect |
| `build-offline-bundle.sh` | Build amd64 offline Docker bundle (see DOCKER.md) |
| `smoke-ui.sh` / `smoke-ui.cjs` | Headless Chromium UI smoke on the lab VM (Docker; does not change the app) |
| `capture-ui-shots.sh` / `capture-ui-shots.cjs` | Blurred UI screenshots for docs (private fields CSS-blurred) |

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
