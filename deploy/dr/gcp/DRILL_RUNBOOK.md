# GCP Cold-VM DR 드릴 런북 (DRILL_RUNBOOK)

> **목적**: 콜로(colo) → GCP cold-VM 복구 체인 전체를 **프로덕션 트래픽·공유 백업을 건드리지 않고** 리허설한다.
> 이 문서는 **미래의 운영자(사람/에이전트)가 그대로 재현**할 수 있도록 2026-06-30 격리 드릴 + 2026-07-01 실(live) 컷오버 훈련에서 실제로 실행·검증된 절차를 기록한 것이다.
>
> - **설계 배경/결정 로그**: [`../../../DR_IMPLEMENTATION_PLAN.md`](../../../DR_IMPLEMENTATION_PLAN.md)
> - **운영자 개요 런북**: [`README.md`](README.md) (파일 표·1회성 셋업·비용·안전규칙)
> - **레거시(office 웜스탠바이 + CF LB)**: [`../DR_STEP3_4_RUNBOOK.md`](../DR_STEP3_4_RUNBOOK.md) — 폐기됨(2026-06-30), LB/failback 설계 배경용으로만 보존
> - **감지/알림(Phase A)**: [`../cloudflare/README.md`](../cloudflare/README.md) — `turnflow-scheduler-tick` 워커. 이 드릴의 범위 밖.

---

## 0. 모델 한눈에

```
콜로(primary, 121.126.99.70, /opt/turnflow_backend, SITE_ID=colo)
    │  장애 감지(Phase A) → 사람 판단
    ▼
운영자 노트북에서 deploy/dr/gcp/ 스크립트 실행
    │
    ├─ provision.sh  →  GCP cold VM 생성 (turnflow-487816 / asia-northeast3-a / e2-standard-16)
    │
    └─ (VM 부팅 시 자동) startup.sh:
          Secret Manager pull → 핀 SHA git clone → R2 pgBackRest PITR 복구
          → gated migrate → 스택 기동 → dr_catchup → mark_restore_complete --promote (epoch++)
          → Caddy(CF Origin cert) → ready=200 에서 정지
    │
    ▼
[사람 승인]  cf_origin.sh <GCP_IP>  →  Cloudflare DNS 스왑(turnflow-api → GCP IP, proxied)
    │
    ▼
teardown.sh  →  VM 삭제 + 정적 IP 해제 → 과금 $0
```

**드릴(drill)과 실(live)의 유일한 차이 = 프로덕션 격리 3종 세트**:

| | 드릴(`drill.sh` / `provision.sh drill`) | 실 컷오버(`provision.sh live`) |
|---|---|---|
| `SITE_ID` | `gcp-drill` (콜로 권위와 절대 안 섞임) | `gcp` |
| pgBackRest | `--archive-mode=off` (공유 스탠자에 WAL 안 씀) | 새 타임라인 full backup + `pgbackrest check` |
| Celery DM/verify 워커 | **미기동** (dm_send/verify 큐 소비자 0) + `dr_catchup --dry-run` → 실제 DM 0건 | 전 워커 기동 |
| Cloudflare DNS | **안 건드림** | `cf_origin.sh` 로 스왑(사람 게이트) |
| 웹훅 재구독 | 스킵(실 Meta 안 건드림) | `resubscribe_webhooks` 실행 |

> ⚠️ `startup.sh` 는 `MODE` 메타데이터가 없으면 **즉시 abort** 한다 — 드릴이 실로 오인돼 실 DM을 쏘거나 공유 R2 스탠자를 오염시키는 사고 방지.

---

## 1. 사전 준비 (1회성 + 매 드릴 확인)

### 1.1 운영자 노트북
- `gcloud` 설치 + `gcloud auth login`. 계정에 `turnflow-487816` 프로젝트의 `roles/compute.admin`(또는 owner) + DR 서비스계정(`turnflow-dr-sa@turnflow-487816.iam.gserviceaccount.com`)에 대한 `roles/iam.serviceAccountUser`.

