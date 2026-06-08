#!/usr/bin/env bash
#
# GATE-0 — DISASTER RECOVERY: rebuild the DB on a FRESH box after total IDC-box loss.
# (DR strategy chosen: backup→new-box, no paid cloud standby. This script pre-stages the
#  recovery so RTO is a *rehearsed* tens-of-minutes, not an improvised scramble.)
#
# Two modes:
#   A) PITR  (preferred — RPO seconds/minutes): restore base backup + replay WAL via pgBackRest.
#   B) DUMP  (fallback — RPO ≤24h): restore the latest daily logical dump from R2.
#
# Prereqs on the new box: docker, pgbackrest, gpg (private key imported), awscli,
#   /opt/turnflow_backend/.env.backup + /etc/pgbackrest/pgbackrest.conf in place.
#
# Usage:  restore_to_new_box.sh pitr  ["2026-06-08 14:30:00"]   |   restore_to_new_box.sh dump
set -euo pipefail
MODE="${1:-pitr}"
BACKUP_ENV="${BACKUP_ENV:-/opt/turnflow_backend/.env.backup}"; source "$BACKUP_ENV"
export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" AWS_DEFAULT_REGION=auto
R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
DATA_DIR="${DATA_DIR:-/var/lib/docker/volumes/turnflow_instagram_postgres/_data}"

if [ "$MODE" = "pitr" ]; then
  TARGET="${2:-}"
  echo ">> PITR restore into $DATA_DIR"
  systemctl stop docker 2>/dev/null || true
  rm -rf "${DATA_DIR:?}/"* || true
  if [ -n "$TARGET" ]; then
    pgbackrest --stanza=turnflow --type=time "--target=${TARGET}" --delta restore
  else
    pgbackrest --stanza=turnflow --delta restore     # restore to latest
  fi
  echo ">> restored. Start the db container, confirm it reaches consistency, then bring up the app."
  echo "   docker compose -f /opt/turnflow_backend/docker-compose.prod.yml up -d db"
else
  echo ">> DUMP restore (latest daily) — RPO up to 24h"
  KEY="$(aws --endpoint-url "$R2_ENDPOINT" s3 ls "s3://${R2_BUCKET_DB}/daily/" | sort | tail -1 | awk '{print $4}')"
  aws --endpoint-url "$R2_ENDPOINT" s3 cp "s3://${R2_BUCKET_DB}/daily/${KEY}" /tmp/d.gpg --only-show-errors
  gpg --batch --yes --decrypt /tmp/d.gpg > /tmp/d.dump
  docker compose -f /opt/turnflow_backend/docker-compose.prod.yml up -d db
  for i in $(seq 1 30); do docker exec turnflow_instagram_db pg_isready -U "${PGUSER}" >/dev/null 2>&1 && break; sleep 1; done
  docker exec -e PGPASSWORD="$DB_PASSWORD" turnflow_instagram_db createdb -U "$PGUSER" "$PGDATABASE" 2>/dev/null || true
  docker cp /tmp/d.dump turnflow_instagram_db:/tmp/d.dump
  docker exec -e PGPASSWORD="$DB_PASSWORD" turnflow_instagram_db \
    pg_restore -U "$PGUSER" -d "$PGDATABASE" --no-owner --clean --if-exists /tmp/d.dump
  echo ">> dump restored. Bring up the rest of the stack."
fi
