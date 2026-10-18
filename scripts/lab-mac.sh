#!/usr/bin/env bash
# Mac lab: run api + worker on the Mac network stack (VPN/LAN reach PBS).
# Docker Desktop "host" networking cannot use the Mac VPN — do not use
# docker compose host mode for VPN PBS on Mac.
#
# Usage (run in Terminal.app / iTerm from repo root — keep this process open):
#   ./scripts/lab-mac.sh
# UI: http://127.0.0.1:8000
#
# Redis: starts Compose Redis on 127.0.0.1:6380 (or reuses it).
# config.docker.yaml must use:
#   redis.url: redis://127.0.0.1:6380/0
#   pbs_servers[].host: real PBS IP/hostname (NOT host.docker.internal)

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CFG="${RESTORE_ENGINE_CONFIG:-$ROOT/config.docker.yaml}"
if [[ ! -f "$CFG" ]]; then
  echo "Missing $CFG — copy config.docker.example.yaml and edit." >&2
  exit 1
fi

python3 - <<'PY' "$CFG"
import sys, yaml
from pathlib import Path
p = Path(sys.argv[1])
cfg = yaml.safe_load(p.read_text()) or {}
redis = cfg.setdefault("redis", {})
redis["url"] = "redis://127.0.0.1:6380/0"
# Guard against leftover proxy host from earlier lab attempts.
for s in cfg.get("pbs_servers") or []:
    if not isinstance(s, dict):
        continue
    if str(s.get("host") or "").strip() in {"host.docker.internal", "localhost", "127.0.0.1"}:
        print(
            "WARNING: pbs_servers host is",
            s.get("host"),
            "- set it to the real PBS address (VPN/LAN IP), not a Docker proxy host.",
            file=sys.stderr,
        )
p.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120))
print("redis.url → redis://127.0.0.1:6380/0")
PY

echo "Stopping Docker api/worker (Redis stays)…"
docker compose stop api worker >/dev/null 2>&1 || true
docker compose up -d redis

echo "Waiting for Redis on 127.0.0.1:6380…"
for _ in $(seq 1 30); do
  if docker compose exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; then
    break
  fi
  sleep 0.5
done

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

export RESTORE_ENGINE_CONFIG="$CFG"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

mkdir -p /tmp/restore-engine-lab
if [[ -f /tmp/restore-engine-lab/worker.pid ]] && kill -0 "$(cat /tmp/restore-engine-lab/worker.pid)" 2>/dev/null; then
  kill "$(cat /tmp/restore-engine-lab/worker.pid)" 2>/dev/null || true
fi
python worker.py > /tmp/restore-engine-lab/worker.log 2>&1 &
echo $! > /tmp/restore-engine-lab/worker.pid
echo "Worker pid $(cat /tmp/restore-engine-lab/worker.pid) (log /tmp/restore-engine-lab/worker.log)"
echo "API on http://127.0.0.1:8000  (Ctrl+C stops API; worker keeps running until killed)"
exec python -m uvicorn main:app --host 127.0.0.1 --port 8000 --log-level info
