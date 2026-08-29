#!/usr/bin/env bash
# Build linux/amd64 offline bundle: Docker images + compose tree (no secrets).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="${RESTORE_ENGINE_VERSION:-0.1.0}"
GIT_REV="${RESTORE_ENGINE_GIT_REVISION:-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)}"
IMAGE="ghcr.io/robertlukan/restore-engine:${VERSION}"
STAMP="$(date +%Y%m%d)"
OUT_NAME="restore-engine-offline-full-amd64-${STAMP}.tar"
BUNDLE_DIR="$(mktemp -d /tmp/restore-engine-offline-full.XXXXXX)"
IMAGES_DIR="$BUNDLE_DIR/images"

cleanup() {
  rm -rf "$BUNDLE_DIR"
}
trap cleanup EXIT

echo "Building ${IMAGE} (${GIT_REV}) for linux/amd64..."
export RESTORE_ENGINE_VERSION="$VERSION"
export RESTORE_ENGINE_GIT_REVISION="$GIT_REV"

if docker buildx version >/dev/null 2>&1; then
  docker buildx build \
    --platform linux/amd64 \
    --provenance=false \
    --sbom=false \
    --load \
    --build-arg "RESTORE_ENGINE_VERSION=${VERSION}" \
    --build-arg "RESTORE_ENGINE_GIT_REVISION=${GIT_REV}" \
    -t "${IMAGE}" \
    .
else
  docker build \
    --platform linux/amd64 \
    --build-arg "RESTORE_ENGINE_VERSION=${VERSION}" \
    --build-arg "RESTORE_ENGINE_GIT_REVISION=${GIT_REV}" \
    -t "${IMAGE}" \
    .
fi

mkdir -p "$IMAGES_DIR"

export_image() {
  local ref="$1"
  local out="$2"
  echo "=== export $ref -> $(basename "$out") ==="
  if command -v skopeo >/dev/null 2>&1; then
    skopeo copy --override-os linux --override-arch amd64 \
      "docker://$ref" \
      "docker-archive:${out}:${ref}"
  else
    docker pull --platform linux/amd64 "$ref"
    docker save "$ref" -o "$out"
  fi
  gzip -1 -f "$out"
}

# App image via docker save (local build)
APP_TAR="$IMAGES_DIR/restore-engine_app.tar"
SAFE_TAG="${IMAGE//\//_}"
SAFE_TAG="${SAFE_TAG//:/_}"
APP_TAR="$IMAGES_DIR/${SAFE_TAG}.tar"
echo "=== save ${IMAGE} -> $(basename "$APP_TAR") ==="
docker save "${IMAGE}" -o "$APP_TAR"
gzip -1 -f "$APP_TAR"

export_image redis:7-alpine "$IMAGES_DIR/redis_7-alpine.tar"
export_image prom/prometheus:v2.55.1 "$IMAGES_DIR/prom_prometheus_v2.55.1.tar"
export_image otel/opentelemetry-collector-contrib:0.114.0 "$IMAGES_DIR/otel_collector_0.114.0.tar"
export_image grafana/grafana:11.3.1 "$IMAGES_DIR/grafana_grafana_11.3.1.tar"

rsync -a \
  --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='*.pyc' --exclude='.DS_Store' --exclude='._*' \
  --exclude='config.yaml' --exclude='config.docker.yaml' \
  --exclude='restore-engine-offline-*' --exclude='*.tar' --exclude='*.tar.gz' \
  ./ "$BUNDLE_DIR/restore-engine/"

cat > "$BUNDLE_DIR/OFFLINE-README.txt" <<EOF
restore-engine offline bundle (linux/amd64)
Version: ${VERSION}
Git: ${GIT_REV}
Built: $(date -u +%Y-%m-%dT%H:%MZ)

Contents
  images/*.tar.gz            Docker images (gunzip | docker load for each)
  restore-engine/            compose tree, docs, deploy/observability

On the offline host
  tar -xf ${OUT_NAME}
  cd images && for f in *.tar.gz; do gunzip -c "\$f" | docker load; done
  cd ../restore-engine
  cp config.docker.example.yaml config.docker.yaml   # edit secrets before up
  docker compose up -d
  docker compose --profile observability up -d       # optional

UI default: http://localhost:8001
Never commit or share config.docker.yaml with real tokens.
EOF

OUT_PATH="$ROOT/$OUT_NAME"
tar -cf "$OUT_PATH" -C "$BUNDLE_DIR" .
SHA=$(shasum -a 256 "$OUT_PATH" | awk '{print $1}')
SIZE=$(ls -lh "$OUT_PATH" | awk '{print $5}')

echo ""
echo "Bundle: $OUT_PATH ($SIZE)"
echo "SHA256: $SHA"
