#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT_DIR/.env"
  set +a
fi

HOST="${DEEP_PARSE_HOST:-127.0.0.1}"
PORT="${DEEP_PARSE_PORT:-18080}"
VENV_DIR="${DEEP_PARSE_VENV_DIR:-$ROOT_DIR/.venv}"

if [ ! -x "$VENV_DIR/bin/uvicorn" ]; then
  echo "uvicorn not found in $VENV_DIR. Run scripts/setup-venv.sh first." >&2
  exit 1
fi

mkdir -p "${MINERU_API_OUTPUT_ROOT:-/tmp/deftpdf-deep-parse-output}"

exec env \
  MINERU_MODEL_SOURCE="${MINERU_MODEL_SOURCE:-modelscope}" \
  MINERU_LOG_LEVEL="${MINERU_LOG_LEVEL:-INFO}" \
  MINERU_API_ENABLE_FASTAPI_DOCS="${MINERU_API_ENABLE_FASTAPI_DOCS:-0}" \
  MINERU_API_ENABLE_VLM_PRELOAD="${MINERU_API_ENABLE_VLM_PRELOAD:-false}" \
  MINERU_API_OUTPUT_ROOT="${MINERU_API_OUTPUT_ROOT:-/tmp/deftpdf-deep-parse-output}" \
  MINERU_API_MAX_CONCURRENT_REQUESTS="${MINERU_API_MAX_CONCURRENT_REQUESTS:-1}" \
  "$VENV_DIR/bin/uvicorn" \
  deftpdf_deep_parse.app:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers 1
