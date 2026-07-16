#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="${DEEP_PARSE_VENV_DIR:-$ROOT_DIR/.venv}"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
if [ "$(uname -s)" = "Linux" ] && [ "${DEEP_PARSE_CPU_ONLY:-true}" = "true" ]; then
  "$VENV_DIR/bin/python" -m pip install \
    --index-url "${DEEP_PARSE_TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}" \
    "torch==${DEEP_PARSE_TORCH_VERSION:-2.11.0}" \
    "torchvision==${DEEP_PARSE_TORCHVISION_VERSION:-0.26.0}"
fi
"$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"

echo "Deep Parse service venv ready at $VENV_DIR"
