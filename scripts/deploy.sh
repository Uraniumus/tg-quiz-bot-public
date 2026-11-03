#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="${BRANCH:-master}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
SERVICE_NAME="${SERVICE_NAME:-quiz-bot.service}"
RESTART_SERVICE="${RESTART_SERVICE:-1}"

cd "$APP_DIR"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required"
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree has local changes. Commit/stash them before deploy."
  exit 1
fi

git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt

if [ "$RESTART_SERVICE" = "1" ]; then
  if [ "$(id -u)" -eq 0 ]; then
    systemctl restart "$SERVICE_NAME"
  else
    sudo systemctl restart "$SERVICE_NAME"
  fi
fi

echo "Deploy finished for branch '$BRANCH'."
