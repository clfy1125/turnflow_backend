#!/usr/bin/env bash
# GCP DR: 컨테이너 기반 PITR 복구. db 컨테이너에 내장된 pgbackrest + 컨테이너경로 pgbackrest.conf 재사용
# → 호스트 pgbackrest/호스트경로 불필요(콜로와 동일 실행 모델). startup.sh 가 호출.
#
# 사용: restore_from_r2.sh [PITR_TARGET] [DRILL]
#   PITR_TARGET : "2026-06-30 12:00:00+09" (생략=최신까지 재생)
#   DRILL=1     : --archive-mode=off → 공유 R2 stanza 에 WAL 안 씀(타임라인 오염 방지, 드릴 필수)
set -euo pipefail
cd /opt/turnflow_backend
COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.production"
STANZA="turnflow"
TARGET="${1:-}"
DRILL="${2:-0}"

ARCH_OPT=""
if [ "$DRILL" = "1" ]; then
  ARCH_OPT="--archive-mode=off"
  echo "[restore] DRILL 모드 → --archive-mode=off (공유 stanza 아카이브 금지)"
fi

echo "[restore] 1) db 볼륨 생성 + 빈 클러스터 초기화(initdb)"
$COMPOSE up -d db
until $COMPOSE exec -T db pg_isready -U postgres >/dev/null 2>&1; do sleep 2; done

echo "[restore] 2) postgres 정지(복구 위해 PGDATA 잠금 해제)"
$COMPOSE stop db

echo "[restore] 3) pgBackRest 복구(컨테이너 내부 일회성, R2 read) target='${TARGET:-LATEST}' drill=$DRILL"
# --delta: initdb 클러스터 위에 차등 복원(불일치 파일 덮어쓰기/잉여 제거).
if [ -n "$TARGET" ]; then
  # PITR 특정 시각으로 복구 후 승격.
  $COMPOSE run --rm --no-deps -u postgres --entrypoint pgbackrest db \
    --stanza="$STANZA" --type=time --target="$TARGET" --delta --target-action=promote $ARCH_OPT restore
elif [ "$DRILL" = "1" ]; then
  # 드릴: 백업 일관성 지점까지만 복구 후 '즉시' 승격(아카이브 WAL 재생 불필요 → 빠름, RPO=마지막 백업).
  # latest(=type=default)는 마지막 백업 이후 아카이브 WAL 을 segment 단위로 전부 재생하느라 매우 느림 → 드릴엔 부적합.
  $COMPOSE run --rm --no-deps -u postgres --entrypoint pgbackrest db \
    --stanza="$STANZA" --type=immediate --target-action=promote --delta $ARCH_OPT restore
else
  # LIVE 최신: 아카이브 WAL 전부 재생(RPO≈분) 후 끝에서 자동 승격. target-action 은 type 있을 때만 유효 → 미지정.
  $COMPOSE run --rm --no-deps -u postgres --entrypoint pgbackrest db \
    --stanza="$STANZA" --delta $ARCH_OPT restore
fi

echo "[restore] 4) db 기동 + 승격 대기 (immediate/time=즉시, latest=아카이브 WAL량 비례로 길 수 있음)"
$COMPOSE up -d db
until $COMPOSE exec -T db pg_isready -U postgres >/dev/null 2>&1; do sleep 2; done
# recovery 종료(=읽기쓰기 승격) 대기. drill/time 은 즉시, live-latest 는 WAL 전부 재생까지 길 수 있어 넉넉히.
in_recovery=""
for i in $(seq 1 300); do   # 최대 ~15분
  in_recovery="$($COMPOSE exec -T db psql -U postgres -tAc 'SELECT pg_is_in_recovery();' 2>/dev/null | tr -d '[:space:]')"
  [ "$in_recovery" = "f" ] && break
  [ $((i % 10)) -eq 0 ] && echo "    ...승격 대기(${i}) — latest 는 마지막 백업 이후 WAL segment 를 전부 재생 중"
  sleep 3
done
if [ "$in_recovery" != "f" ]; then
  # ⚠️ 자동 강제 pg_promote 안 함 — 미재생 WAL 손실(데이터 유실) 위험. db 로그 확인 후 운영자가 수동 판단.
  echo "[restore] ⚠️ 아직 recovery(읽기전용). WAL 재생이 매우 길거나 멈춤 → db 로그 확인:"
  echo "[restore]    docker compose -f docker-compose.prod.yml --env-file .env.production logs --tail 40 db"
  echo "[restore]    (정말 멈춤이고 RPO 손실 감수면 수동) ... exec -u postgres db psql -U postgres -c 'SELECT pg_promote();'"
fi
echo "[restore] 복구 완료 (pg_is_in_recovery=${in_recovery:-?})"

# 드릴 fail-closed 가드: archive_mode 가 실제 off 인지 검증. 어떤 이유로든 on 이면 공유 R2 'turnflow'
# stanza 에 드릴 타임라인 WAL 이 섞일 위험 → 즉시 중단. + 잔존 archive_command(공유 stanza 포인터) 제거.
if [ "$DRILL" = "1" ]; then
  AM="$($COMPOSE exec -T db psql -U postgres -tAc 'SHOW archive_mode;' 2>/dev/null | tr -d '[:space:]')"
  if [ "$AM" != "off" ]; then
    echo "!! DRILL 인데 archive_mode=$AM (off 아님) → 공유 stanza 오염 위험. 중단."; exit 1
  fi
  $COMPOSE exec -T db psql -U postgres -c "ALTER SYSTEM RESET archive_command;" >/dev/null 2>&1 || true
  echo "[restore] drill guard OK: archive_mode=off + archive_command reset(지뢰 제거)"
fi
