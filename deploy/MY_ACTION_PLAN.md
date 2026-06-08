# 내 할 일 — DM 백엔드 하드닝 배포 액션 플랜 (체크리스트)

> 명령/검증 세부는 [SERVER_RUNBOOK.md](./SERVER_RUNBOOK.md) 참고. 이 문서는 **순서대로 체크하며 진행하는 요약본**.

## 현재 상태 (2026-06-08 기준)
- ✅ 코드 전부 git 브랜치 **`hardening/dm-surge`** 에 있음 (origin push 완료).
- ✅ `main` = 작업 전 체크포인트(기존 WIP 스냅샷).
- ⏸ **아직 서버 미배포.** Meta/IG **API 승인 + 사이트 검사 통과 후** 반영 예정.

---

## 전제조건 (반영 시작 전에 충족)
- [ ] **Meta/Instagram 앱 검수 승인** (DM/메시징 권한 등) — 실 DM 대량 발송의 전제.
- [ ] 저트래픽 **유지보수 창** 확보 (DB 재시작/컷오버 포함, ~1시간).
- [ ] **배포 전 CI/컨테이너에서 검증** (로컬엔 Django 미설치라 못 돌림):
  - [ ] `python manage.py check`
  - [ ] `python manage.py makemigrations --check --dry-run` (0018/0019 외 추가 diff 없는지)
  - [ ] `pytest` (특히 integrations 웹훅/DM)
- [ ] **코드 확인 2건** (구현 중 남긴 TODO):
  - [ ] `sent_dm_logs.external_account_id` 가 컬럼인지 `campaign__ig_connection` 조인인지 → 0019 인덱스/거버너 필드 확정
  - [ ] `workspace.plan` 접근 경로 (거버너 wiring 용)
  - [ ] Caddyfile 의 external-IO/webhook **경로 prefix** 를 실제 `config/api_urls.py` + `apps/pages/link_urls.py` 와 대조
  - [ ] `docker ps` 로 실제 컨테이너명 확인 (litellm 이 `litellm-proxy` 맞는지 등)

---

## 0단계 — 승인과 무관하게 "지금" 해도 되는 것 (권장: 먼저 해두기)
백업·보안은 DM 트래픽과 무관하므로 API 승인 전에 미리 해두면 좋다.

### 0-Z. (선택) 스테이징에서 하드닝 동작/처리량 검증 — Meta 호출 없음(mock)
- [ ] `docker compose -p turnflow_staging -f docker-compose.staging.yml --env-file .env.staging up -d --build`
- [ ] `loadtest_dm --seed` → `loadtest_dm --count 5000` 로 **DM/s 처리량 측정** (가이드: [STAGING_LOADTEST.md](./STAGING_LOADTEST.md))
- [ ] threads vs prefork 비교로 "얼마나 빨라졌나" 숫자 확인 + PgBouncer 커넥션 수렴 확인
- [ ] 끝나면 `down -v` 로 정리 (실 prod 와 격리되어 충돌 없음)

### 0-A. 백업 (GATE-0) — 데이터 손실 방어, 최우선
- [ ] R2 에 미디어와 **분리된 버킷** `turnflow-db-backups` + **스코프 토큰** 생성
- [ ] gpg 백업 키페어 생성 → **공개키만** 서버에 import (개인키는 박스 밖 보관)
- [ ] `cp deploy/backups/.env.backup.example .env.backup` → 값 채우고 `chmod 600`
- [ ] `apt-get install -y awscli gnupg`, `chmod +x deploy/backups/*.sh`
- [ ] `./deploy/backups/pg_backup.sh` 1회 실행 → R2 업로드 + Telegram '✅' 확인
- [ ] crontab 등록: 일일 백업 `15 4 * * *`, 월 복구드릴 `0 5 1 * *`
- [ ] R2 lifecycle 규칙 (daily 8일 / weekly 35일 / monthly 100일)
- [ ] **복구 드릴 통과**: `./deploy/backups/restore.sh` → "✅ RESTORE DRILL PASSED" (← 이게 통과해야 함)
- [ ] (권장) WAL PITR 활성화 — 유지보수 창에서 db 재시작 1회 (RUNBOOK A6)

