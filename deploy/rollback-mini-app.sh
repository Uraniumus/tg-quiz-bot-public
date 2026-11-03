#!/bin/sh
set -eu

APP_DIR="${APP_DIR:-/opt/tg-quiz-bot}"
BACKUP_TAG="${1:-pre-mini-app-20260827}"
cd "$APP_DIR"

git rev-parse --verify "$BACKUP_TAG^{commit}" >/dev/null
git diff --quiet
git archive "$BACKUP_TAG" | tar -x -C "$APP_DIR"

if [ -f "$APP_DIR/.env" ]; then
  BOT_TOKEN=$(grep '^BOT_TOKEN=' "$APP_DIR/.env" | cut -d= -f2-)
  if [ -n "$BOT_TOKEN" ]; then
    curl -fsS "https://api.telegram.org/bot${BOT_TOKEN}/setChatMenuButton" \
      --data-urlencode 'menu_button={"type":"commands"}' >/dev/null
  fi
fi

systemctl restart quiz-bot.service
systemctl restart quiz-share.service
systemctl is-active quiz-bot.service quiz-share.service