### 1.2 1회성 GCP 셋업 (멱등)
```bash
cd deploy/dr/gcp
bash setup_gcp.sh      # compute+secretmanager API 활성화, SA(turnflow-dr-sa) 생성,
                       # 방화벽 turnflow-dr-https(CF CIDR:443)/turnflow-dr-ssh(OPERATOR_CIDR:22), 타깃태그 turnflow-dr
bash setup_secrets.sh  # Secret Manager 에 5개 시크릿 로드, SA 를 secretAccessor 로 바인딩
```
**필수 시크릿 5개** (`gcloud secrets list --filter='name~turnflow'` 로 확인):
| 시크릿 | 내용 |
|---|---|
| `turnflow-dr-env-production` | 콜로 `.env.production` 사본(SITE_ID=gcp; 드릴 모드면 startup 이 gcp-drill 로 재교정) |
| `turnflow-dr-pgbackrest-conf` | 콜로 `pgbackrest.conf` 그대로(R2 read 키 + cipher, `pg1-path=/var/lib/postgresql/data` 컨테이너 경로) |
| `turnflow-cf-origin-cert` / `turnflow-cf-origin-key` | Cloudflare Origin 인증서 PEM + 키 |
| `turnflow-cf-api-token` | Zone.DNS:Edit, `clfy.ai.kr` 단일 존 최소권한 |
> 🔐 로드 후 로컬 `./secrets/` 는 삭제. Origin 개인키·pgbackrest cipher 는 Secret Manager + 오프사이트에만 존재(분실 = 복구 불가).

### 1.3 `deploy/dr/gcp/gcp.env` (gitignore, 매 드릴 확인)
```
GCP_PROJECT=turnflow-487816
GCP_ZONE=asia-northeast3-a
MACHINE_TYPE=e2-standard-16          # ← 16 vCPU 필수. §5 gotcha 참조
DISK_SIZE=300GB (pd-ssd)
VM_NAME=turnflow-dr
STATIC_IP_NAME=turnflow-dr-ip
OPERATOR_CIDR=<이 노트북 공인 IP>/32  # curl https://api.ipify.org 로 확인. 바뀌면 SSH 막힘
GIT_REF=<콜로의 현재 배포 SHA>         # 브랜치 아닌 정확한 SHA 핀
COLO_IP=121.126.99.70
CF_ZONE_NAME=clfy.ai.kr
CF_RECORD_NAME=turnflow-api.clfy.ai.kr   # ← 1단계 서브도메인(§5 gotcha)
```
- **실(live) 컷오버 전 반드시**: 핀된 `GIT_REF` 가 콜로의 현재 배포 SHA 와 같은지 확인.
  `ssh colo 'cd /opt/turnflow_backend && git rev-parse HEAD'` → 다르면 `gcp.env` 갱신.
- Cloudflare SSL 모드 = **Full (strict)**. 현재 로드된 인증서는 `*.clfy.ai.kr` 와일드카드(v2)로, 1단계 호스트 `turnflow-api.clfy.ai.kr` 를 커버함(§5 와일드카드 누출 주의).

---

## 2. 드릴 실행 절차

### Phase 0 — 사전점검(노트북)
```bash
cd deploy/dr/gcp
cat gcp.env                                   # PROJECT/ZONE/MACHINE_TYPE/GIT_REF 확인
curl https://api.ipify.org                    # OPERATOR_CIDR 와 일치하나? 다르면 gcp.env 갱신 후 setup_gcp.sh 재실행
gcloud compute instances list                 # turnflow-dr 잔존 VM 없어야 함(provision 은 비멱등, 있으면 실패 → teardown 먼저)
gcloud secrets list --filter='name~turnflow'  # 시크릿 5개 확인
```

### Phase 1 — 드릴 VM 프로비저닝
```bash
bash drill.sh                              # 최신 백업 consistency 지점으로 복구(빠름, 권장)
# 또는 특정 PITR 시각 테스트:
bash drill.sh "2026-06-30 12:00:00+09"
```
- `drill.sh` → `provision.sh drill "$PITR_TARGET"`: 정적 IP `turnflow-dr-ip` 예약/재사용 → `gcloud compute instances create turnflow-dr`(asia-northeast3-a, e2-standard-16, ubuntu-2204-lts, 300GB pd-ssd, SA+cloud-platform scope, 태그 turnflow-dr) → 메타데이터 `MODE=drill PITR_TARGET=… GIT_REF=… SITE_ID=gcp-drill` + `startup-script=startup.sh` 주입.
- 격리 3종(SITE_ID=gcp-drill / archive off / DNS 미변경)이 여기서 세팅돼 하류에서 강제된다.
- provision 이 정적 IP 와 ssh 로그팔로우 명령을 출력 → **IP 메모**.

