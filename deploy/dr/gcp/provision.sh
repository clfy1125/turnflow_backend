#!/usr/bin/env bash
# GCP DR — VM 프로비저닝(운영자 실행, gcloud 필요). 정적 IP 예약 + VM 생성(startup.sh 주입).
# 멱등: 같은 이름 VM 이 이미 있으면 실패(중복 방지) — 새로 하려면 teardown.sh 먼저.
#
# 사용: provision.sh [live|drill] ["PITR_TARGET"]
#   live  : 실제 failover 용(복구 후 사람이 cf_origin.sh 로 DNS 스왑)
#   drill : 드릴(SITE_ID=gcp-drill, archive 끔, DNS 안 건드림)
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./gcp.env ] || { echo "!! gcp.env 없음 — gcp.env.example 복사해서 채우세요"; exit 1; }
source ./gcp.env
MODE="${1:-live}"; PITR_TARGET="${2:-}"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
gcloud config set project "$GCP_PROJECT" >/dev/null

echo ">> [1/2] 정적 외부 IP 예약(있으면 재사용)"
gcloud compute addresses describe "$STATIC_IP_NAME" --region "$GCP_REGION" >/dev/null 2>&1 \
  || gcloud compute addresses create "$STATIC_IP_NAME" --region "$GCP_REGION"
IP="$(gcloud compute addresses describe "$STATIC_IP_NAME" --region "$GCP_REGION" --format='value(address)')"
echo "   static IP = $IP"

echo ">> [2/2] VM 생성 ($VM_NAME, $MACHINE_TYPE, $DISK_SIZE $DISK_TYPE, mode=$MODE)"
SITE_TAG=$([ "$MODE" = "drill" ] && echo "gcp-drill" || echo "gcp")   # 메타데이터 SITE_ID 를 모드와 일치
gcloud compute instances create "$VM_NAME" \
  --zone "$GCP_ZONE" --machine-type "$MACHINE_TYPE" \
  --image-family "$IMAGE_FAMILY" --image-project "$IMAGE_PROJECT" \
  --boot-disk-size "$DISK_SIZE" --boot-disk-type "$DISK_TYPE" \
  --service-account "$SA_EMAIL" \
  --scopes "https://www.googleapis.com/auth/cloud-platform" \
  --address "$IP" --tags "$NETWORK_TAG" \
  --metadata "MODE=${MODE},PITR_TARGET=${PITR_TARGET},GIT_REF=${GIT_REF},REPO_URL=${REPO_URL},SITE_ID=${SITE_TAG}" \
  --metadata-from-file "startup-script=startup.sh"

echo
echo ">> VM 생성됨. 부팅/복구 진행 로그:"
echo "   gcloud compute ssh $VM_NAME --zone $GCP_ZONE -- 'sudo tail -n 200 -f /var/log/turnflow-dr-startup.log'"
echo ">> 복구 완료(ready=200, Telegram 알림) 후:"
if [ "$MODE" = "drill" ]; then
  echo "   드릴 검증 끝나면 →  bash teardown.sh    (실 DNS 안 건드림)"
else
  echo "   트래픽 전환(사람 승인) →  bash cf_origin.sh $IP"
fi
