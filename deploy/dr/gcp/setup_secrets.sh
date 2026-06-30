#!/usr/bin/env bash
# GCP DR — 일회성: 로컬 시크릿 파일들을 Secret Manager 에 적재 + SA 에 secretAccessor 바인딩.
# 파일 경로는 환경변수로 오버라이드(기본 ./secrets/). 값은 화면에 안 찍음.
#
# 준비(./secrets/ 에 두거나 *_FILE 로 지정):
#   .env.production   ← 콜로 것 복사 후 SITE_ID=gcp (startup 이 한 번 더 보정하지만 맞춰두면 안전)
#   pgbackrest.conf   ← 콜로 것 그대로(R2 키 + cipher, pg1-path=/var/lib/postgresql/data 컨테이너경로)
#   origin.pem/.key   ← Cloudflare Origin Certificate(인증서/개인키)
#   cf-api-token.txt  ← CF API 토큰(Zone.DNS:Edit, clfy.ai.kr 단일 존) — 한 줄
#   .env.backup       ← (선택) dump 폴백용
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./gcp.env ] || { echo "!! gcp.env 없음"; exit 1; }
source ./gcp.env
SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
gcloud config set project "$GCP_PROJECT" >/dev/null

put() { # $1=secret name, $2=file, $3=required(1/0)
  local name="$1" file="$2" req="${3:-1}"
  if [ ! -f "$file" ]; then
    [ "$req" = "1" ] && { echo "!! 필수 파일 없음: $file ($name)"; exit 1; } || { echo "  (건너뜀, 선택: $name)"; return 0; }
  fi
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    gcloud secrets versions add "$name" --data-file="$file" >/dev/null
  else
    gcloud secrets create "$name" --replication-policy=automatic --data-file="$file" >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:$SA_EMAIL" --role=roles/secretmanager.secretAccessor >/dev/null
  echo "  ok: $name"
}

ENV_PROD_FILE="${ENV_PROD_FILE:-./secrets/.env.production}"
# SITE_ID=gcp 사전 점검(경고만 — startup 이 어차피 보정)
if [ -f "$ENV_PROD_FILE" ] && ! grep -q '^SITE_ID=gcp' "$ENV_PROD_FILE"; then
  echo "  ⚠️ $ENV_PROD_FILE 의 SITE_ID 가 gcp 아님(startup 이 보정하지만 확인 권장)"
fi

put "$SEC_ENV_PROD"   "$ENV_PROD_FILE"                              1
put "$SEC_PGBACKREST" "${PGBACKREST_FILE:-./secrets/pgbackrest.conf}" 1
put "$SEC_CF_CERT"    "${CF_CERT_FILE:-./secrets/origin.pem}"       1
put "$SEC_CF_KEY"     "${CF_KEY_FILE:-./secrets/origin.key}"        1
put "$SEC_CF_TOKEN"   "${CF_TOKEN_FILE:-./secrets/cf-api-token.txt}" 1
put "$SEC_ENV_BACKUP" "${ENV_BACKUP_FILE:-./secrets/.env.backup}"   0

echo ">> 완료. ./secrets/ 는 적재 후 안전 삭제 권장(시크릿은 Secret Manager 에 있음)."
echo "   확인: gcloud secrets list --filter='name~turnflow'"
