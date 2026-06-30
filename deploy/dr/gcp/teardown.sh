#!/usr/bin/env bash
# GCP DR — VM 삭제 + 정적 IP 해제 → 과금 0 복귀. (방화벽/SA/시크릿은 유지)
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./gcp.env ] || { echo "!! gcp.env 없음"; exit 1; }
source ./gcp.env

echo ">> 삭제 대상:"
echo "   VM = $VM_NAME (zone $GCP_ZONE)"
echo "   정적IP = $STATIC_IP_NAME (region $GCP_REGION)"
read -r -p "정말 삭제? (yes/no) " a; [ "$a" = "yes" ] || { echo "중단"; exit 1; }

gcloud compute instances delete "$VM_NAME" --zone "$GCP_ZONE" --quiet || echo "  (VM 없음/이미 삭제)"
# 정적 IP: 미할당 상태로 두면 소액 과금 → 해제(다음 provision 이 재예약). live 운영 중엔 해제 금지.
gcloud compute addresses delete "$STATIC_IP_NAME" --region "$GCP_REGION" --quiet || echo "  (IP 없음/사용중)"

echo ">> 완료 — 과금 0. 다음 드릴/복구는 provision.sh(또는 drill.sh) 만 다시 실행."
