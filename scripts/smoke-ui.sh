#!/usr/bin/env bash
# Run restore-engine UI smoke on the lab VM with Chromium in Docker.
# Does not modify the running app containers or config files.
#
# Usage (on guestos-lab):
#   cd /opt/restore-engine
#   ./scripts/smoke-ui.sh
#
# Optional env:
#   RE_UI_URL=http://127.0.0.1:8006
#   RE_CONFIG=/opt/restore-engine/config.docker.yaml
#   RE_SMOKE_IMAGE=zenika/alpine-chrome:with-puppeteer
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/smoke-ui.cjs"
CONFIG="${RE_CONFIG:-$ROOT/config.docker.yaml}"
URL="${RE_UI_URL:-http://127.0.0.1:8006}"
IMAGE="${RE_SMOKE_IMAGE:-zenika/alpine-chrome:with-puppeteer}"

if [[ ! -f "$SCRIPT" ]]; then
  echo "missing $SCRIPT" >&2
  exit 2
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "missing config $CONFIG (set RE_CONFIG)" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required on this host" >&2
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to read ui.password from config" >&2
  exit 2
fi

# Password stays on this host; never echoed.
RE_UI_PASSWORD="$(
  python3 - "$CONFIG" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
pw = ((cfg or {}).get("ui") or {}).get("password") or ""
if not pw:
    sys.stderr.write("ui.password missing in config\n")
    sys.exit(2)
print(pw, end="")
PY
)"
export RE_UI_PASSWORD

echo "UI smoke → $URL (image $IMAGE)"
# --network host: talk to UI bound on the VM.
# Password via env inside the ephemeral container only.
docker run --rm --network host \
  -e RE_UI_URL="$URL" \
  -e RE_UI_PASSWORD \
  -e PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-browser \
  -e NODE_PATH=/usr/src/app/node_modules \
  -v "$SCRIPT:/smoke/smoke-ui.cjs:ro" \
  -w /usr/src/app \
  "$IMAGE" \
  node /smoke/smoke-ui.cjs
status=$?
unset RE_UI_PASSWORD
exit "$status"
