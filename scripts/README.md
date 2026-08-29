# Development / lab scripts

These scripts read **`config.yaml`** (or `RESTORE_ENGINE_CONFIG`) from your working directory. They are **not** used by the Docker API/worker at runtime.

| Script | Purpose |
|--------|---------|
| `probe_pbs_wire.py` | Sample PBS chunk compression over the wire |
| `probe_pbs_wire_auth.py` | Auth + reader smoke test |
| `probe_pbs_reader_fidx.py` | Download `.fidx` and inspect |
| `build-offline-bundle.sh` | Build amd64 offline Docker bundle (see DOCKER.md) |

**Never commit** a real `config.yaml` or `config.docker.yaml`. Use `config.docker.example.yaml` as a template only.
