#!/bin/sh
set -eu

# Certificate directory: certbot deploy hooks set RENEWED_LINEAGE; otherwise
# pass the domain as the first argument or via SHARE_DOMAIN.
CERT_DIR="${RENEWED_LINEAGE:-/etc/letsencrypt/live/${1:-${SHARE_DOMAIN:?Set SHARE_DOMAIN or pass domain as first argument}}}"

install -d -o root -g quizbot -m 0750 /etc/quiz-share
install -o root -g quizbot -m 0640 "$CERT_DIR/fullchain.pem" /etc/quiz-share/fullchain.pem
install -o root -g quizbot -m 0640 "$CERT_DIR/privkey.pem" /etc/quiz-share/privkey.pem
systemctl try-restart quiz-share.service
