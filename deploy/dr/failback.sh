#!/usr/bin/env bash
# DR FAILBACK (수동 승인 전용) — office → colo 복귀. 가장 위험한 방향.
#
# ⚠️ 자동 failback 절대 금지: office 가 active 인 동안 office DB 가 최신 원본이다.
#    콜로가 살아났다고 트래픽을 되돌리면 과거 DB 로 회귀(데이터 손실)한다.
#
# 안전 절차(요약, 상세 DR_IMPLEMENTATION_PLAN.md §8.3):
#   1) office 계속 active 유지
#   2) 콜로 write 펜스 (콜로 스택 정지 + ufw)
#   3) office 백업(R2)에서 콜로 DB 재시드 (콜로의 과거 DB 폐기/격리)
#   4) 콜로 /healthz/ready 검증 → FAILBACK_READY
#   5) 사람 승인 → 콜로 promote(epoch++) → CF LB 콜로 복귀 → office standby
#
# 이 스크립트는 단계별 **승인 게이트**만 제공하는 골격이다. 각 단계는 환경에 맞게 채울 것.
set -euo pipefail

confirm() { read -r -p "$1 (yes/no) " a; [ "$a" = "yes" ] || { echo "중단"; exit 1; }; }

echo "=== DR FAILBACK (office → colo) ==="
echo "전제: office 가 현재 active. 아래 각 단계를 사람이 확인하며 진행."

confirm "STEP1) 콜로에서 write 펜스(스택 정지 + ufw)를 적용했습니까?"
confirm "STEP2) 콜로의 기존(과거) DB 를 격리/폐기했습니까?"
confirm "STEP3) office 백업(R2)에서 콜로 DB 재시드(restore_to_new_box.sh / pgbackrest restore)를 완료했습니까?"
confirm "STEP4) 콜로에서 'migrate --check' 통과 + /healthz/ready(또는 디버그포트)로 앱 검증을 마쳤습니까?"

echo "→ FAILBACK_READY. 콜로에서 다음을 실행하세요(콜로 SITE_ID=colo):"
echo "    docker compose -f docker-compose.prod.yml run --rm web_dashboard \\"
echo "      python manage.py mark_restore_complete --promote --note 'failback'"
echo "    cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile && \\"
echo "      docker exec -w /etc/caddy caddy caddy validate --config /etc/caddy/Caddyfile && \\"
echo "      docker exec -w /etc/caddy caddy caddy reload   --config /etc/caddy/Caddyfile"
echo "그 다음 CF LB steering 을 콜로 우선으로 복귀, office 는 maintenance Caddy + demote 로 standby 전환."
echo "    (office) python manage.py mark_restore_complete --demote --note 'standby after failback'"
echo "⚠️ pgBackRest 타임라인 split 주의: office promote 가 새 타임라인을 만들었으면, 콜로 재시드 후"
echo "   새 base backup + 'pgbackrest check' 로 타임라인을 정합화하고, 콜로 archive_mode=on 재기동은 그 후."
