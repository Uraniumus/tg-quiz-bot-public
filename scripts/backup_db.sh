#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

DB_PATH="${DB_PATH:-$APP_DIR/quiz_bot.sqlite3}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
STAMP="$(date +%Y%m%d_%H%M%S)"
TMP_DB="$BACKUP_DIR/quiz_bot_${STAMP}.sqlite3"
ARCHIVE="$TMP_DB.gz"

mkdir -p "$BACKUP_DIR"

if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB_PATH" ".backup '$TMP_DB'"
else
  echo "sqlite3 not found, fallback to plain copy"
  cp "$DB_PATH" "$TMP_DB"
fi

gzip -f "$TMP_DB"

find "$BACKUP_DIR" -type f -name "quiz_bot_*.sqlite3.gz" -mtime "+$RETENTION_DAYS" -delete

if [ -n "${RCLONE_REMOTE:-}" ]; then
  rclone copy "$ARCHIVE" "$RCLONE_REMOTE"
fi

if [ "${SEND_TO_TELEGRAM_BACKUP:-0}" = "1" ] && [ -n "${BOT_TOKEN:-}" ] && [ -n "${BACKUP_CHAT_ID:-}" ]; then
  curl -fsS \
    -F "chat_id=$BACKUP_CHAT_ID" \
    -F "document=@$ARCHIVE" \
    -F "caption=SQLite backup $STAMP" \
    "https://api.telegram.org/bot${BOT_TOKEN}/sendDocument" >/dev/null
fi

echo "Backup created: $ARCHIVE"
