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

echo "==> 1/6 record current image for rollback"
# Save whatever is currently running as the rollback target.
CUR="$(docker inspect --format '{{.Config.Image}}' turnflow_instagram_web_dashboard 2>/dev/null || echo '')"
[ -n "$CUR" ] && echo "APP_IMAGE=$CUR" > .deploy.prev || echo "APP_IMAGE=turnflow_instagram_web:latest" > .deploy.prev
echo "    previous = $(cat .deploy.prev)"

echo "==> 2/6 pull, then pin image tag to the NEW HEAD"
# ⚠️ TAG 는 **pull 이후에** 계산해야 한다. pull 전에 계산하면 새 코드를 '현재 실행 중인
# 이미지와 같은 태그'로 빌드해 덮어쓰고, 방금 .deploy.prev 에 적어둔 **롤백 대상이
# 파괴된다**(롤백해도 새 코드가 뜬다). 2026-07-29 배포에서 발견.
#
# 그리고 이 스크립트를 돌리기 전에 `git pull` 을 먼저 해두는 것을 권장한다 — bash 는
# 스크립트를 바이트 오프셋으로 읽어가므로, 실행 도중 pull 이 이 파일을 바꾸면 이후
# 라인을 잘못 읽을 수 있다. 미리 당겨두면 아래 pull 이 no-op 이 되어 그 위험도 없다.
git pull origin "$(git rev-parse --abbrev-ref HEAD)"
TAG="$(git rev-parse --short HEAD)"
IMAGE="turnflow_instagram_web:${TAG}"
echo "    building $IMAGE"
APP_IMAGE="$IMAGE" $COMPOSE build
docker tag "$IMAGE" turnflow_instagram_web:latest

echo "==> 3/6 bring up stateful tier (no-op if already running)"
APP_IMAGE="$IMAGE" $COMPOSE up -d db pgbouncer redis

echo "==> 4/6 GATED migrations (one-shot, DIRECT to db:5432 — bypass PgBouncer txn pool)"
# Session-mode connection for DDL: override DB_HOST/PORT to hit Postgres directly.
# --no-deps 필수: 없으면 .env.production 변경 시 compose가 의존 서비스(db!)를 재생성해
# 수 초간 DB 블립이 난다 (2026-07-14 실측).
APP_IMAGE="$IMAGE" $COMPOSE run --rm --no-deps \
  -e RUN_MIGRATIONS=0 -e DB_HOST=db -e DB_PORT=5432 -e DB_CONN_MAX_AGE=0 \
  web_dashboard python manage.py migrate --noinput
echo "==> 4b/6 collectstatic (once, shared volume)"
APP_IMAGE="$IMAGE" $COMPOSE run --rm --no-deps -e RUN_MIGRATIONS=0 web_dashboard python manage.py collectstatic --noinput

echo "==> 5/6 recreate web tiers one at a time (Caddy keeps routing to healthy ones)"
for svc in web_external web_dashboard web_webhook; do
  echo "    recreating $svc ..."
  APP_IMAGE="$IMAGE" $COMPOSE up -d --no-deps "$svc"
  sleep 8
done

echo "==> 6/6 recreate workers (celery_beat RETIRED — 외부 cron→/internal/scheduler/tick 으로 이관, DR §6)"
# celery_beat 는 profiles:[fallback] 라 평상시 기동 안 함(이중 발사 방지). 긴급 폴백만 수동 기동.
APP_IMAGE="$IMAGE" $COMPOSE up -d --no-deps celery_dm celery_followup celery_default celery_billing celery_ai

echo "==> done. running images:"
docker ps --format '  {{.Names}}\t{{.Image}}\t{{.Status}}' | grep turnflow || true
echo "Verify: webhook p95, /api/v1/healthz, pg_stat_activity, queue lag. Rollback: deploy/scripts/rollback.sh"
