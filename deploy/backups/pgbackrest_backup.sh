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
pgb() { $COMPOSE exec -T -u postgres db pgbackrest --stanza="$STANZA" "$@"; }

# ── 1) 백업 (expire 가 이 안에 포함돼 있다) ────────────────────────────────
# ⚠️ `backup` 은 마지막에 expire 를 내부 호출한다. 백업 세트는 정상 저장됐는데 **expire
# 만** 실패해도 rc≠0 이라, 예전 구조(`set -e` 로 즉사)에서는 뒤따르는 `check` 가 통째로
# 건너뛰어졌다. 2026-08-24·08-31 full 이 연달아 이렇게 죽어 2주간 복구 검증이 없었고,
# 옛 WAL 6.4GB 가 R2 에 잔존했다. 그래서 rc 를 받아두고 끝까지 진행한다.
BACKUP_RC=0
pgb --type="$TYPE" backup || BACKUP_RC=$?

# ── 2) expire 단독 재시도 ─────────────────────────────────────────────────
# expire 가 안 돌면 베이스 백업이 사라진 구간의 WAL 이 R2 에 영구 잔존한다(재생할 베이스가
# 없으니 복구엔 못 쓰는데 용량만 먹는다). backup 의 rc 만으로는 실패 지점이 expire 인지
# 구분할 수 없으므로 무조건 한 번 더 돌린다 — expire 는 멱등이라 이미 정리됐으면
# "no archive to remove" 로 즉시 끝난다.
EXPIRE_RC=0
if [ "$BACKUP_RC" -ne 0 ]; then
  echo "[$(date -Is)] backup rc=${BACKUP_RC} → expire 단독 재시도"
  pgb expire || EXPIRE_RC=$?
fi

# ── 3) check 는 성공·실패 무관하게 항상 돌린다 ────────────────────────────
# "백업 파일이 있다" 와 "그 백업으로 복구할 수 있다" 는 다른 명제다. 부분 실패했을 때
# 오히려 더 알아야 하는 정보라, 위에서 무슨 일이 있었든 검증은 반드시 수행한다.
CHECK_RC=0
pgb check || CHECK_RC=$?

INFO="$(pgb info 2>/dev/null | head -n 20 || true)"
echo "$INFO"

if [ "$BACKUP_RC" -eq 0 ] && [ "$CHECK_RC" -eq 0 ]; then
  echo "[$(date -Is)] pgBackRest ${TYPE} backup OK"
  telegram "✅ [TurnFlow] pgBackRest ${TYPE} backup OK ($(date +%F_%H%M))"
  exit 0
fi

# 부분 실패 — 무엇이 깨졌는지 알림에 담는다. "백업 없음"과 "정리 실패"는 심각도가 다르다.
MSG="⚠️ [TurnFlow] pgBackRest ${TYPE} 부분 실패 ($(date +%F_%H%M)) — backup rc=${BACKUP_RC}"
if [ "$BACKUP_RC" -ne 0 ]; then
  MSG="${MSG} / expire 재시도 rc=${EXPIRE_RC}"
fi
MSG="${MSG} / check rc=${CHECK_RC}"
if [ "$CHECK_RC" -eq 0 ]; then
  MSG="${MSG} — check 통과: 복구 가능한 백업은 존재한다. 정리(expire)가 실패했다면 R2 용량이 계속 늘어난다."
else
  MSG="${MSG} — 🔴 check 실패: 복구 가능성 미확인. 즉시 조사."
fi
MSG="${MSG} /var/log/turnflow_pgbackrest.log"
echo "$MSG"
telegram "$MSG"
exit 1
