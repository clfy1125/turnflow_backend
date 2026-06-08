#!/usr/bin/env bash
#
# P4d — one-command rollback to the previously-running image (written by deploy.sh as .deploy.prev).
# Recreates web tiers + workers on the old image. DB/Redis/PgBouncer untouched.
# NOTE: rollback does NOT auto-revert DB migrations — keep migrations backwards-compatible
#       (this plan uses additive-only migrations: new EventInbox table + CONCURRENTLY indexes).
set -euo pipefail
cd "$(dirname "$0")/../.."

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.production"
[ -f .deploy.prev ] || { echo "no .deploy.prev — nothing to roll back to"; exit 1; }
# shellcheck disable=SC1091
source .deploy.prev
echo "==> rolling back to APP_IMAGE=$APP_IMAGE"

for svc in web_webhook web_dashboard web_external celery_dm celery_followup celery_default celery_billing celery_beat; do
  APP_IMAGE="$APP_IMAGE" $COMPOSE up -d --no-deps "$svc"
done
echo "==> rolled back. If the issue is in the Caddy routing, also revert the Caddyfile and 'caddy reload'."
docker ps --format '  {{.Names}}\t{{.Image}}\t{{.Status}}' | grep turnflow || true
