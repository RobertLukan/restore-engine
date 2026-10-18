#!/usr/bin/env bash
# Thin HTTP helper for restore-engine (same API as the web UI).
# Usage:
#   re-api.sh GET /api/backups
#   re-api.sh GET '/api/jobs?limit=20'
#   re-api.sh POST /api/jobs/restore-selected '{"backup_ids":["…"],…}'
#   re-api.sh POST /api/jobs/purge '{}'
set -euo pipefail

if [[ -z "${RESTORE_ENGINE_URL:-}" ]]; then
  echo "RESTORE_ENGINE_URL is not set (e.g. http://127.0.0.1:8000 host-network, or http://localhost:8001 bridge)" >&2
  exit 1
fi
if [[ -z "${RESTORE_ENGINE_TOKEN:-}" ]]; then
  echo "RESTORE_ENGINE_TOKEN is not set (ui.api_tokens operator token)" >&2
  exit 1
fi

METHOD="${1:-}"
PATH_Q="${2:-}"
BODY="${3:-}"

if [[ -z "$METHOD" || -z "$PATH_Q" ]]; then
  echo "Usage: $0 METHOD /path [json-body]" >&2
  exit 1
fi

BASE="${RESTORE_ENGINE_URL%/}"
case "$PATH_Q" in
  /*) URL="${BASE}${PATH_Q}" ;;
  *)  URL="${BASE}/${PATH_Q}" ;;
esac

AUTH=( -H "Authorization: Bearer ${RESTORE_ENGINE_TOKEN}" )
CT=( -H "Content-Type: application/json" )

METHOD_U=$(printf '%s' "$METHOD" | tr '[:lower:]' '[:upper:]')

if [[ -n "$BODY" ]]; then
  curl -sS -X "$METHOD_U" "${AUTH[@]}" "${CT[@]}" -d "$BODY" "$URL"
else
  case "$METHOD_U" in
    POST|PUT|PATCH)
      curl -sS -X "$METHOD_U" "${AUTH[@]}" "${CT[@]}" -d '{}' "$URL"
      ;;
    *)
      curl -sS -X "$METHOD_U" "${AUTH[@]}" "$URL"
      ;;
  esac
fi
echo
