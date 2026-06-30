#!/usr/bin/env bash
# DR FAILBACK (gcp → colo) — 수동 승인 전용. 가장 위험한 방향.
#
# ⚠️ GCP 가 active 인 동안 **GCP DB 가 최신 원본**. 콜로가 살아났다고 트래픽 되돌리면 과거 DB 회귀(손실).
#    자동 failback 절대 금지. 이 스크립트는 단계별 승인 게이트 + 안내만 제공(deploy/dr/failback.sh 의 GCP판).
#
# 권위 타임라인 체인: colo(원본) → gcp(promote,+1) → colo(재시드+promote,+1)
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./gcp.env ] || { echo "!! gcp.env 없음"; exit 1; }
source ./gcp.env
confirm() { read -r -p "$1 (yes/no) " a; [ "$a" = "yes" ] || { echo "중단"; exit 1; }; }

echo "=== DR FAILBACK (gcp → colo) — 수동 승인 ==="
echo "전제: 현재 GCP 가 active(원본). 아래 각 단계를 사람이 확인하며 진행."
echo

confirm "STEP1) 콜로 write 펜스 적용? (콜로 스택 정지 + 443 차단 — 직접 도달로 인한 split-brain 차단)"
confirm "STEP2) 콜로의 과거 DB 격리/폐기? (사고 이전 타임라인이라 폐기)"
echo "STEP3) 콜로 DB 를 **GCP 가 쓴 새 타임라인**으로 재시드:"
echo "   - 선행: GCP startup(live)의 full backup 이 R2 turnflow stanza 에 새 타임라인 정착(알림 archive=OK)인지 확인."
echo "   - 콜로에서(컨테이너 내장 pgbackrest — 권장, 호스트 바이너리/경로 불필요):"
echo "       cd /opt/turnflow_backend"
echo "       C=\"docker compose -f docker-compose.prod.yml --env-file .env.production\""
echo "       \$C stop db"
echo "       \$C run --rm --no-deps -u postgres --entrypoint pgbackrest db --stanza=turnflow --delta --target-action=promote restore"
echo "       \$C up -d db"
echo "     (호스트 pgbackrest 가 별도 설치돼 있을 때만 restore_to_new_box.sh — 볼륨명 prefix/경로 주의)"
confirm "STEP3) 콜로 재시드 완료?"
confirm "STEP4) 콜로 'migrate --check' 통과 + /healthz/ready(디버그포트) 검증 완료?"

echo
echo "→ FAILBACK_READY. 콜로에서(SITE_ID=colo) 실행:"
echo "    docker compose -f docker-compose.prod.yml run --rm --no-deps web_dashboard \\"
echo "      python manage.py mark_restore_complete --promote --note 'failback to colo'"
echo "    # 콜로 Caddy production 유지(이미 colo Caddyfile). 그다음 DNS 복귀:"
echo "    bash deploy/dr/gcp/cf_origin.sh $COLO_IP        # api.turnflow → 콜로"
echo "    # GCP 강등:"
echo "    (gcp) docker compose ... run --rm --no-deps web_dashboard python manage.py mark_restore_complete --demote --note 'standby after failback'"
echo "    bash deploy/dr/gcp/teardown.sh                  # GCP VM 정리 → 과금 0"
echo
echo "⚠️ pgBackRest 타임라인: 콜로 promote 가 또 새 타임라인을 만든다 → 콜로 재시드 후"
echo "   콜로에서 새 full backup + 'pgbackrest check' 로 정합화하고, 콜로 archive_mode=on 재개는 그 후."
