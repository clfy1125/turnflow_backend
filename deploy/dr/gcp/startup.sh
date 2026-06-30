#!/usr/bin/env bash
# GCP DR cold-VM — 온부팅 스타트업 스크립트(provision.sh 가 instance metadata 로 주입).
# root 로 1회 실행. 로그: /var/log/turnflow-dr-startup.log + 직렬콘솔.
#
# 흐름: docker 설치 → Secret Manager 시크릿 적재 → 핀 clone → build → PITR 복구
#       → gated migrate → collectstatic → 스택 기동 → dr_catchup → promote
#       → (real) 새 타임라인 full backup → Caddy(Origin cert) 기동 → 멈춤(사람이 DNS 스왑).
#
# metadata attributes(provision.sh 가 설정): MODE(drill|live), PITR_TARGET, GIT_REF, REPO_URL, SITE_ID
set -euo pipefail
exec > >(tee -a /var/log/turnflow-dr-startup.log) 2>&1
echo "===== TurnFlow DR startup $(date -u) ====="

REPO_DIR=/opt/turnflow_backend
md() { curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/$1"; }
attr() { md "instance/attributes/$1" 2>/dev/null || true; }

MODE="$(attr MODE)"
[ -n "$MODE" ] || { echo "!! MODE metadata 없음 — 안전상 중단(드릴이 live 로 오인되면 실DM·공유 stanza 오염)"; exit 1; }
PITR_TARGET="$(attr PITR_TARGET)"
GIT_REF="$(attr GIT_REF)"
REPO_URL="$(attr REPO_URL)"
SITE_ID_OVERRIDE="$(attr SITE_ID)"
PROJECT="$(md project/project-id)"
DRILL=0; [ "$MODE" = "drill" ] && DRILL=1
echo ">> MODE=$MODE DRILL=$DRILL GIT_REF=$GIT_REF PROJECT=$PROJECT"

# ── 0) 기본 패키지 + docker ───────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl git python3 ca-certificates
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

# ── 1) Secret Manager 시크릿을 '먼저' 전부 적재(토큰 1h 만료 → 긴 복구 전에 받아둠) ──
ACCESS_TOKEN="$(md instance/service-accounts/default/token | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')"
fetch_secret() { # $1=secret name → stdout(plaintext)
  curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
    "https://secretmanager.googleapis.com/v1/projects/$PROJECT/secrets/$1/versions/latest:access" \
    | python3 -c 'import sys,json,base64;print(base64.b64decode(json.load(sys.stdin)["payload"]["data"]).decode("utf-8","replace"),end="")'
}

# ── 2) 핀 고정 clone ──────────────────────────────────────────────
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"
git fetch --all
git checkout "$GIT_REF"
echo ">> checked out $(git rev-parse --short HEAD)"

# ── 3) 시크릿 → 파일 배치 ─────────────────────────────────────────
umask 077
fetch_secret turnflow-dr-env-production > "$REPO_DIR/.env.production"
fetch_secret turnflow-dr-pgbackrest-conf > "$REPO_DIR/deploy/backups/pgbackrest.conf"
chmod 0644 "$REPO_DIR/deploy/backups/pgbackrest.conf"   # umask 077 무력화 — 컨테이너 postgres(uid 70)가 읽어야 함(없으면 복구 전체 실패)
mkdir -p "$REPO_DIR/deploy/dr/gcp/certs"
fetch_secret turnflow-cf-origin-cert > "$REPO_DIR/deploy/dr/gcp/certs/origin.pem"
fetch_secret turnflow-cf-origin-key  > "$REPO_DIR/deploy/dr/gcp/certs/origin.key"
# SITE_ID 보정: drill 이면 gcp-drill 로(콜로 권위와 절대 안 섞이게), 아니면 metadata/그대로.
DESIRED_SITE="${SITE_ID_OVERRIDE:-gcp}"; [ "$DRILL" = "1" ] && DESIRED_SITE="gcp-drill"
if grep -q '^SITE_ID=' "$REPO_DIR/.env.production"; then
  sed -i "s/^SITE_ID=.*/SITE_ID=${DESIRED_SITE}/" "$REPO_DIR/.env.production"
else
  echo "SITE_ID=${DESIRED_SITE}" >> "$REPO_DIR/.env.production"
fi
# DR 오버레이(워커/버퍼 축소) 병합 — 있으면.
[ -f "$REPO_DIR/deploy/dr/gcp/dr-overlay.env" ] && cat "$REPO_DIR/deploy/dr/gcp/dr-overlay.env" >> "$REPO_DIR/.env.production"
echo ">> SITE_ID=${DESIRED_SITE}"

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.production"

# ── 4) 이미지 빌드(app + db) ──────────────────────────────────────
echo ">> docker compose build"
$COMPOSE build

# ── 5) PITR 복구 ──────────────────────────────────────────────────
bash deploy/dr/gcp/restore_from_r2.sh "$PITR_TARGET" "$DRILL"

# ── 6) gated migrate(db:5432 직결) + collectstatic ───────────────
$COMPOSE run --rm --no-deps -e DB_HOST=db -e DB_PORT=5432 -e DB_CONN_MAX_AGE=0 \
  web_dashboard python manage.py migrate --noinput
$COMPOSE run --rm --no-deps web_dashboard python manage.py collectstatic --noinput | tail -2

# ── 7) 앱/워커 기동(beat 제외 — 은퇴) ─────────────────────────────
$COMPOSE up -d pgbouncer
if [ "$DRILL" = "1" ]; then
  # 드릴: DM/검증 워커(celery_dm/celery_followup) 미기동 → dm_send/verify 큐 '소비자 0'
  # → 어떤 레이스에서도 실유저 DM/공개답글 발송 불가(구조적 차단).
  $COMPOSE up -d web_webhook web_dashboard web_external
else
  $COMPOSE up -d web_webhook web_dashboard web_external celery_dm celery_followup celery_default celery_billing
fi

# ── 8) DB 기반 catch-up + 권위 승격 ───────────────────────────────
# 드릴: --dry-run → send_dm_task.delay() 적재 자체를 안 함(실발송 방지 이중화). live: 정상.
DRY=""; [ "$DRILL" = "1" ] && DRY="--dry-run"
$COMPOSE run --rm --no-deps web_dashboard python manage.py dr_catchup --skip-poll $DRY || true
# promote 는 db:5432 직결(pgbouncer 우회) — db 재기동 직후 pgbouncer 의 stale 연결로 'server terminated abnormally' 나는 것 방지.
$COMPOSE run --rm --no-deps -e DB_HOST=db -e DB_PORT=5432 -e DB_CONN_MAX_AGE=0 \
  web_dashboard python manage.py mark_restore_complete --promote --note "gcp ${MODE} $(date -u +%FT%TZ)"

# ── 9) (real 전용) 새 타임라인 full backup → R2 에 GCP 타임라인 정착 ──
R2_SETTLED=1
if [ "$DRILL" != "1" ]; then
  echo ">> (live) 새 타임라인 full backup + check"
  $COMPOSE exec -T -u postgres db pgbackrest --stanza=turnflow --type=full backup || R2_SETTLED=0
  $COMPOSE exec -T -u postgres db pgbackrest --stanza=turnflow check || R2_SETTLED=0
  [ "$R2_SETTLED" = "1" ] || echo "!! R2 정착(full backup/check) 실패 — failback 불가, DNS 스왑 전 수동 확인 필수"
fi

# ── 10) Caddy(Origin cert, tls 명시) 기동 ─────────────────────────
NET="$(docker network ls --format '{{.Name}}' | grep -m1 '^turnflow_instagram_net$' || true)"
NET="${NET:-turnflow_instagram_net}"   # compose 가 name: 으로 고정 → 프로젝트 prefix 없음
docker rm -f caddy 2>/dev/null || true
docker run -d --name caddy --restart unless-stopped \
  --network "$NET" -p 80:80 -p 443:443 \
  -v "$REPO_DIR/deploy/dr/gcp/Caddyfile.gcp:/etc/caddy/Caddyfile:ro" \
  -v "$REPO_DIR/deploy/dr/gcp/certs:/certs:ro" \
  -v caddy_data:/data -v caddy_config:/config \
  caddy:2
sleep 3
docker exec caddy caddy validate --config /etc/caddy/Caddyfile || echo "!! Caddyfile validate 실패"

# ── 11) 자기 검증 + 알림(여기서 멈춤 — DNS 스왑은 사람) ───────────
sleep 5
READY="$(curl -sk --resolve api.turnflow.clfy.ai.kr:443:127.0.0.1 -o /dev/null -w '%{http_code}' https://api.turnflow.clfy.ai.kr/api/v1/healthz/ready || echo 000)"
EXT_IP="$(md instance/network-interfaces/0/access-configs/0/external-ip)"
echo ">> /healthz/ready(local)=$READY  external-ip=$EXT_IP"

# Telegram(.env.production 의 봇 재사용) — best-effort
TG_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' .env.production | head -1 | sed -E 's/^[^=]+=//; s/^["'"'"']//; s/["'"'"']$//' || true)"
TG_CHAT="$(grep -E '^TELEGRAM_CHAT_ID=' .env.production | head -1 | sed -E 's/^[^=]+=//; s/^["'"'"']//; s/["'"'"']$//' || true)"
if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
  if [ "$DRILL" = "1" ]; then
    MSG="🧪 *DR 드릴* — GCP 박스 ready=$READY (SITE_ID=$DESIRED_SITE, IP=$EXT_IP). 실제 DNS 스왑 안 함. 검증 후 teardown.sh."
  elif [ "${R2_SETTLED:-1}" = "1" ]; then
    MSG="🟢 *DR* — GCP 복구완료 ready=$READY archive=OK (IP=$EXT_IP). **콜로 펜스 확인 후** \`cf_origin.sh $EXT_IP\` 로 전환(사람 승인)."
  else
    MSG="🟠 *DR* — GCP ready=$READY 이나 ⚠️ R2 정착 실패(failback 불가). DNS 스왑 전 새 타임라인 full backup 수동 확인 필수. IP=$EXT_IP"
  fi
  curl -s -o /dev/null -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys;print(json.dumps({"chat_id":sys.argv[1],"text":sys.argv[2],"parse_mode":"Markdown"}))' "$TG_CHAT" "$MSG")" || true
fi
echo "===== startup 완료: ready=$READY mode=$MODE ip=$EXT_IP ====="
