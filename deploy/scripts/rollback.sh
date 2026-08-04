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

# celery_reports — deploy.sh 6b/6 과 같은 이유로 조건부(리포트 1건 13~18분). 빼두면 롤백 후
# celery_reports 만 **새** 이미지로 남아 반대 방향 스큐가 생긴다.
_rq="$(docker exec turnflow_instagram_redis sh -c \
        'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" llen reports' 2>/dev/null | tr -d '\r' || echo '?')"
_ract="$(docker exec "$($COMPOSE ps -q celery_reports 2>/dev/null | head -1)" \
          celery -A config inspect active -t 10 2>/dev/null | grep -c 'insta_reports' || echo '?')"
echo "    celery_reports: 큐=${_rq} 진행중=${_ract}"
if [ "${_rq}" = "0" ] && [ "${_ract}" = "0" ]; then
  APP_IMAGE="$APP_IMAGE" $COMPOSE up -d --no-deps celery_reports
else
  echo "    ⏸ SKIPPED — 진행 중 리포트 보호. 완료 후: APP_IMAGE=$APP_IMAGE $COMPOSE up -d --no-deps celery_reports"
fi

echo "==> rolled back. If the issue is in the Caddy routing, also revert the Caddyfile and 'caddy reload'."
docker ps --format '  {{.Names}}\t{{.Image}}\t{{.Status}}' | grep turnflow || true
echo "==> image skew check (expect all = $APP_IMAGE)"
docker ps --format '{{.Names}}' | grep -E '^turnflow' | while read -r c; do
  img="$(docker inspect --format '{{.Config.Image}}' "$c" 2>/dev/null)"
  case "$img" in
    turnflow_instagram_web:*) [ "$img" = "$APP_IMAGE" ] || echo "    ✗ SKEW: $c = $img";;
  esac
done
