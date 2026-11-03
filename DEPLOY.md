# Deploy on Ubuntu server

## 1. Prepare server

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip sqlite3 curl
sudo useradd -m -s /bin/bash quizbot || true
sudo mkdir -p /opt/tg-quiz-bot
sudo chown -R quizbot:quizbot /opt/tg-quiz-bot
```

## 2. Clone repo

Do it as `quizbot` user (via deploy key or personal access token):

```bash
sudo -u quizbot -H bash -lc 'cd /opt && git clone <REPO_URL> tg-quiz-bot'
```

## 3. Configure env

```bash
sudo -u quizbot -H bash -lc 'cd /opt/tg-quiz-bot && cp .env.example .env'
sudo -u quizbot -H bash -lc 'nano /opt/tg-quiz-bot/.env'
```

Required keys:
- `BOT_TOKEN`
- `ADMIN_USERNAMES`
- `DB_PATH` (recommended absolute path)

## 4. First deploy

```bash
sudo chmod +x /opt/tg-quiz-bot/scripts/*.sh
sudo -u quizbot -H bash -lc 'cd /opt/tg-quiz-bot && BRANCH=master RESTART_SERVICE=0 ./scripts/deploy.sh'
```

## 5. Enable systemd service

```bash
sudo cp /opt/tg-quiz-bot/deploy/quiz-bot.service /etc/systemd/system/quiz-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now quiz-bot.service
sudo systemctl status quiz-bot.service
```

## 6. Update from `master`

After merge to `master`:

```bash
ssh <server> 'sudo -u quizbot -H bash -lc "cd /opt/tg-quiz-bot && BRANCH=master SERVICE_NAME=quiz-bot.service ./scripts/deploy.sh"'
```

This does:
- `git pull --ff-only` from `master`
- install/update Python dependencies
- restart bot service

## 7. Logs and errors

Runtime logs:

```bash
sudo journalctl -u quiz-bot.service -n 200 --no-pager
sudo journalctl -u quiz-bot.service -f
```

If bot crashes, `systemd` auto-restarts it (`Restart=always`).

## 8. Database backups

### Local daily backup (systemd timer)

```bash
sudo cp /opt/tg-quiz-bot/deploy/quiz-bot-backup.service /etc/systemd/system/
sudo cp /opt/tg-quiz-bot/deploy/quiz-bot-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quiz-bot-backup.timer
sudo systemctl list-timers | grep quiz-bot-backup
```

### Cloud backup (recommended)

1. Install/configure `rclone`.
2. Set `RCLONE_REMOTE` in `.env` (example: `b2:quiz-bot-backups` or `s3:quiz-bot-backups`).
3. Run manual test:

```bash
cd /opt/tg-quiz-bot
sudo -u quizbot -H ./scripts/backup_db.sh
```

### Backup to Telegram (fallback)

Set in `.env`:
- `SEND_TO_TELEGRAM_BACKUP=1`
- `BACKUP_CHAT_ID=<main_admin_chat_id>`

This sends backup archive via Bot API after each backup run.

## 9. Restore DB from backup

```bash
cd /opt/tg-quiz-bot
sudo ./scripts/restore_db.sh /opt/tg-quiz-bot/backups/quiz_bot_YYYYMMDD_HHMMSS.sqlite3.gz
```

Script stops service, restores DB, starts service again.

## 10. Auto deploy from GitHub Actions

Workflow file is already in repo:
- `.github/workflows/deploy.yml`

### GitHub secrets

In GitHub repo settings add:
- `DEPLOY_HOST` - server IP or domain
- `DEPLOY_USER` - SSH user on server
- `DEPLOY_SSH_KEY` - private SSH key content for this user

If SSH port is not `22`, add `port: <your_port>` in `.github/workflows/deploy.yml`.

### Server sudo permissions for CI user

Workflow runs deploy commands with `sudo`, so allow NOPASSWD for deploy user:

```bash
sudo visudo -f /etc/sudoers.d/quiz-bot-deploy
```

Add (replace `DEPLOY_USER` with your real username):

```text
DEPLOY_USER ALL=(quizbot) NOPASSWD: /bin/bash
DEPLOY_USER ALL=(root) NOPASSWD: /bin/systemctl
```

After that, each push to `main` triggers deploy automatically.