### Phase 2 — startup.sh 자동 실행 (VM 위, cold ~8분+)
진행 관찰:
```bash
gcloud compute ssh turnflow-dr --zone asia-northeast3-a -- \
  'sudo tail -n 300 -f /var/log/turnflow-dr-startup.log'
```
순서:
1. `MODE` 메타 없으면 abort(가드).
2. docker 설치 → **Secret Manager 시크릿 먼저 전부 pull**(메타 액세스토큰 1h 만료 + 복구가 길어서 느린 단계 전에 확보).
3. `git clone` → `git checkout $GIT_REF`(핀 SHA) → 짧은 HEAD 로깅.
4. 시크릿을 파일로 기록: `.env.production`, `deploy/backups/pgbackrest.conf`(**chmod 0644** — 컨테이너 postgres uid 70 이 읽어야 함; umask 077 이면 복구 실패), `certs/origin.pem`+`origin.key`. 그다음 `.env.production` 에 **SITE_ID=gcp-drill 강제**(드릴) + `dr-overlay.env` 병합.
5. `docker compose ... build`(app+db 이미지; cold build ~8분, playwright 포함 — **RTO 지배 요인**. 이미지 프리베이킹하면 단축).
6. **PITR 복구** `bash restore_from_r2.sh "$PITR_TARGET" 1`(DRILL=1):
   - 드릴/무타깃: `--type=immediate --target-action=promote --delta --archive-mode=off`(마지막 백업 consistency 로 즉시 promote, RPO=마지막 백업, **빠름**).
   - PITR 타깃: `--type=time --target=… --target-action=promote`.
   - 실-latest(무타깃): 순수 `--delta`(모든 아카이브 WAL 재생, **느림**).
   - `pg_is_in_recovery()=f` 까지 ~15분 대기, **강제 promote 절대 안 함**(미재생 WAL 유실 방지).
   - 드릴 fail-closed 가드: `archive_mode != off` 면 abort(공유 스탠자 오염 위험), 잔존 `archive_command` 리셋.
7. gated migrate + collectstatic 를 **db:5432 직결**(`-e DB_HOST=db -e DB_PORT=5432 -e DB_CONN_MAX_AGE=0`, `--no-deps`)로 실행(pgbouncer 우회).
8. 스택 기동: `up -d pgbouncer` 후 (드릴) `web_webhook`+`web_dashboard`+`web_external` **만**. `celery_dm/celery_followup/celery_default/celery_billing` **미기동** → dm_send/verify 큐 소비자 0(실 발송 구조적 차단). celery-beat 은 완전 은퇴(스케줄=CF tick + `core.ScheduledJob` 행).
9. `dr_catchup --skip-poll --dry-run`(드릴): 7단계 프린트만(STEP0 rehydrate rate_governor / STEP1 reconcile_stuck_submitting / STEP2 reconcile_accepted_dms / STEP3 revive_failed_token_logs / STEP4 requeue_deferred_dms / STEP5 enforce_campaign_schedules / STEP6 poll_missed_comments[skip]) — `send_dm_task.delay` 호출 안 함.
10. `mark_restore_complete --promote --note 'gcp drill <ts>'`(**db:5432 직결** — 방금 재시작한 db 에 pgbouncer 로 붙으면 'server terminated abnormally'): `active_site=gcp-drill, mode=live, restore_complete=True, epoch++`, site_state 캐시 무효화.
11. 드릴은 **실 전용 단계 스킵**: `resubscribe_webhooks`(실 Meta 접촉) + 새 타임라인 `pgbackrest --type=full backup`/`check`(공유 스탠자 쓰기).
12. Caddy 컨테이너를 `turnflow_instagram_net` 네트워크에서 `Caddyfile.gcp` + 명시 `tls /certs/origin.pem /certs/origin.key`(ACME 없음 — CF proxied + 80포트 잠금)로 기동 → `caddy validate`.
13. 셀프체크 `curl -sk --resolve turnflow-api.clfy.ai.kr:443:127.0.0.1 …/healthz/ready` + Telegram `🧪 DR 드릴 … ready=200`(best-effort). **여기서 정지 — 드릴은 DNS 스왑 없음.**

### Phase 3 — 드릴 검증 (ready=200 알림 / 로그 'startup 완료' 시)
```bash
Z="--zone asia-northeast3-a"
# 1) Caddy/풀체인 ready
gcloud compute ssh turnflow-dr $Z -- \
 'curl -sk --resolve turnflow-api.clfy.ai.kr:443:127.0.0.1 -o /dev/null -w "caddy_ready=%{http_code}\n" https://turnflow-api.clfy.ai.kr/api/v1/healthz/ready'
# → 200 = restore+migrate+dr_catchup+promote+Caddy/Origin-cert 전부 OK

# 2) SiteControl 상태
gcloud compute ssh turnflow-dr $Z -- \
 'cd /opt/turnflow_backend && docker compose -f docker-compose.prod.yml exec -T web_dashboard \
  python manage.py shell -c "from apps.core.site_control import get_site_state as g; print(g())"'
# → active_site=gcp-drill, mode=live, restore_complete=True
```
- **RTO 기록**: provision 시작 → ready 타임스탬프(~8분+, cold docker build 지배).
- **RPO 기록**: 복구 타깃 vs 사고 시각.
- **프로덕션 무영향 확인**: 콜로 `/healthz/ready` 여전히 200(active_site=colo), CF DNS 미변경, R2 `turnflow` 스탠자 타임라인 불변, DM `failed_count=0`.
- **드릴 DM 안전 증거**: celery_dm/followup 미기동(소비자 0) + dr_catchup `--dry-run`(send_dm_task.delay 미호출) → 실 DM/공개답글 0건. 복구 가드 로그 `drill guard OK: archive_mode=off + archive_command reset`.

