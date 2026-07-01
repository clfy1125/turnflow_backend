#!/usr/bin/env bash
# GCP DR 드릴 — 안전 리허설. drill 모드 VM 프로비저닝 → 복구 체인 검증(실 CF DNS 무변경) → teardown 안내.
# 드릴 안전장치: SITE_ID=gcp-drill(콜로 권위와 분리) + restore --archive-mode=off(공유 stanza 오염 0) + DNS 안 건드림.
#
# 사용: drill.sh ["PITR_TARGET"]
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./gcp.env ] || { echo "!! gcp.env 없음"; exit 1; }
source ./gcp.env
PITR_TARGET="${1:-}"

echo "=== GCP DR 드릴 (SITE_ID=gcp-drill · archive off · DNS 무변경) ==="
bash provision.sh drill "$PITR_TARGET"

SSH="gcloud compute ssh $VM_NAME --zone $GCP_ZONE"
echo
echo ">> 1) 부팅/복구 진행 로그(별도 터미널 권장):"
echo "     $SSH -- 'sudo tail -n 300 -f /var/log/turnflow-dr-startup.log'"
echo
echo ">> 2) Telegram '🧪 DR 드릴 ... ready=200' 알림(또는 로그 'startup 완료') 뜨면 검증:"
echo "     $SSH -- 'curl -sk --resolve turnflow-api.clfy.ai.kr:443:127.0.0.1 -o /dev/null -w \"caddy_ready=%{http_code}\\n\" https://turnflow-api.clfy.ai.kr/api/v1/healthz/ready'"
echo "     # 200 이면: 복원 + migrate + dr_catchup + promote + Caddy/Origin cert 전 체인 OK"
echo "     $SSH -- 'cd /opt/turnflow_backend && docker compose -f docker-compose.prod.yml exec -T web_dashboard python manage.py shell -c \"from apps.core.site_control import get_site_state as g; print(g())\"'"
echo "     # active_site=gcp-drill, mode=live, restore_complete=True 확인"
echo
echo ">> 3) RTO 기록: provision 시각 ~ ready 시각. RPO: 복원 target vs 사고시각."
echo
echo ">> 4) 검증 끝나면 반드시 정리 → 과금 0:"
echo "     bash teardown.sh"
echo
echo "   ⚠️ 드릴은 실 CF DNS 를 절대 안 바꾼다. 실제 트래픽 전환(cf_origin.sh)은 live failover 에서만."
