# Contributing

Thanks for improving restore-engine.

## Setup

```bash
cd restore-engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Tests use `tests/fixtures/minimal_config.yaml` via `RESTORE_ENGINE_CONFIG` — no live PBS, PVE, or Redis required.

## Pull requests

- Keep changes focused; match existing style in surrounding code.
- Run `pytest` before opening a PR.
- **Do not** commit secrets, real IPs, or production hostnames in configs or docs. Use RFC5737 examples (`203.0.113.x`) or placeholders (`10.0.0.x`, `CHANGE_ME`).
- `config.yaml` and `config.docker.yaml` are gitignored — only update `config.docker.example.yaml` for template changes.

## Security

See [SECURITY.md](SECURITY.md) for responsible disclosure.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
