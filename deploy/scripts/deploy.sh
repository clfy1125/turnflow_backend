#!/usr/bin/env bash
#
# P4d — low-downtime, digest-pinned deploy with gated migrations + one-command rollback.
# No orchestrator. DB/Redis/PgBouncer stay up; web tiers recreate one at a time behind Caddy.
#
# Usage:  deploy/scripts/deploy.sh
# Rollback:  deploy/scripts/rollback.sh        (uses .deploy.prev written below)
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root (/opt/turnflow_backend)

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.production"
TAG="$(git rev-parse --short HEAD)"
IMAGE="turnflow_instagram_web:${TAG}"

echo "==> 1/6 record current image for rollback"
# Save whatever is currently running as the rollback target.
CUR="$(docker inspect --format '{{.Config.Image}}' turnflow_instagram_web_dashboard 2>/dev/null || echo '')"
[ -n "$CUR" ] && echo "APP_IMAGE=$CUR" > .deploy.prev || echo "APP_IMAGE=turnflow_instagram_web:latest" > .deploy.prev
echo "    previous = $(cat .deploy.prev)"

echo "==> 2/6 build pinned image ($IMAGE)"
git pull origin "$(git rev-parse --abbrev-ref HEAD)"
APP_IMAGE="$IMAGE" $COMPOSE build
docker tag "$IMAGE" turnflow_instagram_web:latest

echo "==> 3/6 bring up stateful tier (no-op if already running)"
APP_IMAGE="$IMAGE" $COMPOSE up -d db pgbouncer redis

echo "==> 4/6 GATED migrations (one-shot, DIRECT to db:5432 — bypass PgBouncer txn pool)"
# Session-mode connection for DDL: override DB_HOST/PORT to hit Postgres directly.
APP_IMAGE="$IMAGE" $COMPOSE run --rm \
  -e RUN_MIGRATIONS=0 -e DB_HOST=db -e DB_PORT=5432 -e DB_CONN_MAX_AGE=0 \
  web_dashboard python manage.py migrate --noinput
echo "==> 4b/6 collectstatic (once, shared volume)"
APP_IMAGE="$IMAGE" $COMPOSE run --rm -e RUN_MIGRATIONS=0 web_dashboard python manage.py collectstatic --noinput

echo "==> 5/6 recreate web tiers one at a time (Caddy keeps routing to healthy ones)"
for svc in web_external web_dashboard web_webhook; do
  echo "    recreating $svc ..."
  APP_IMAGE="$IMAGE" $COMPOSE up -d --no-deps "$svc"
  sleep 8
done

echo "==> 6/6 recreate workers + beat"
APP_IMAGE="$IMAGE" $COMPOSE up -d --no-deps celery_dm celery_followup celery_default celery_billing celery_beat

echo "==> done. running images:"
docker ps --format '  {{.Names}}\t{{.Image}}\t{{.Status}}' | grep turnflow || true
echo "Verify: webhook p95, /api/v1/healthz, pg_stat_activity, queue lag. Rollback: deploy/scripts/rollback.sh"
