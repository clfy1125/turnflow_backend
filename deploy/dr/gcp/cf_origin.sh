#!/usr/bin/env bash
# Cloudflare DNS 스왑 — turnflow-api.clfy.ai.kr A 레코드를 지정 IP 로(proxied=on). 트래픽 전환(사람 승인).
# 양방향: cf_origin.sh <GCP_IP>(전환) / cf_origin.sh "$COLO_IP"(failback 복귀).
# 토큰: env CF_API_TOKEN 우선, 없으면 Secret Manager(turnflow-cf-api-token).
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./gcp.env ] || { echo "!! gcp.env 없음"; exit 1; }
source ./gcp.env
NEW_IP="${1:?사용: cf_origin.sh <NEW_IP>   (GCP 전환) | cf_origin.sh \$COLO_IP (복귀)}"

TOKEN="${CF_API_TOKEN:-}"
[ -n "$TOKEN" ] || TOKEN="$(gcloud secrets versions access latest --secret="$SEC_CF_TOKEN" --project="$GCP_PROJECT")"
API=https://api.cloudflare.com/client/v4
HDR=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json")
jqpy() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }

ZONE_ID="$(curl -s "${HDR[@]}" "$API/zones?name=$CF_ZONE_NAME" | jqpy 'd["result"][0]["id"] if d.get("result") else ""')"
[ -n "$ZONE_ID" ] || { echo "!! zone '$CF_ZONE_NAME' 못 찾음(토큰 권한/존 이름 확인)"; exit 1; }
REC="$(curl -s "${HDR[@]}" "$API/zones/$ZONE_ID/dns_records?type=A&name=$CF_RECORD_NAME")"
REC_ID="$(echo "$REC" | jqpy 'd["result"][0]["id"] if d.get("result") else ""')"
CUR_IP="$(echo "$REC" | jqpy 'd["result"][0]["content"] if d.get("result") else ""')"
[ -n "$REC_ID" ] || { echo "!! A record '$CF_RECORD_NAME' 못 찾음"; exit 1; }

echo ">> $CF_RECORD_NAME :  $CUR_IP  →  $NEW_IP  (proxied)"
# GCP 로 전환(=콜로 아님)이면 split-brain 1순위 방어: 콜로 펜스 선확인 강제(echo 안내가 아니라 게이트).
if [ "$NEW_IP" != "$COLO_IP" ]; then
  echo "   ⚠️ 콜로가 살아있으면(false-failover) 공유 R2 stanza 경합 + 이중 primary 위험."
  read -r -p "   콜로 펜스 완료? (콜로 스택 정지 + 443 차단, 또는 콜로 db archive_mode=off) (yes/no) " f
  [ "$f" = "yes" ] || { echo "중단 — 콜로 펜스 먼저 하세요"; exit 1; }
fi
read -r -p "⚠️ 트래픽을 전환합니다. 진행? (yes/no) " a; [ "$a" = "yes" ] || { echo "중단"; exit 1; }

BODY="$(python3 -c 'import json,sys;print(json.dumps({"type":"A","name":sys.argv[1],"content":sys.argv[2],"proxied":True,"ttl":1}))' "$CF_RECORD_NAME" "$NEW_IP")"
RESP="$(curl -s -X PATCH "${HDR[@]}" "$API/zones/$ZONE_ID/dns_records/$REC_ID" --data "$BODY")"
[ "$(echo "$RESP" | jqpy 'd.get("success")')" = "True" ] || { echo "!! 스왑 실패: $RESP"; exit 1; }

echo ">> 스왑 완료(프록시라 엣지 반영 수초). 검증:"
sleep 4
curl -s -o /dev/null -w "   $ORIGIN/healthz/ready = %{http_code}\n" "$ORIGIN/api/v1/healthz/ready"
# 콜로가 아직 443 응답하면 split-brain 경고(능동 프로브 — 안내문이 아니라 실측).
if [ "$NEW_IP" != "$COLO_IP" ] && [ -n "$COLO_IP" ]; then
  CC="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "https://$COLO_IP/api/v1/healthz/ready" 2>/dev/null || echo 000)"
  if [ "$CC" != "000" ]; then
    echo "   ⚠️⚠️ 콜로($COLO_IP) 가 아직 443 응답(code=$CC) — split-brain 위험! 즉시 콜로 스택 정지 + 443 차단."
  else
    echo "   ✓ 콜로($COLO_IP) 443 무응답 — 펜스 OK."
  fi
fi
