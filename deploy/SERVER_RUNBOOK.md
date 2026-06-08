# SERVER RUNBOOK — DM 백엔드 하드닝 적용 (인플루언서 서지 대비, 목표 분당 10~20K)

브랜치: `hardening/dm-surge` · 서버: `/opt/turnflow_backend` (IDC 단일 박스)
원칙: **각 게이트 독립 적용 + 측정 → 다음**, 모든 위험 단계는 ~30분 롤백.

> 코드/설정 변경은 전부 이 브랜치에 들어있습니다. 아래는 **서버에서 칠 명령**과 **검증/롤백**입니다.
> 가장 아래 [§9 "내가 해야 할 행동"](#9-내가-해야-할-행동-체크리스트) 만 봐도 순서대로 실행 가능합니다.

---

## 적용 순서 (의존성)
```
GATE-0 백업  ─┐ (병렬)
P1a 보안     ─┘
   → A. DB 유지보수 창: PgBouncer + db 튜닝 + (선택)WAL 아카이빙
   → B. 앱 배포: 마이그레이션(게이트) → 3-tier 컷오버 (deploy.sh) → Caddy 라우팅 전환
   → C. OS sysctl → 관측성 → (테스트 후) P3f 거버너 wiring
```

---

## GATE-0 — 백업 (먼저, 무조건. 인플루언서 온보딩 전 완료)

### A1. R2 버킷 + 백업 시크릿
```bash
# R2 대시보드: 미디어와 분리된 버킷 'turnflow-db-backups' 생성 + 그 버킷에만 권한 있는 스코프 토큰 발급
# 백업 암호화용 gpg 키페어: 로컬에서 생성 → 공개키만 서버로
gpg --quick-generate-key "backup@turnflow.clfy.ai.kr" rsa4096 encr never
gpg --export --armor backup@turnflow.clfy.ai.kr > turnflow-backup-pub.asc   # 서버로 복사
# (개인키 gpg --export-secret-keys 는 절대 서버에 두지 말고 비밀번호 관리자/복구 노트북에 보관)

# 서버:
cd /opt/turnflow_backend
gpg --import turnflow-backup-pub.asc
cp deploy/backups/.env.backup.example .env.backup && nano .env.backup   # 값 채우기
chmod 600 .env.backup
sudo apt-get install -y awscli gnupg     # awscli + gpg
chmod +x deploy/backups/*.sh
```

### A2. 일일 논리 백업 cron (Layer 1)
```bash
./deploy/backups/pg_backup.sh           # 수동 1회 — R2 업로드 + Telegram '✅' 확인
crontab -e
# 추가:
15 4 * * *  /opt/turnflow_backend/deploy/backups/pg_backup.sh >> /var/log/turnflow_backup.log 2>&1
0  5 1 * *  /opt/turnflow_backend/deploy/backups/restore.sh    >> /var/log/turnflow_restore.log 2>&1   # 월 1회 복구 드릴
```
R2 대시보드에서 `daily/ weekly/ monthly/` 프리픽스에 lifecycle 만료 규칙(8일/35일/100일) 설정.

### A3. 복구 드릴 (이게 통과해야 다음 단계로)
```bash
./deploy/backups/restore.sh             # 스크래치 컨테이너에 최신 백업 복구 → "✅ RESTORE DRILL PASSED" 확인
```

### A6. (선택, 권장) WAL 아카이빙 PITR — Layer 2 *유지보수 창에서*
RPO 를 24h → 초/분 으로. **이게 유일하게 db 재시작이 필요한 단계.**
```bash
# 1) db 를 pgbackrest 포함 이미지로 전환: docker-compose.prod.yml 의 db 서비스에서
#    image: postgres:16-alpine  →  build: { context: ., dockerfile: deploy/postgres/Dockerfile }
# 2) /etc/pgbackrest/pgbackrest.conf 작성 (deploy/backups/pgbackrest.conf.example 참고)
# 3) postgresql.conf 에 deploy/backups/postgresql.archive.conf 내용 추가(마운트 or append)
docker exec turnflow_instagram_db pgbackrest --stanza=turnflow stanza-create
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build db   # 재시작 1회
docker exec turnflow_instagram_db pgbackrest --stanza=turnflow --type=full backup
docker exec turnflow_instagram_db pgbackrest --stanza=turnflow check                     # ✅
```
**롤백(~30분):** postgresql.archive.conf 4줄 제거 + db 이미지 원복 + 재시작.

---

## P1a — 보안 (GATE-0 과 병렬, 즉시)
```bash
# 1) 공개된 약한 LiteLLM 마스터키 회전 — sk-master-key-admin1234 폐기
#    deploy/vllm-server/.env(또는 litellm-config) 의 LLM_API_KEY 를 강한 랜덤으로 교체
openssl rand -hex 32        # 새 키 생성 → LiteLLM + 호출하는 앱(.env LLM_API_KEY) 양쪽 교체
docker compose -f deploy/vllm-server/docker-compose.yml up -d litellm
# 2) llm.clfy.ai.kr 잠금: /root/caddy/Caddyfile 의 llm 블록에서 IP 허용목록 or basic_auth 활성화
#    (Caddy 는 turnflow_instagram_net 위의 컨테이너이므로 docker exec 로 reload)
docker exec -w /etc/caddy caddy caddy reload --config /etc/caddy/Caddyfile
# 3) 평문 .env 시크릿 점검 — META_APP_SECRET/OPENAI/R2 등이 한 번이라도 git 에 올라갔는지 확인, 올라갔으면 회전
```
**검증:** `curl https://llm.clfy.ai.kr/v1/models`(무키) → 401/403, 정상 키 → 200.

---

## B. 앱 배포 (3-tier 컷오버)

### 사전: .env.production 갱신
`.env.production.example` 의 신규 변수를 반영:
```
DB_HOST=pgbouncer
DB_PORT=6432
DB_CONN_MAX_AGE=0
DB_DISABLE_SERVER_SIDE_CURSORS=True
DB_CONN_HEALTH_CHECKS=False
WEBHOOK_ASYNC_MESSAGING=True
```

### B1. 코드 가져오기
```bash
cd /opt/turnflow_backend
git fetch origin && git checkout hardening/dm-surge && git pull origin hardening/dm-surge
```

### B2. 한 방 배포 (deploy.sh — 게이트 마이그레이션 + 티어별 순차 재생성 + 롤백 기록)
```bash
chmod +x deploy/scripts/*.sh
./deploy/scripts/deploy.sh
```
deploy.sh 가 자동으로: 이전 이미지 기록 → 빌드(digest 태그) → db/pgbouncer/redis 기동 →
**마이그레이션 one-shot(db:5432 직결, 0018 EventInbox + 0019 CONCURRENTLY 인덱스)** →
collectstatic → web_external→dashboard→webhook 순차 재생성 → 워커/beat 재생성.

### B3. Caddy 라우팅 전환 (3-tier)
Caddy 는 `turnflow_instagram_net` 위의 **컨테이너**라 업스트림을 서비스 DNS 이름으로 잡는다
(`web_webhook:8000` / `web_external:8000` / `web_dashboard:8000`). ⚠️ 컷오버 전 Caddyfile 의
`turnflow_instagram_web:8000` 은 배포 후 사라지므로 **반드시 교체해야 함**.
```bash
cp deploy/caddy/Caddyfile /root/caddy/Caddyfile   # 외부-IO/webhook prefix 를 실제 URL 과 대조 후
docker exec -w /etc/caddy caddy caddy reload --config /etc/caddy/Caddyfile   # 무중단 reload ~2s
```
구 단일 컨테이너 `turnflow_instagram_web` 는 새 compose 에 없어 deploy.sh 가 건드리지 않으므로 **계속 떠 있다**
(컷오버 중 안전한 공존 — Caddy 가 옛 설정이면 그게, 새 설정이면 tier 가 서빙). 컷오버 검증 후 정리:
```bash
docker rm -f turnflow_instagram_web    # 검증 끝난 뒤에만
```

**롤백(~15~30분):** `./deploy/scripts/rollback.sh` (이전 이미지로 워커/웹 복귀) +
`/root/caddy/Caddyfile` 을 `[LEGACY]` 단일 업스트림 블록으로 되돌리고 위 reload. 마이그레이션은 가산적이라 되돌릴 필요 없음.

---

## C. OS 튜닝 / 관측성 / 거버너

### C1. sysctl (P4c)
```bash
sudo cp deploy/os-tuning/99-turnflow.conf /etc/sysctl.d/99-turnflow.conf
sudo sysctl --system
```

### C2. 관측성 (P5-obs)
```bash
docker compose -f docker-compose.prod.yml -f deploy/observability/docker-compose.obs.yml --env-file .env.production up -d
# Flower 127.0.0.1:5555 / Grafana 127.0.0.1:3001 / Prometheus 9090 — Caddy basic-auth 뒤에 노출
```
대시보드 4종: 대역폭(node), DB 쓰기/커넥션/락(pg), 큐 lag(redis/flower), 컨테이너 CPU/RAM(cadvisor).

### C3. P3f 계정당 거버너 wiring — ⚠️ 테스트 후 적용 (코드 1곳)
`apps/integrations/rate_governor.py` 는 준비됨. 발송 직전 검사로 **`apps/integrations/tasks.py` 의 `send_dm_task`** 에 5줄 추가(로컬/스테이징 테스트 후):
```python
from .rate_governor import check as _rate_check
# send_dm_task 안, 실제 API 호출 직전:
_d = _rate_check(ig_account_id=str(log.campaign.ig_connection.external_account_id),
                 plan=log.campaign.ig_connection.workspace.plan)   # 실제 필드명 확인
if not _d.allowed:
    raise self.retry(countdown=_d.retry_after)   # 드롭 아님 — 뒤로 재스케줄
```
이유: 동시성을 올리면 한 계정 과속 발송 → Meta 밴 위험. 거버너가 시간당(플랜) + 분당(버스트) 상한을 강제.

---

## 검증 (엔드투엔드 — 캠페인 전 전부 green)
1. **백업**: A3 복구 PASS, A6 `pgbackrest check` OK, 월 `restore.sh` Telegram 통과.
2. **보안**: 무키 `llm.clfy` 401/403.
3. **웹훅 멱등성**: 같은 Meta event id 2회 전송 → `webhook_event_inbox` 1행, `sent_dm_logs` 동시 UPDATE 에러 0, delivered/read 수 초 내 수렴.
4. **동시성**: 합성 부하에서 `dm_send` 큐 처리율 ≥333/s, in-flight ≥400, 큐 lag 안정 (Flower).
5. **커넥션**: `docker exec turnflow_instagram_db psql -U postgres -c "select count(*) from pg_stat_activity"` → 통합부하에도 db 서버측 conn 이 PgBouncer 풀(~40)로 수렴, <300.
6. **DB 쓰기**: pg_exporter writes/s 안정, WAL/디스크 여유.
7. **티어 격리**: Tier3 에 인위 슬로우 호출 → webhook(8001)/dashboard(8002) p95 불변.
8. **배포/롤백**: `rollback.sh` 1회 리허설(다운타임 측정).

---

## 서지 준비 게이트 체크리스트 (온보딩 전 필수)
- [ ] GATE-0 백업 + 복구 드릴 PASS
- [ ] P1a LiteLLM 키 회전 + llm 잠금
- [ ] PgBouncer + db 튜닝 적용, 앱이 6432 경유
- [ ] 0018/0019 마이그레이션 적용 (EventInbox + recipient 인덱스)
- [ ] `WEBHOOK_ASYNC_MESSAGING=True` 동작 확인
- [ ] 3-tier 컷오버 + Caddy 라우팅 + 헬스체크 동작
- [ ] Celery 큐 분리(dm_send/webhook_followup/verify/snapshot/billing) 워커 기동
- [ ] sysctl 적용, 관측성 대시보드 가동
- [ ] (테스트 후) P3f 거버너 wiring
- [ ] 20K/분 합성 부하 리허설 통과

---

## 9. 내가 해야 할 행동 (체크리스트)

순서대로. 코드는 이미 `hardening/dm-surge` 브랜치에 있음.

**즉시 (오늘, 병렬):**
1. **R2 백업 버킷 + 스코프 토큰 + gpg 공개키** 만들고 서버 `.env.backup` 채우기 → `pg_backup.sh` 1회 실행 → `restore.sh` 로 복구 PASS 확인. (GATE-0)
2. **LiteLLM 키 회전** + `llm.clfy.ai.kr` 잠금(Caddy). 평문 .env 시크릿 git 추적 여부 확인. (P1a)

**유지보수 창 (저트래픽 시간, ~1시간):**
3. `.env.production` 에 PgBouncer 변수 6개 반영(위 B 사전).
4. 서버에서 `git checkout hardening/dm-surge && ./deploy/scripts/deploy.sh` → PgBouncer/3-tier/큐분리/마이그레이션 한 번에.
5. `cp deploy/caddy/Caddyfile /root/caddy/Caddyfile` (prefix 실제 URL 과 대조) → `docker exec -w /etc/caddy caddy caddy reload --config /etc/caddy/Caddyfile`. **컷오버 전 Caddyfile 의 `turnflow_instagram_web` 는 배포 후 사라지니 필수 교체.**
6. `sudo cp deploy/os-tuning/99-turnflow.conf /etc/sysctl.d/ && sudo sysctl --system`.
7. (권장) WAL PITR(A6): db 이미지 pgbackrest 전환 + stanza-create + full backup + check.

**그 다음 (며칠 내):**
8. 관측성 스택 기동(C2) + Grafana 대시보드 확인 → 대역폭/큐/DB 모니터링.
9. **P3f 거버너 wiring(C3)** 을 스테이징에서 테스트 후 prod 반영 — 실제 `workspace.plan`/`external_account_id` 필드명 확인.
10. **20K/분 합성 부하 리허설** 로 검증 §1~8 전부 통과 후 인플루언서 온보딩.

**문제 시 즉시 롤백:**
- 웹훅 이상 → `.env` 에 `WEBHOOK_ASYNC_MESSAGING=False` + 워커 재시작 (재배포 불필요).
- 배포 전반 이상 → `./deploy/scripts/rollback.sh` + Caddyfile 원복.
- PgBouncer 이상 → `.env` `DB_HOST=db`/`DB_PORT=5432`/`DB_CONN_MAX_AGE=600` 으로 원복 후 웹/워커 재시작.

> 확인 필요 2건(구현 중 메모): ① `sent_dm_logs` 에 `external_account_id` 가 컬럼인지 조인인지(0019 인덱스/거버너 필드) ② Caddy 호스트 설정 파일 위치.
