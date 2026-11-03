#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 /path/to/backup.sqlite3.gz"
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
BACKUP_FILE="$1"
SERVICE_NAME="${SERVICE_NAME:-quiz-bot.service}"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

DB_PATH="${DB_PATH:-$APP_DIR/quiz_bot.sqlite3}"
TMP_FILE="$(mktemp)"

systemctl_cmd() {
  if [ "$(id -u)" -eq 0 ]; then
    systemctl "$@"
  else
    sudo systemctl "$@"
  fi
}

if systemctl_cmd is-active --quiet "$SERVICE_NAME"; then
  systemctl_cmd stop "$SERVICE_NAME"
fi

gunzip -c "$BACKUP_FILE" > "$TMP_FILE"
mv "$TMP_FILE" "$DB_PATH"

systemctl_cmd start "$SERVICE_NAME"

echo "Database restored from $BACKUP_FILE"
