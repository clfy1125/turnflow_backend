#!/usr/bin/env bash
#
# GATE-0 / Layer 1 — daily logical PostgreSQL backup → Cloudflare R2 (offsite).
#
# WHY host cron (not Celery-beat): the backup must fire even when the app/broker
# is sick (Redis down, workers wedged during a surge) — exactly when you most
# need it. Run this from the HOST crontab, independent of the Django process tree.
#
# Install (on the server, as the deploy user):
#   chmod +x /opt/turnflow_backend/deploy/backups/pg_backup.sh
#   cp /opt/turnflow_backend/deploy/backups/.env.backup.example /opt/turnflow_backend/.env.backup
#   #  → fill in R2_*, PG*, GPG_RECIPIENT, TELEGRAM_* ; chmod 600 .env.backup
#   crontab -e
#   15 4 * * * /opt/turnflow_backend/deploy/backups/pg_backup.sh >> /var/log/turnflow_backup.log 2>&1
#
# Restore:   deploy/backups/restore.sh  (drill)   /   restore_to_new_box.sh (DR)
#
set -euo pipefail

BACKUP_ENV="${BACKUP_ENV:-/opt/turnflow_backend/.env.backup}"
# shellcheck disable=SC1090
source "$BACKUP_ENV"

: "${DB_CONTAINER:=turnflow_instagram_db}"
: "${PGUSER:=postgres}"
: "${PGDATABASE:=instagram_service}"
: "${LOCAL_DIR:=/opt/turnflow_backend/backups}"
: "${R2_ACCOUNT_ID:?set in .env.backup}"
: "${R2_BUCKET_DB:?set in .env.backup}"      # e.g. turnflow-db-backups (SEPARATE from media bucket)
: "${GPG_RECIPIENT:?set in .env.backup}"     # public-key recipient; private key lives OFF-box
: "${LOCAL_KEEP:=3}"

R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:?}"
export AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:?}"
export AWS_DEFAULT_REGION="auto"

TS="$(date +%F_%H%M)"
DOW="$(date +%u)"   # 1=Mon .. 7=Sun
DOM="$(date +%d)"
mkdir -p "$LOCAL_DIR"
DUMP="${LOCAL_DIR}/turnflow_${TS}.dump.gpg"

telegram() {
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || return 0
  curl -fsS --max-time 10 \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

trap 'telegram "🔴 [TurnFlow] pg_backup FAILED at ${TS} (host cron). Check /var/log/turnflow_backup.log"' ERR

echo "[$(date -Is)] dump start ($PGDATABASE from $DB_CONTAINER)"

# pg_dump custom format (compressed, parallel-restorable) → gpg encrypt (public key) → file.
# Client-side encryption: an R2-bucket compromise alone yields only ciphertext.
docker exec -e PGPASSWORD="${DB_PASSWORD:?set in .env.backup}" "$DB_CONTAINER" \
  pg_dump -U "$PGUSER" -d "$PGDATABASE" --format=custom --compress=6 \
  | gpg --batch --yes --encrypt --recipient "$GPG_RECIPIENT" --trust-model always -o "$DUMP"

SIZE="$(du -h "$DUMP" | cut -f1)"
echo "[$(date -Is)] dump done ($SIZE) → uploading"

aws --endpoint-url "$R2_ENDPOINT" s3 cp "$DUMP" \
  "s3://${R2_BUCKET_DB}/daily/turnflow_${TS}.dump.gpg" --only-show-errors

# Weekly (Sun) / monthly (1st) copies for tiered retention (expiry via R2 lifecycle rules).
[ "$DOW" = "7" ] && aws --endpoint-url "$R2_ENDPOINT" s3 cp "$DUMP" \
  "s3://${R2_BUCKET_DB}/weekly/turnflow_${TS}.dump.gpg" --only-show-errors || true
[ "$DOM" = "01" ] && aws --endpoint-url "$R2_ENDPOINT" s3 cp "$DUMP" \
  "s3://${R2_BUCKET_DB}/monthly/turnflow_${TS}.dump.gpg" --only-show-errors || true

# Heartbeat marker — read by apps/core/tasks.py:backup_health_check.
echo "$TS" | aws --endpoint-url "$R2_ENDPOINT" s3 cp - \
  "s3://${R2_BUCKET_DB}/_last_success_daily" --only-show-errors

# Local retention: keep the newest $LOCAL_KEEP encrypted dumps; R2 is the long-term store.
ls -1t "${LOCAL_DIR}"/turnflow_*.dump.gpg 2>/dev/null | tail -n +"$((LOCAL_KEEP + 1))" | xargs -r rm -f

echo "[$(date -Is)] backup OK ($SIZE) → r2://${R2_BUCKET_DB}/daily/turnflow_${TS}.dump.gpg"
telegram "✅ [TurnFlow] daily DB backup OK ${TS} (${SIZE})"
