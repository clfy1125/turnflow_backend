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
# ⚠️ 반드시 **web_webhook 만** 빌드한다 (인자 없는 `build` 금지).
# 앱 이미지는 web_webhook 하나만 `build:` 컨텍스트를 갖고 web_dashboard/web_external/
# celery_* 는 `image: *app_image` 로 공유하므로 **결과 이미지는 완전히 동일**하다.
# 그런데 인자를 안 주면 buildx bake 가 db 서비스의 이미지(deploy/backups/Dockerfile,
# turnflow_instagram_db_pgbackrest:16)까지 함께 구워 manifest 가 새로 생기고, 그러면
# 아래 3/6 의 `up -d db` 가 **running db 컨테이너를 재생성**한다(~48초 DB 블립).
# 2026-06-30·07-07·07-14·07-15·07-16 배포에서 매번 재현됐고 2026-07-30 에 이렇게 제거.
APP_IMAGE="$IMAGE" $COMPOSE build web_webhook
docker tag "$IMAGE" turnflow_instagram_web:latest

echo "==> 3/6 bring up stateful tier (이미 running 이면 손대지 않는다)"
# `up -d db pgbouncer redis` 를 무조건 호출하면, config drift(이미지 manifest 변경·
# .env.production 수정)를 compose 가 감지해 **정상 동작 중인 db 를 재생성**한다.
# 위에서 build 를 한정해 주 원인은 없앴지만, env 변경만으로도 같은 일이 벌어지므로
# (2026-07-14 실측) "떠 있으면 스킵"으로 이중 방어한다. 내려가 있을 때만 올린다.
for svc in db pgbouncer redis; do
  cid="$(APP_IMAGE="$IMAGE" $COMPOSE ps -q "$svc" 2>/dev/null || true)"
  if [ -n "$cid" ] && [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" = "true" ]; then
    echo "    $svc already running — skip (재생성 방지)"
  else
    echo "    starting $svc ..."
    APP_IMAGE="$IMAGE" $COMPOSE up -d --no-deps "$svc"
  fi
done

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

# ── celery_reports (2026-08-05 추가) ──────────────────────────────────────────
# 왜 따로 두는가: 리포트 1건이 13~18분이다. 다른 워커처럼 무조건 재생성하면 진행 중인
# 리포트가 죽는다(compose 의 stop grace 는 10초, `--max-tasks-per-child=1` 이라 warm
# shutdown 이 15분을 기다려주지 않는다). 그래서 **큐가 비고 진행 중 태스크가 없을 때만** 바꾼다.
#
# 이걸 아예 빼두면(2026-08-04 까지 그랬다) 배포마다 celery_reports 만 옛 이미지로 남아
# 코드 스큐가 쌓인다 — 실제로 web 이 15커밋 앞선 상태가 발생했다.
echo "==> 6b/6 recreate celery_reports (진행 중 리포트가 없을 때만)"
_rq="$(docker exec turnflow_instagram_redis sh -c \
        'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" llen reports' 2>/dev/null | tr -d '\r' || echo '?')"
# grep -c 는 매칭 0건이면 exit 1 이다 → `|| echo '?'` 를 쓰면 "0건"과
#   "확인불가"가 뒤섞인다(2026-08-05 실제로 그 버그로 항상 SKIP 됐다).
#   출력 유무로 두 경우를 가른다.
_ract_raw="$(docker exec "$($COMPOSE ps -q celery_reports 2>/dev/null | head -1)" \
              celery -A config inspect active -t 10 2>/dev/null)"
if [ -z "$_ract_raw" ]; then
  _ract='?'
else
  _ract="$(printf '%s' "$_ract_raw" | grep -c 'insta_reports')"
fi
echo "    reports 큐=${_rq} 진행중=${_ract}"
if [ "${_rq}" = "0" ] && [ "${_ract}" = "0" ]; then
  APP_IMAGE="$IMAGE" $COMPOSE up -d --no-deps celery_reports
else
  echo "    ⏸ SKIPPED — 진행 중 리포트 보호. 완료 후 아래를 직접 실행하세요:"
  echo "       APP_IMAGE=$IMAGE $COMPOSE up -d --no-deps celery_reports"
fi

echo "==> done. running images:"
docker ps --format '  {{.Names}}\t{{.Image}}\t{{.Status}}' | grep turnflow || true
echo "Verify: webhook p95, /api/v1/healthz, pg_stat_activity, queue lag. Rollback: deploy/scripts/rollback.sh"
# 이미지 스큐 자동 점검 — 앱 컨테이너가 전부 같은 태그인지 (스킵된 celery_reports 를 놓치지 않게)
echo "==> image skew check (expect all = $IMAGE)"
docker ps --format '{{.Names}}' | grep -E '^turnflow' | while read -r c; do
  img="$(docker inspect --format '{{.Config.Image}}' "$c" 2>/dev/null)"
  case "$img" in
    turnflow_instagram_web:*) [ "$img" = "$IMAGE" ] || echo "    ✗ SKEW: $c = $img";;
  esac
done
echo "    (위에 ✗ 가 없으면 스큐 없음)"
