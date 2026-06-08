#!/usr/bin/env bash
#
# GATE-0 — Layer 1 RESTORE DRILL (the #1 reason backups fail is never testing restore).
# Restores the latest (or a named) daily dump into a THROWAWAY container and runs sanity checks.
# NEVER points at production. Safe to run any time.
#
# Usage:
#   deploy/backups/restore.sh                       # latest daily
#   deploy/backups/restore.sh turnflow_2026-06-08_0415.dump.gpg
#
set -euo pipefail

BACKUP_ENV="${BACKUP_ENV:-/opt/turnflow_backend/.env.backup}"
# shellcheck disable=SC1090
source "$BACKUP_ENV"

R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto

TEST_CONTAINER="turnflow_restore_test"
TEST_DB="turnflow_restore_test"
WORK="$(mktemp -d)"
trap 'docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

KEY="${1:-}"
if [ -z "$KEY" ]; then
  KEY="$(aws --endpoint-url "$R2_ENDPOINT" s3 ls "s3://${R2_BUCKET_DB}/daily/" | sort | tail -1 | awk '{print $4}')"
fi
echo "Restoring: daily/$KEY"

aws --endpoint-url "$R2_ENDPOINT" s3 cp "s3://${R2_BUCKET_DB}/daily/${KEY}" "${WORK}/d.gpg" --only-show-errors
gpg --batch --yes --decrypt "${WORK}/d.gpg" > "${WORK}/d.dump"

# Throwaway Postgres (same major version as prod: 16).
docker run -d --name "$TEST_CONTAINER" -e POSTGRES_PASSWORD=test postgres:16-alpine >/dev/null
for i in $(seq 1 30); do docker exec "$TEST_CONTAINER" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

docker exec "$TEST_CONTAINER" createdb -U postgres "$TEST_DB"
docker cp "${WORK}/d.dump" "${TEST_CONTAINER}:/tmp/d.dump"
docker exec "$TEST_CONTAINER" pg_restore -U postgres -d "$TEST_DB" --no-owner --clean --if-exists /tmp/d.dump || true

echo "=== sanity row counts ==="
for tbl in sent_dm_logs auto_dm_campaigns ig_account_connections; do
  cnt="$(docker exec "$TEST_CONTAINER" psql -U postgres -d "$TEST_DB" -tAc "SELECT count(*) FROM $tbl" 2>/dev/null || echo 'N/A')"
  echo "  $tbl = $cnt"
done

telegram() {
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || return 0
  curl -fsS --max-time 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

DMC="$(docker exec "$TEST_CONTAINER" psql -U postgres -d "$TEST_DB" -tAc 'SELECT count(*) FROM sent_dm_logs' 2>/dev/null || echo 0)"
if [ "${DMC:-0}" -gt 0 ] 2>/dev/null; then
  echo "✅ RESTORE DRILL PASSED (sent_dm_logs=$DMC) from daily/$KEY"
  telegram "✅ [TurnFlow] monthly restore drill PASSED (sent_dm_logs=$DMC, $KEY)"
else
  echo "⚠️  RESTORE produced sent_dm_logs=$DMC — verify table name / dump contents."
  telegram "🔴 [TurnFlow] restore drill SUSPECT: sent_dm_logs=$DMC ($KEY). Investigate."
fi
