#!/usr/bin/env bash
#
# P4d — one-command rollback to the previously-running image (written by deploy.sh as .deploy.prev).
# Recreates web tiers + workers on the old image. DB/Redis/PgBouncer untouched.
# NOTE: rollback does NOT auto-revert DB migrations — keep migrations backwards-compatible
#       (this plan uses additive-only migrations: new EventInbox table + CONCURRENTLY indexes).
#
# ⚠️ 서비스 목록은 deploy.sh 6/6 과 **정확히 같아야 한다**. 2026-07-30 까지 어긋나 있었다:
#   - `celery_beat` 가 들어 있었다 → 치명적. beat 는 은퇴(외부 cron→/internal/scheduler/tick,
#     DR §6)라 compose 에서 `profiles: [fallback]` 로 빼놨는데, **서비스명을 명시하면
#     compose 가 프로필을 무시하고 기동**한다. 그러면 CF tick 과 beat 가 동시에 돌아
#     주기잡이 이중 발사되고, DM 발송 계열 태스크가 두 번 나갈 수 있다.
#   - `celery_ai` 가 빠져 있었다 → 롤백 후 ai_jobs 워커만 새 이미지로 남는 code skew.
set -euo pipefail
cd "$(dirname "$0")/../.."

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.production"
[ -f .deploy.prev ] || { echo "no .deploy.prev — nothing to roll back to"; exit 1; }
# shellcheck disable=SC1091
source .deploy.prev
echo "==> rolling back to APP_IMAGE=$APP_IMAGE"

# web 은 한 번에 다 내리지 않고 하나씩 (Caddy 가 살아있는 tier 로 계속 라우팅).
for svc in web_external web_dashboard web_webhook; do
  echo "    rolling back $svc ..."
  APP_IMAGE="$APP_IMAGE" $COMPOSE up -d --no-deps "$svc"
  sleep 8
done
# 워커 — beat 는 절대 넣지 말 것 (위 주석). 목록은 deploy.sh 6/6 과 동일하게 유지한다.
APP_IMAGE="$APP_IMAGE" $COMPOSE up -d --no-deps celery_dm celery_followup celery_default celery_billing celery_ai
echo "==> rolled back. If the issue is in the Caddy routing, also revert the Caddyfile and 'caddy reload'."
docker ps --format '  {{.Names}}\t{{.Image}}\t{{.Status}}' | grep turnflow || true
