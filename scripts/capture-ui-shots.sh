#!/usr/bin/env bash
# Capture blurred UI screenshots on the lab VM (Chromium in Docker).
# Does not modify the running app. Writes PNGs to RE_SHOT_DIR.
#
# Usage:
#   cd /opt/restore-engine
#   ./scripts/capture-ui-shots.sh
#   # optional: RE_SHOT_DIR=/tmp/re-ui-shots ./scripts/capture-ui-shots.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/capture-ui-shots.cjs"
CONFIG="${RE_CONFIG:-$ROOT/config.docker.yaml}"
# Lab defaults: host-network / lab-mac → :8000; some utility hosts still use :8006.
URL="${RE_UI_URL:-http://127.0.0.1:8000}"
IMAGE="${RE_SMOKE_IMAGE:-zenika/alpine-chrome:with-puppeteer}"
OUT="${RE_SHOT_DIR:-$ROOT/docs/screenshots/ui}"

if [[ ! -f "$SCRIPT" ]]; then
  echo "missing $SCRIPT" >&2
  exit 2
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "missing config $CONFIG" >&2
  exit 2
fi

mkdir -p "$OUT"
# alpine-chrome runs as non-root; ensure the bind mount is writable
chmod a+rwx "$OUT" 2>/dev/null || true

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

# Docker Desktop (Mac): --network host is the Linux VM, not the Mac loopback.
# Reach lab-mac / host-bound UI via host.docker.internal instead.
RUN_ARGS=(run --rm --user root)
if [[ "$(uname -s)" == "Darwin" ]]; then
  if [[ "$URL" == *"127.0.0.1"* ]] || [[ "$URL" == *"localhost"* ]]; then
    URL="${URL/127.0.0.1/host.docker.internal}"
    URL="${URL/localhost/host.docker.internal}"
  fi
else
  RUN_ARGS+=(--network host)
fi

echo "UI screenshots → $URL → $OUT"
docker "${RUN_ARGS[@]}" \
  -e RE_UI_URL="$URL" \
  -e RE_UI_PASSWORD \
  -e RE_SHOT_DIR=/shots \
  -e PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-browser \
  -e NODE_PATH=/usr/src/app/node_modules \
  -v "$SCRIPT:/smoke/capture-ui-shots.cjs:ro" \
  -v "$OUT:/shots" \
  -w /usr/src/app \
  "$IMAGE" \
  node /smoke/capture-ui-shots.cjs
status=$?
unset RE_UI_PASSWORD
# normalize ownership when run as root in the container
if command -v sudo >/dev/null 2>&1; then
  sudo chown -R "$(id -u):$(id -g)" "$OUT" 2>/dev/null || true
else
  chown -R "$(id -u):$(id -g)" "$OUT" 2>/dev/null || true
fi
echo "Screenshots in $OUT"
ls -la "$OUT"
exit "$status"