### Phase 3b — (실 컷오버 전용; 2026-07-01 검증됨, 순수 드릴엔 없음)
```bash
bash provision.sh live ["YYYY-MM-DD HH:MM:SS+09"]   # startup 이 추가로 resubscribe_webhooks + 새 타임라인 full backup/check
# 🟢 ready=200 Telegram 후:
bash cf_origin.sh <GCP_IP>    # 게이트: 'colo fence complete?' 확인(콜로 스택 down + 443 차단) → turnflow-api A레코드 스왑(proxied)
                              # → 스왑 후 colo:443 능동 프로브로 split-brain 경고
```
- **컷오버 캠페인 실증(2026-07-01 실행)**: **다른 IG 계정**으로(소유자 self-comment 는 웹훅 안 옴 + 코드가 스킵) 캠페인 계정 게시물에 댓글 → `web_webhook` 로그에 `facebookexternalua` POST 확인 → GCP 박스가 comment→DM 처리하는지 확인. 웹훅 무음이면 `subscribed_apps` 점검(`resubscribe_webhooks`).
- **Failback(gcp→colo) 은 수동 전용**: `failback_from_gcp.sh` — colo 펜스 → **GCP가 쓴 새 R2 타임라인**에서 colo DB 재시드 → colo `mark_restore_complete --promote` → `cf_origin.sh $COLO_IP` → gcp `--demote` + teardown.

### Phase 4 — 과금 $0 로 teardown
```bash
bash teardown.sh    # yes/no 프롬프트. VM turnflow-dr 삭제 + 정적 IP turnflow-dr-ip 해제. 방화벽/SA/시크릿은 유지.
gcloud compute instances list      # turnflow-dr 없음 확인
gcloud compute addresses list      # turnflow-dr-ip 없음 확인 (R2 egress 는 무료)
```
> ⚠️ **실 컷오버 중에는 절대 teardown 금지** — 정적 IP 해제 = DNS 타깃 소멸.

---

## 3. 검증 체크리스트 (요약)

- [ ] VM 위 `--resolve turnflow-api.clfy.ai.kr:443:127.0.0.1 …/healthz/ready` = **200**
- [ ] `get_site_state()` → `active_site=gcp-drill, mode=live, restore_complete=True`
- [ ] `dr_catchup` 로그에 STEP0~STEP6 전부(드릴은 `[dry-run]`). STEP0 rehydrate + STEP3 revive 가 DM 정합 핵심
- [ ] 프로덕션 격리: 콜로 200(active_site=colo) / CF DNS 불변 / R2 스탠자 타임라인 불변 / `failed_count=0`
- [ ] 드릴 DM 안전: celery_dm/followup 미기동 + `--dry-run` → 실 DM 0
- [ ] 복구 가드 로그: `drill guard OK: archive_mode=off …`
- [ ] (실) proxied ready=200(`ssl_verify=0`) + 비소유자 계정 comment→DM 1건 처리
- [ ] RTO / RPO 기록

---

## 4. 함정(Gotchas) — 실제 관측된 것만

