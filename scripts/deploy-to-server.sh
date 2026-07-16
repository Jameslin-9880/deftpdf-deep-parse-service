#!/usr/bin/env bash
set -euo pipefail

log() {
  local level="$1"
  shift
  printf '[%s] %s\n' "$level" "$*"
}

die() {
  log ERROR "$*"
  exit 1
}

escape_shell() {
  printf '%q' "$1"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DEPLOY_HOST="${DEPLOY_HOST:-${1:-}}"
EXPECTED_SHA="${EXPECTED_SHA:-${2:-}}"
APP_ROOT="${APP_ROOT:-/opt/deftpdf-deep-parse}"
STATE_ROOT="${STATE_ROOT:-/var/lib/deftpdf-deep-parse}"
SERVICE_NAME="${SERVICE_NAME:-deftpdf-deep-parse.service}"
KEEP_RELEASES="${KEEP_RELEASES:-3}"
REMOTE_TMP_DIR="${REMOTE_TMP_DIR:-/tmp}"
SSH_KEY="${SSH_KEY:-}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

[ -n "$DEPLOY_HOST" ] || die "Deploy host is required."

CURRENT_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
if [ -n "$EXPECTED_SHA" ] && [ "$EXPECTED_SHA" != "$CURRENT_SHA" ]; then
  die "Checkout SHA $CURRENT_SHA does not match expected SHA $EXPECTED_SHA."
fi
EXPECTED_SHA="$CURRENT_SHA"

if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  die "Refusing to deploy a dirty checkout."
fi

SHORT_SHA="$(git -C "$REPO_ROOT" rev-parse --short=12 "$EXPECTED_SHA")"
RELEASE_ID="$(date -u +%Y%m%d_%H%M%S)-$SHORT_SHA"
ARTIFACT="$(mktemp "${TMPDIR:-/tmp}/deftpdf-deep-parse.XXXXXX.tar.gz")"
REMOTE_ARTIFACT="$REMOTE_TMP_DIR/deftpdf-deep-parse-$RELEASE_ID.tar.gz"

cleanup() {
  rm -f "$ARTIFACT"
}
trap cleanup EXIT

git -C "$REPO_ROOT" archive --format=tar.gz --output="$ARTIFACT" "$EXPECTED_SHA"

SSH_OPTIONS=(
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o ConnectTimeout=15
  -o ConnectionAttempts=1
)
if [ -n "$SSH_KEY" ]; then
  SSH_OPTIONS=(-i "$SSH_KEY" "${SSH_OPTIONS[@]}")
fi

log INFO "Uploading $EXPECTED_SHA to $DEPLOY_HOST"
scp "${SSH_OPTIONS[@]}" "$ARTIFACT" "$DEPLOY_HOST:$REMOTE_ARTIFACT"

log INFO "Installing release $RELEASE_ID"
ssh "${SSH_OPTIONS[@]}" "$DEPLOY_HOST" /bin/bash <<EOF
set -euo pipefail

APP_ROOT=$(escape_shell "$APP_ROOT")
STATE_ROOT=$(escape_shell "$STATE_ROOT")
SERVICE_NAME=$(escape_shell "$SERVICE_NAME")
KEEP_RELEASES=$(escape_shell "$KEEP_RELEASES")
PYTHON_BIN=$(escape_shell "$PYTHON_BIN")
RELEASE_ID=$(escape_shell "$RELEASE_ID")
EXPECTED_SHA=$(escape_shell "$EXPECTED_SHA")
REMOTE_ARTIFACT=$(escape_shell "$REMOTE_ARTIFACT")

RELEASES_DIR="\$APP_ROOT/releases"
RELEASE_DIR="\$RELEASES_DIR/\$RELEASE_ID"
CURRENT_LINK="\$APP_ROOT/current"
UNIT_PATH="/etc/systemd/system/\$SERVICE_NAME"
ENV_PATH="/etc/default/deftpdf-deep-parse"
UNIT_BACKUP="/run/\${SERVICE_NAME}.before-\$RELEASE_ID"
PREVIOUS_CURRENT=""
HAD_UNIT=0
WAS_ENABLED=0

id deftpdf >/dev/null 2>&1 || useradd --system --create-home --home-dir "\$APP_ROOT" deftpdf
# Deployment control files stay root-owned while the running parser keeps
# write access only to state and cache data.
install -d -o root -g root -m 0755 "\$APP_ROOT" "\$RELEASES_DIR"
install -d -o deftpdf -g deftpdf -m 0700 "\$STATE_ROOT" "\$STATE_ROOT/output"
if [ -L "\$CURRENT_LINK" ]; then
  PREVIOUS_CURRENT="\$(readlink -f "\$CURRENT_LINK")"
fi
if [ -f "\$UNIT_PATH" ]; then
  install -o root -g root -m 0600 "\$UNIT_PATH" "\$UNIT_BACKUP"
  HAD_UNIT=1
fi
if systemctl is-enabled --quiet "\$SERVICE_NAME" 2>/dev/null; then
  WAS_ENABLED=1
fi

rollback() {
  local exit_code="\$?"
  if [ "\$exit_code" -eq 0 ]; then
    return
  fi

  echo "[ERROR] Deep Parse deploy failed; restoring the previous service release." >&2
  if [ -n "\$PREVIOUS_CURRENT" ]; then
    ln -sfn "\$PREVIOUS_CURRENT" "\$CURRENT_LINK"
  else
    rm -f "\$CURRENT_LINK"
  fi
  if [ "\$HAD_UNIT" -eq 1 ] && [ -f "\$UNIT_BACKUP" ]; then
    cp "\$UNIT_BACKUP" "\$UNIT_PATH"
  else
    systemctl disable "\$SERVICE_NAME" >/dev/null 2>&1 || true
    rm -f "\$UNIT_PATH"
  fi
  systemctl daemon-reload || true
  if [ "\$WAS_ENABLED" -eq 1 ]; then
    systemctl enable "\$SERVICE_NAME" >/dev/null 2>&1 || true
  else
    systemctl disable "\$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
  systemctl restart "\$SERVICE_NAME" || true
  rm -rf "\$RELEASE_DIR"
  rm -f "\$REMOTE_ARTIFACT" "\$UNIT_BACKUP"
  exit "\$exit_code"
}
trap rollback EXIT

mkdir -p "\$RELEASE_DIR"
tar -xzf "\$REMOTE_ARTIFACT" -C "\$RELEASE_DIR"
printf '%s\n' "\$EXPECTED_SHA" >"\$RELEASE_DIR/RELEASE_SHA"

chown -R root:root "\$RELEASE_DIR"
chmod -R go-w "\$RELEASE_DIR"
chown -R deftpdf:deftpdf "\$STATE_ROOT"
if [ ! -f "\$ENV_PATH" ]; then
  install -o root -g root -m 0600 "\$RELEASE_DIR/.env.example" "\$ENV_PATH"
fi

VENV_DIR="\$RELEASE_DIR/.venv"
# Build dependencies without granting the service account write access to
# release source, the current symlink, or the systemd unit source.
install -d -o deftpdf -g deftpdf -m 0755 "\$VENV_DIR"
runuser -u deftpdf -- "\$PYTHON_BIN" -m venv "\$VENV_DIR"
runuser -u deftpdf -- "\$VENV_DIR/bin/python" -m pip install --upgrade pip
CPU_ONLY="\$(grep -E '^DEEP_PARSE_CPU_ONLY=' "\$ENV_PATH" | tail -n 1 | cut -d= -f2- || true)"
CPU_ONLY="\${CPU_ONLY:-true}"
if printf '%s' "\$CPU_ONLY" | grep -Eiq '^(1|true|yes|on)$'; then
  TORCH_VERSION="\$(grep -E '^DEEP_PARSE_TORCH_VERSION=' "\$ENV_PATH" | tail -n 1 | cut -d= -f2- || true)"
  TORCHVISION_VERSION="\$(grep -E '^DEEP_PARSE_TORCHVISION_VERSION=' "\$ENV_PATH" | tail -n 1 | cut -d= -f2- || true)"
  TORCH_VERSION="\${TORCH_VERSION:-2.11.0}"
  TORCHVISION_VERSION="\${TORCHVISION_VERSION:-0.26.0}"
  runuser -u deftpdf -- "\$VENV_DIR/bin/python" -m pip install \
    --index-url https://download.pytorch.org/whl/cpu \
    "torch==\$TORCH_VERSION" \
    "torchvision==\$TORCHVISION_VERSION"
fi
runuser -u deftpdf -- "\$VENV_DIR/bin/python" -m pip install -r "\$RELEASE_DIR/requirements.txt"
test "\$(stat -c '%U:%G' "\$APP_ROOT")" = "root:root"
test "\$(stat -c '%U:%G' "\$RELEASE_DIR")" = "root:root"
runuser -u deftpdf -- test ! -w "\$RELEASE_DIR/systemd/deftpdf-deep-parse.service"

LEGACY_MODEL_DIR="\$APP_ROOT/.cache/modelscope/hub/models/OpenDataLab/PDF-Extract-Kit-1___0"
CURRENT_MODEL_PARENT="\$APP_ROOT/.cache/modelscope/models/OpenDataLab--PDF-Extract-Kit-1.0/snapshots"
CURRENT_MODEL_DIR="\$CURRENT_MODEL_PARENT/master"
if [ -d "\$LEGACY_MODEL_DIR" ] && [ ! -e "\$CURRENT_MODEL_DIR" ]; then
  install -d -o deftpdf -g deftpdf "\$CURRENT_MODEL_PARENT"
  ln -s "\$LEGACY_MODEL_DIR" "\$CURRENT_MODEL_DIR"
  chown -h deftpdf:deftpdf "\$CURRENT_MODEL_DIR"
fi

ln -sfn "\$RELEASE_DIR" "\$CURRENT_LINK"
install -o root -g root -m 0644 "\$RELEASE_DIR/systemd/deftpdf-deep-parse.service" "\$UNIT_PATH"
systemctl daemon-reload
systemctl enable "\$SERVICE_NAME"
systemctl restart "\$SERVICE_NAME"

healthy=0
for _attempt in \$(seq 1 60); do
  if health="\$(curl -fsS --max-time 5 http://127.0.0.1:18080/health 2>/dev/null)" \
    && printf '%s' "\$health" | grep -q '"status":"healthy"' \
    && printf '%s' "\$health" | grep -q '"persistent_tasks":true'; then
    healthy=1
    printf '%s\n' "\$health"
    break
  fi
  sleep 2
done

if [ "\$healthy" -ne 1 ]; then
  journalctl -u "\$SERVICE_NAME" -n 100 --no-pager >&2 || true
  exit 1
fi

test "\$(cat "\$CURRENT_LINK/RELEASE_SHA")" = "\$EXPECTED_SHA"
systemctl is-active --quiet "\$SERVICE_NAME"

find "\$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
  | sort -nr \
  | awk -v keep="\$KEEP_RELEASES" 'NR > keep {sub(/^[^ ]+ /, ""); print}' \
  | while IFS= read -r old_release; do
      [ -n "\$old_release" ] || continue
      [ "\$old_release" = "\$(readlink -f "\$CURRENT_LINK")" ] && continue
      rm -rf "\$old_release"
    done

rm -f "\$REMOTE_ARTIFACT" "\$UNIT_BACKUP"
trap - EXIT
EOF

log OK "Deployed Deep Parse $EXPECTED_SHA to $DEPLOY_HOST"
