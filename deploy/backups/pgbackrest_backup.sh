#!/usr/bin/env bash
#
# WS-1 ② — pgBackRest 베이스 백업(full/diff) → R2 (WAL PITR 의 base anchor).
#
# WHY: WAL 아카이빙(연속)만으론 복구 시 replay 구간이 무한히 길어지고, retention 도
# 새 백업을 떠야 트리거됨. 이 cron 이 base 를 정기 갱신 + 종료 시 expire 로 옛 WAL/백업 정리
# → R2 용량을 유계로 유지. (DR_IMPLEMENTATION_PLAN.md §15.3)
#
# pgBackRest 는 db 컨테이너 안에서 돈다(deploy/backups/Dockerfile.db) → docker compose exec 로 구동.
#
# 설치(서버, deploy 유저):
#   chmod +x /opt/turnflow_backend/deploy/backups/pgbackrest_backup.sh
#   crontab -e   # 아래 2줄 (KST 가정; 서버 TZ 가 UTC 면 시각을 환산: 03:00 KST = 18:00 UTC 전일)
#   #  0  3 * * 2          .../pgbackrest_backup.sh full >> /var/log/turnflow_pgbackrest.log 2>&1   # 화 03:00 주1 full
#   # 30  3 * * 0,1,3,4,5,6 .../pgbackrest_backup.sh diff >> /var/log/turnflow_pgbackrest.log 2>&1   # 그 외 매일 03:30 diff
#
# ⚠️ full 은 surge 와 I/O 경쟁 → 반드시 최저 트래픽 창. db 볼륨에 WAL 백로그 여유(≥ 수십 GB) 확보.
#
set -euo pipefail

TYPE="${1:?usage: $0 full|diff}"
case "$TYPE" in
  full|diff|incr) ;;
  *) echo "invalid type: $TYPE (full|diff|incr)" >&2; exit 2 ;;
esac

PROJECT_DIR="${PROJECT_DIR:-/opt/turnflow_backend}"
STANZA="${PGBACKREST_STANZA:-turnflow}"
COMPOSE="docker compose -f ${PROJECT_DIR}/docker-compose.prod.yml --env-file ${PROJECT_DIR}/.env.production"

# Telegram 알림(있으면). pg_backup.sh 와 동일하게 .env.backup 에서 토큰 로드.
BACKUP_ENV="${BACKUP_ENV:-${PROJECT_DIR}/.env.backup}"
# shellcheck disable=SC1090
[ -f "$BACKUP_ENV" ] && source "$BACKUP_ENV" || true

telegram() {
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || return 0
  curl -fsS --max-time 10 \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

trap 'telegram "🔴 [TurnFlow] pgBackRest ${TYPE} backup FAILED (host cron). /var/log/turnflow_pgbackrest.log 확인"' ERR

echo "[$(date -Is)] pgBackRest ${TYPE} backup start (stanza=${STANZA})"

# -T: cron 에는 TTY 없음. -u postgres: 로컬 소켓 peer 인증.
$COMPOSE exec -T -u postgres db pgbackrest --stanza="$STANZA" --type="$TYPE" backup
$COMPOSE exec -T -u postgres db pgbackrest --stanza="$STANZA" check

INFO="$($COMPOSE exec -T -u postgres db pgbackrest --stanza="$STANZA" info 2>/dev/null | head -n 20 || true)"
echo "[$(date -Is)] pgBackRest ${TYPE} backup OK"
echo "$INFO"
telegram "✅ [TurnFlow] pgBackRest ${TYPE} backup OK ($(date +%F_%H%M))"
