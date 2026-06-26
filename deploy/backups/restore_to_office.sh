#!/usr/bin/env bash
# DR: 사내(office) 웜스탠바이 **비파괴** PITR 복구 (pgBackRest → R2).
#
# 콜드박스용 restore_to_new_box.sh(도커 데몬 정지 + DATA_DIR 삭제)와 달리, 이미 도커가 도는
# 웜스탠바이에서 안전하게 db 볼륨만 복구한다.
#
# 전제(D-1 게이트 통과):
#   - 이 박스에서 pgBackRest stanza 'turnflow' 가 R2 repo 에 접근 가능(읽기 토큰)
#   - /etc/pgbackrest/pgbackrest.conf + PGBACKREST_CIPHER_PASS(또는 conf 내) 구성
#   - docker-compose.prod.yml(3-tier) 가 이 박스에 존재
#
# 사용:  ./restore_to_office.sh ["YYYY-MM-DD HH:MM:SS+09"]   (인자 없으면 최신까지)
set -euo pipefail

COMPOSE="docker compose -f docker-compose.prod.yml"
STANZA="turnflow"
TARGET_TIME="${1:-}"
PG_DATA_VOLUME="turnflow_instagram_postgres"   # docker volume 명 (compose 와 일치 확인)

echo "[restore] 1) 앱/워커/DB 정지 (redis 는 유지 — 어차피 fresh)"
$COMPOSE stop web_webhook web_dashboard web_external celery_dm celery_followup celery_default celery_billing celery_beat pgbouncer db || true

echo "[restore] 2) pgBackRest PITR 복구"
if [ -n "$TARGET_TIME" ]; then
  echo "    → target time: $TARGET_TIME"
  pgbackrest --stanza="$STANZA" --type=time --target="$TARGET_TIME" --delta --target-action=promote restore
else
  echo "    → 최신(default)"
  pgbackrest --stanza="$STANZA" --delta --target-action=promote restore
fi
# ※ pgbackrest 가 직접 접근하는 PGDATA 경로는 환경에 맞게(pg1-path) 구성돼 있어야 함.
#   docker volume 기반이면 호스트 마운트 경로(/var/lib/docker/volumes/${PG_DATA_VOLUME}/_data)를
#   pgbackrest.conf 의 pg1-path 로 지정하거나, db 컨테이너 안에서 pgbackrest 를 실행.

echo "[restore] 3) DB 기동 + consistency 대기"
$COMPOSE up -d db
until $COMPOSE exec -T db pg_isready -U "${DB_USER:-postgres}" >/dev/null 2>&1; do sleep 2; done

echo "[restore] 4) 마이그레이션 체크 (pgbouncer 우회 — db:5432 직결)"
# deploy.sh 와 동일 패턴: transaction-pool(6432) 이 아니라 5432 직결로 migrate.
$COMPOSE run --rm \
  -e DB_HOST=db -e DB_PORT=5432 \
  web_dashboard python manage.py migrate --check || {
    echo "[restore] 스키마 skew 감지 → migrate 적용"
    $COMPOSE run --rm -e DB_HOST=db -e DB_PORT=5432 web_dashboard python manage.py migrate --noinput
}

echo "[restore] 5) pgbouncer + 앱/워커 기동"
$COMPOSE up -d pgbouncer
$COMPOSE up -d web_webhook web_dashboard web_external celery_dm celery_followup celery_default celery_billing

echo "[restore] 완료. 다음: dr_catchup → mark_restore_complete --promote → Caddy production 스왑 (failover.sh 참고)"
