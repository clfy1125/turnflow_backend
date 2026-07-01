#!/usr/bin/env bash
# DR FAILOVER (반자동) — 사내(office) 박스에서 실행.
# 콜로 장애 → CF LB 가 이미 office maintenance 페이지로 전환된 상태에서, 사람이 승인 후 실행.
#
# 흐름: restore → migrate gate → 앱 기동 → dmrate 재수화/dr_catchup → promote → Caddy 스왑.
# 상세: DR_IMPLEMENTATION_PLAN.md §8.2.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/turnflow_backend}"   # 이 박스의 레포 경로
COMPOSE="docker compose -f docker-compose.prod.yml"
CADDY_ETC="/etc/caddy"
TARGET_TIME="${1:-}"                              # PITR 목표 시각(생략=최신)

cd "$REPO_DIR"

read -r -p "⚠️  콜로 장애를 확인했고 office 로 FAILOVER 합니까? (yes/no) " ans
[ "$ans" = "yes" ] || { echo "중단"; exit 1; }

echo "[failover] 1) PITR 복구 + 마이그레이션 + 앱 기동"
bash deploy/backups/restore_to_office.sh "$TARGET_TIME"

echo "[failover] 2) DB 기반 catch-up (Redis 큐 재구성; poll 은 1차 생략)"
$COMPOSE run --rm web_dashboard python manage.py dr_catchup --skip-poll

echo "[failover] 3) 권위 승격 (active_site=office, epoch++, restore_complete=True)"
$COMPOSE run --rm web_dashboard python manage.py mark_restore_complete --promote --note "failover $(cat /etc/hostname)"

echo "[failover] 4) Caddy maintenance → production(3-tier) 스왑"
cp "$REPO_DIR/deploy/caddy/Caddyfile" "$CADDY_ETC/Caddyfile"
docker exec -w "$CADDY_ETC" caddy caddy validate --config "$CADDY_ETC/Caddyfile"
docker exec -w "$CADDY_ETC" caddy caddy reload   --config "$CADDY_ETC/Caddyfile"

echo "[failover] 완료. CF LB office 풀이 /healthz/ready=200 을 보면 실 앱 서빙."
echo "          확인:  curl -fsS https://turnflow-api.clfy.ai.kr/api/v1/healthz/ready"
echo "          ⚠️ 되돌아온 콜로는 stale epoch 라 passive 유지됨. failback 은 수동 승인(failback.sh)."
