#!/usr/bin/env bash
# GCP DR — 일회성 셋업(운영자, gcloud). API 활성화 + 서비스계정 + 방화벽.
# 시크릿 적재는 setup_secrets.sh 가 담당(secretAccessor 바인딩 포함).
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./gcp.env ] || { echo "!! gcp.env 없음"; exit 1; }
source ./gcp.env
SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
gcloud config set project "$GCP_PROJECT" >/dev/null

echo ">> [1/3] API 활성화(compute, secretmanager)"
gcloud services enable compute.googleapis.com secretmanager.googleapis.com

echo ">> [2/3] 서비스계정 ($SA_EMAIL) — VM 에 붙어 Secret Manager 만 읽음(최소권한)"
gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --display-name "TurnFlow DR VM"
# secretAccessor 는 setup_secrets.sh 가 시크릿별로 바인딩.

echo ">> [3/3] 방화벽 — 443=Cloudflare 만, 22=운영자만 (태그 $NETWORK_TAG)"
# if/else (A&&B||C 금지 — update 실패 시 doomed create 로 빠지지 않게)
if gcloud compute firewall-rules describe turnflow-dr-https >/dev/null 2>&1; then
  gcloud compute firewall-rules update turnflow-dr-https --source-ranges="$CF_CIDRS" --rules=tcp:443
else
  gcloud compute firewall-rules create turnflow-dr-https \
    --direction=INGRESS --action=ALLOW --rules=tcp:443 \
    --source-ranges="$CF_CIDRS" --target-tags="$NETWORK_TAG"
fi
if gcloud compute firewall-rules describe turnflow-dr-ssh >/dev/null 2>&1; then
  gcloud compute firewall-rules update turnflow-dr-ssh --source-ranges="$OPERATOR_CIDR" --rules=tcp:22
else
  gcloud compute firewall-rules create turnflow-dr-ssh \
    --direction=INGRESS --action=ALLOW --rules=tcp:22 \
    --source-ranges="$OPERATOR_CIDR" --target-tags="$NETWORK_TAG"
fi

echo ">> 완료. 다음: setup_secrets.sh (시크릿 적재 + secretAccessor 바인딩)"
echo "   ⚠️ 운영자 계정에 roles/iam.serviceAccountUser ON $SA_EMAIL + compute 권한 필요(없으면 README 참고)."