### 0-B. 보안 (P1a)
- [ ] **LiteLLM 마스터키 회전** (`sk-master-key-admin1234` 폐기 → `openssl rand -hex 32`); LiteLLM + 앱 `.env` 양쪽 교체
- [ ] `/root/caddy/Caddyfile` 의 `llm.clfy` 블록에 IP 허용목록 또는 basic_auth 활성화 → `docker exec -w /etc/caddy caddy caddy reload --config /etc/caddy/Caddyfile`
- [ ] 평문 `.env` 시크릿(META/OPENAI/R2 등)이 git 에 올라간 적 있는지 확인 → 있으면 회전
- [ ] 검증: 무키 `curl https://llm.clfy.ai.kr/v1/models` → 401/403

---

## A단계 — 앱 배포 (유지보수 창, API 승인 후)

- [ ] `.env.production` 갱신 — PgBouncer 변수 6개:
      `DB_HOST=pgbouncer` `DB_PORT=6432` `DB_CONN_MAX_AGE=0` `DB_DISABLE_SERVER_SIDE_CURSORS=True` `DB_CONN_HEALTH_CHECKS=False` `WEBHOOK_ASYNC_MESSAGING=True`
- [ ] 코드: `git fetch origin && git checkout hardening/dm-surge && git pull`
- [ ] **한 방 배포**: `chmod +x deploy/scripts/*.sh && ./deploy/scripts/deploy.sh`
      (db/pgbouncer/redis → 게이트 마이그레이션(0018/0019) → collectstatic → web 3-tier 순차 → 워커/beat)
- [ ] **Caddyfile 교체**: `cp deploy/caddy/Caddyfile /root/caddy/Caddyfile` (prefix 대조 후) → `docker exec -w /etc/caddy caddy caddy reload --config /etc/caddy/Caddyfile`
- [ ] sysctl: `sudo cp deploy/os-tuning/99-turnflow.conf /etc/sysctl.d/ && sudo sysctl --system`
- [ ] 컷오버 검증 후 구 컨테이너 정리: `docker rm -f turnflow_instagram_web`

---

## B단계 — 그 다음 (며칠 내)

- [ ] **관측성 기동**: `docker compose -f docker-compose.prod.yml -f deploy/observability/docker-compose.obs.yml --env-file .env.production up -d` → Grafana/Flower 로 대역폭·큐·DB·계정당 발송율 확인
- [ ] **P3f 거버너 wiring** (⚠️ 스테이징 테스트 후): `apps/integrations/tasks.py` 의 `send_dm_task` 에 5줄 추가 (RUNBOOK C3) — 한 계정 과속 발송 → Meta 밴 방지
- [ ] **20K/분 합성 부하 리허설** → 검증 §전부 통과
- [ ] 통과 후 **인플루언서 온보딩 시작**

---

## 검증 한눈에 (캠페인 전 전부 green)
- [ ] 백업 복구 PASS / WAL `pgbackrest check` OK
- [ ] 무키 `llm.clfy` 401/403
- [ ] 동일 webhook event id 2회 → `webhook_event_inbox` 1행, 동시 UPDATE 에러 0
- [ ] 합성 부하: `dm_send` 처리율 ≥333/s, 큐 lag 안정 (Flower)
- [ ] `pg_stat_activity` 통합부하에도 < 300 (PgBouncer 풀로 수렴)
- [ ] Tier3 슬로우 주입에도 webhook/dashboard p95 불변 (격리 확인)
- [ ] `rollback.sh` 1회 리허설 (다운타임 측정)

## 문제 시 즉시 롤백
- 웹훅 이상 → `.env` `WEBHOOK_ASYNC_MESSAGING=False` + 워커 재시작 (재배포 불필요)
- 배포 전반 → `./deploy/scripts/rollback.sh` + `/root/caddy/Caddyfile` `[LEGACY]` 블록으로 원복 후 reload
- PgBouncer 이상 → `.env` `DB_HOST=db` `DB_PORT=5432` `DB_CONN_MAX_AGE=600` 원복 + 웹/워커 재시작

---
_참고: 전체 명령/근거/의존성 그래프는 [SERVER_RUNBOOK.md](./SERVER_RUNBOOK.md), 설계 배경은 계획서 `keen-puzzling-cascade.md`._