1. **인증서 호스트네임 (2026-06-30 실 컷오버 실패 근본원인)** — `api.turnflow.clfy.ai.kr` 는 **2단계** 서브도메인 → CF Universal SSL(`*.clfy.ai.kr`) 미커버 → proxied 클라이언트 TLS **000**. 수정: API 호스트를 **1단계** `turnflow-api.clfy.ai.kr` 로 이전(와일드카드가 즉시 커버, proxied ssl_verify=0). 모든 DR 스크립트/`Caddyfile.gcp` 가 turnflow-api 사용.
2. **와일드카드 Origin 인증서 누출 (2026-07-01 실사고)** — `tls .../origin.pem` 이 `*.clfy.ai.kr` 와일드카드면 Caddy 가 **다른 1단계 사이트**(`llm.clfy.ai.kr`, `monitor.clfy.ai.kr`)에도 이 인증서를 서빙(저장된 LE 인증서 있어도 와일드카드 우선). CF Origin cert 는 공개 신뢰 안 됨 → 앱의 서버간 `https://llm.clfy.ai.kr` 호출이 TLS 실패(openai APIConnectionError) → **AI DM 초안이 조용히 실패**. 수정: `LLM_URL=http://litellm-proxy:4000`(내부, 동일 네트워크). 완전 수정(보류)은 turnflow-api 전용 Origin 인증서.
3. **IMMEDIATE vs LATEST 복구** — 드릴/무타깃은 **반드시** `--type=immediate --target-action=promote`(빠름, RPO=마지막 백업). 순수 `--delta`(default 'latest')는 마지막 백업 이후 모든 아카이브 WAL 재생 → 콜로의 분당 아카이빙이면 수백 세그먼트 = 매우 느림(첫 드릴이 멈춘 듯 보인 원인). `--target-action` 은 `--type` 설정 시에만 유효(pgBackRest [031] 거부). PITR 는 `--type=time`.
4. **db 재시작 후 pgbouncer stale** — `mark_restore_complete`/`migrate` 는 **db:5432 직결**(`-e DB_HOST=db -e DB_PORT=5432 -e DB_CONN_MAX_AGE=0`). 재초기화 직후 pgbouncer 경유하면 'server terminated abnormally'. startup.sh 는 이미 직결.
5. **pg_promote 강제 금지** — `restore_from_r2.sh` 는 `pg_is_in_recovery()=f` 까지 ~15분 대기 후 여전히 복구중이면 db 로그 출력하고 운영자 대기. 자동 강제 promote 는 미재생 WAL 유실.
6. **ScheduledJob 행 vs 휴면 CELERY_BEAT_SCHEDULE** — DR Step2 이후 celery-beat 은퇴. 스케줄=CF tick + `core.ScheduledJob` DB 행(next_due_at). **`settings.CELERY_BEAT_SCHEDULE` 는 tick 이 안 읽음** — 새 주기잡은 ScheduledJob 시드 마이그레이션 필요(`core 0002_seed_dr_state`, `0003_seed_resubscribe_webhooks_job`). beat 항목만 추가하면 절대 안 돎.
7. **웹훅 재구독 (실 전용)** — Meta 는 콜백 반복 실패(엣지 장애/컷오버) 시 계정별 comments/messages 구독을 auto-disable → 서버 정상이어도 **댓글 웹훅 무음 → 캠페인 정지**. startup.sh(live)가 promote 직후 `resubscribe_webhooks` 실행. 드릴은 스킵.
8. **self-comment 함정 (컷오버 테스트)** — 게시물 **소유 계정** 댓글은 comments 웹훅 안 옴 + 코드가 스킵('Skipping self-comment DM'). comment→DM 테스트는 **반드시 다른 IG 계정**으로.
9. **MODE 메타 가드** — startup.sh 는 `MODE` 미설정 시 abort(드릴이 실로 오인돼 실 DM 발송·공유 스탠자 오염 방지). SITE_ID=gcp-drill + archive-mode=off + fail-closed 가드(archive_mode!=off 면 abort)가 공유 `turnflow` 스탠자를 청정 유지.
10. **e2-standard-16 필수** — `docker-compose.prod.yml` 이 db `cpus:16` 핀 → e2-standard-8 은 'range of CPUs is from 0.01 to 8' 에러. 더 작게 가려면 compose 의 db `shared_buffers`/`effective_cache_size` + gunicorn/celery 동시성도 낮춰야 함(`dr-overlay.env` 는 env-read 키만 바꾸지 compose 하드코드 db 플래그는 못 바꿈).
11. **False-failover / split-brain (실)** — 콜로가 실제 살아있는데 스왑하면 colo+GCP 가 같은 R2 스탠자에 분기 타임라인 push(백업 손상). `cf_origin.sh` 가 GCP 스왑 시 'colo fence complete?' 게이트 강제 + 스왑 후 colo:443 능동 프로브. **자동 failback 금지**, 콜로 복귀는 항상 수동 + 새 타임라인 재시드(`failback_from_gcp.sh`).
12. **Telegram no-op 함정** — 2026-06-30 이전 콜로 `.env` 의 `TELEGRAM_BOT_TOKEN/CHAT_ID` 가 비어 `send_telegram_notification` 이 조용히 no-op → ready 알림 놓침. 토큰 설정 확인.

---

_최종 검증: 2026-06-30 격리 드릴 + 2026-07-01 실 컷오버(GCP 완전교체 → 실 comment→DM 처리 → 콜로 복구 → teardown $0) 성공._
