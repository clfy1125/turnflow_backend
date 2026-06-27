# TurnFlow DR 구현 계획 (Disaster Recovery)

> **상태:** 설계 확정 / 코드 착수 전
> **작성:** 2026-06-26
> **패턴:** Cloudflare LB + 외부 Scheduler + pgBackRest/WAL → R2 + 사내 Warm-Standby (+ Azure Cold)
> **목표:** 콜로케이션 단일 물리서버를 primary로 유지하되, 장애 시 사내 대기서버(또는 임시 Azure VM)로 **수 분 내** 복구·전환.
> **성격:** 이것은 **DR(복구 후 전환)** 이지 무중단 HA 가 아니다. RTO ≈ 수 분, RPO ≈ 1–2분(Layer-2 PITR ON 시).

이 문서는 **재개 가능(resumable)** 하게 작성되었다. 새 세션/담당자는 §2(결정 로그) → §3(코드 자산) → §10(작업 리스트) → §11(진행 체크리스트) 순으로 읽으면 현재 상태를 즉시 파악할 수 있다.

---

## 0. 목차
1. 개요 & 목표
2. 확정 결정 로그
3. 현재 코드 자산 (REUSE vs NET-NEW)
4. 아키텍처 — Caddy 3-tier ↔ DR 충돌 해소 + CF LB
5. 영역 A — 헬스 + active_site 게이트
6. 영역 B — 스케줄러 외부화
7. 영역 C — 백업/복구 + catch-up + 거버너 재수화 + Action Block 영속화 (**#2 확정 설계**)
8. 영역 D — CF LB + failover/failback + Azure
9. Cloudflare R2 연동 상세
10. ✅ 전체 작업 리스트업 (코드 PR + 운영/외부 서비스)
11. 진행 체크리스트 (resumable)
12. 테스트 계획
13. 리스크 & split-brain 방지
14. 미해결/오너 결정

---

## 1. 개요 & 목표

```
사용자 → Cloudflare(DNS/WAF/Load Balancer)
   ├─ Pool 1: colo-production   (콜로 물리서버 /api/v1/healthz/ready)   ← 평상시
   └─ Pool 2: office-fallback   (사내 서버 /maintenance-health)        ← 장애 시
콜로 서버: Caddy(3-tier) → Docker Compose(Django web×3tier / Celery / Postgres / Redis)
사내 서버: 평소 maintenance page + dr-agent + restore 대기, R2에서 DB 복구 가능 상태
백업: PostgreSQL → pgBackRest(WAL 연속 아카이브) → Cloudflare R2 (PITR)
스케줄러: 외부 Cron(CF Cron + GCS + Healthchecks) → /scheduler/tick → DB due-job → Celery
```

- **RTO** ≈ CF 감지(~45s) + PITR 복구 + `migrate --check` + Caddy 모드 스왑(~2s) + dr_catchup.
- **RPO** ≈ `archive_timeout=60s` 기준 1–2분. **단 Layer-2 PITR이 prod에 실제 ON일 때만**(§2 D-1 게이트).
- **4대 안전 기둥:** ① active_site 락 + epoch 펜싱 ② 강화된 `/healthz/ready` ③ DB 기반 catch-up(기존 멱등 태스크 재사용) ④ 반자동 failover / **수동 승인** failback.

---

## 2. 확정 결정 로그

| # | 결정 | 내용 | 근거 |
|---|---|---|---|
| **D-1** | **PITR 검증 게이트** | DR의 failover/restore를 **신뢰·테스트하기 전**, prod에서 `archive_mode`/`pg_stat_archiver`/`pgbackrest check`/R2 객체를 검증해야 한다(§9.3). 앱-레이어 DR 코드(영역 A/B)는 동작-중립이라 병행 착수 OK, **복구·failover 테스트는 이 게이트 통과 후**. | RPO가 "분"인지 "24h"인지 결정 |
| **D-2** | **거버너 = DB 재수화 (Option C)** | Redis 손실 시 최대 1h 동결 → **`SentDMLog.submitted_at`에서 계정별 카운터 재구성 후 즉시 재개**. 블랭킷 동결은 fallback으로만 남김. **Action Block 쿨다운은 DB(`DMAccountBlock`)에 영속화**해 함께 복원. (§7) | 카운트의 진실은 DB에 있음. 동결의 유일 이유(카운트 소실) 제거 |
| **D-3** | **스케줄러 60초 tick** | 외부 Cron 60초 케이던스 채택. 30초 잡(`reconcile_stuck_submitting`/`requeue_deferred_dms`)도 60초로 — 멱등이라 무손실, cadence만 거칠어짐. | CF Cron 최소 60초 + 무손실 |
| **D-4** | **passive = fully-dark** | passive 서버는 쓰기+읽기 모두 503. 예외 `/api/v1/healthz*`·`/api/v1/internal/scheduler/*`. (`PASSIVE_ALLOW_READS=False`) | split-brain 최소화 |
| **D-5** | **pgBackRest 유지 (WAL-G ❌)** | 이미 통합(`deploy/backups/pgbackrest.conf.example` "Chosen over wal-g") + R2 암호화 + `backup_health_check` 감시. WAL-G는 중복 + 시크릿 표면 증가. | 코드에 이미 명시 |
| **D-6** | **3-tier 보존** | Caddy 3-tier(부하 격리)는 안 버린다. production 모드로 유지하고 DR(서버 단위 전환 + maintenance 모드)을 그 위에 직교적으로 얹음. (§4) | 다른 레이어의 관심사 |
| **D-7** | **코드 우선, 테스트 후행** | 앱-레이어 DR 코드 먼저 구현 → R2/백업/CF/cron 운영 셋업 → 마지막에 failover/restore 드릴로 검증. | 운영자 지시 |

---

## 3. 현재 코드 자산 (REUSE vs NET-NEW)

### ✅ REUSE — 이미 존재 (새로 안 만듦)
| 자산 | 위치 | 용도(DR) |
|---|---|---|
| pgBackRest WAL PITR → R2 | `deploy/backups/pgbackrest.conf.example`, `postgresql.archive.conf` | Layer-2 PITR 복구원 |
| 일일 논리 덤프 → R2 | `deploy/backups/pg_backup.sh` | Layer-1 백업 |
| `backup_health_check` (pg_stat_archiver 감시) | `apps/core/tasks.py` | 아카이브 lag/실패 Telegram |
| catch-up 태스크 (멱등·DB구동) | `apps/integrations/tasks.py`: `reconcile_accepted_dms`, `reconcile_stuck_submitting`, `requeue_deferred_dms`, `dead_letter_alerter`, `dm_backlog_alert`, `enforce_campaign_schedules`, `revive_failed_token_logs` | 복구 후 Redis 큐 DB 재구성 |
| `poll_missed_comments` | `apps/integrations/tasks.py` (`integrations.poll_missed_comments`) | 누락 댓글 보정 |
| Telegram 알림 | `apps/core/telegram.py: send_telegram_notification` | 장애/복구 알림 |
| 503 킬스위치 미들웨어 패턴 | `apps/insights/middleware.py: InsightsDisabledMiddleware` | active_site 게이트 템플릿 |
| `SentDMLog` (submitted_at 등 영구 발송 원장) | `apps/integrations/models.py:890` | 거버너 카운터 재수화원 |
| `select_for_update` 패턴 | `apps/billing/`, `apps/integrations/tasks.py` | tick due-job 락 |
| 미디어 R2 오프사이트 | `USE_R2=True` (settings) | **DB-only 복구** 가능 |
| migrate 게이트(직결) | `deploy/scripts/deploy.sh` | 복구 후 migrate --check |

### 🆕 NET-NEW — 신규 코드 (PR)
| 자산 | 위치 |
|---|---|
| `SiteControl` 싱글톤 모델 (+migration, seed) | `apps/core/models.py` (현재 모델 없음) |
| `ScheduledJob` 모델 (+migration, seed 14잡) | `apps/core/models.py` |
| `DMAccountBlock` 모델 (Action Block 영속화) | `apps/integrations/models.py` |
| `/healthz/live` + `/healthz/ready` | `apps/core/views.py` (+`apps/core/site_control.py` 헬퍼) |
| `ActiveSiteGateMiddleware` | `apps/core/middleware.py` (+ base.py 등록) |
| Celery `task_prerun` 게이트 + epoch 펜스 | `config/celery.py` 또는 `apps/core/celery_gate.py` |
| `/api/v1/internal/scheduler/tick` | `apps/core/views_internal.py` (+`apps/core/internal_auth.py`) |
| 거버너 `rehydrate_from_db()` + DMAccountBlock 듀얼라이트 | `apps/integrations/rate_governor.py` |
| `dr_catchup`, `mark_restore_complete` 관리 명령 | `apps/core/management/commands/` |
| maintenance Caddyfile + maintenance.html | `deploy/caddy/Caddyfile.maintenance`, `deploy/caddy/maintenance.html` |
| failover/failback/restore 스크립트 | `deploy/dr/failover.sh`, `failback.sh`, `deploy/backups/restore_to_office.sh`, `restore_pitr_drill.sh` |

### 🆕 NET-NEW — 운영/인프라 (PR 아님)
Cloudflare Load Balancer / 외부 스케줄러(CF Cron + GCS + Healthchecks.io) / R2 버킷·토큰·lifecycle / 사내 웜스탠바이 박스 / Azure 콜드 VM 아티팩트 / SERVER_RUNBOOK DR 섹션.

---

## 4. 아키텍처 — Caddy 3-tier ↔ DR 충돌 해소 + CF LB

### 4.1 직교 원리
- **3-tier 경로 라우팅** = 한 오리진 내부의 PATH별 부하 격리.
- **DR failover** = 오리진 단위(콜로 vs 사무실) + 서버별 MODE(앱 vs 점검페이지).
- → production 모드(3-tier)는 **활성 오리진인 양쪽 박스에서** 돈다. maintenance 모드는 **passive 스탠바이에서만** 쓰는 일시 상태.

### 4.2 서버당 Caddy 설정 2개
| 모드 | 파일 | 동작 |
|---|---|---|
| **production** | `deploy/caddy/Caddyfile` (기존 3-tier + 하드닝) | `@webhook→web_webhook`, `@external→web_external`, `@auth/@media/default→web_dashboard`. `/healthz*`는 default→web_dashboard로 결정적. `/maintenance-health→200` 추가(스왑 단순화). |
| **maintenance** | `deploy/caddy/Caddyfile.maintenance` (신규, 작음) | `/maintenance-health→200`(항상), `/api/v1/healthz/ready→503`, 그 외 → 정적 `maintenance.html`(503) |

**production Caddyfile에 추가할 명시 matcher (`@webhook` 앞):**
```caddyfile
@health path /api/v1/healthz /api/v1/healthz/live /api/v1/healthz/ready
handle @health { reverse_proxy web_dashboard:8000 }

@maint_health path /maintenance-health
handle @maint_health { respond "OK" 200 }
```

### 4.3 CF LB 모니터 2개 (핵심)
| 풀 | 오리진 | 모니터 경로 | 기대 |
|---|---|---|---|
| `colo-production` | 콜로 | `/api/v1/healthz/ready` (deep) | 200 |
| `office-fallback` | 사무실 | `/maintenance-health` (passive 시 항상 200) | 200 |

- 사무실 풀을 `/healthz/ready`로 모니터하면 passive 동안 503 → CF가 사무실을 DOWN 처리 → **점검페이지 대신 CF 에러**. 그래서 passive는 `/maintenance-health`로 모니터.
- production Caddyfile에도 `/maintenance-health→200`을 두면 maintenance→production 스왑 후에도 사무실 풀 모니터를 **안 바꿔도** 됨(장애 중 CF mutation 불필요).
- 옵션: `ready()`가 `READY_PROBE_SIBLINGS=True`일 때 `web_webhook:8000/healthz/live` + `web_external:8000/healthz/live`를 fan-out 프로빙 → 한 tier만 죽어도 서버 전체 unhealthy → CF가 서버 단위로 전환(올바른 DR 단위).

### 4.4 reload 기반 모드 스왑 (failover.sh 마지막, ~2초 무중단)
```bash
cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile      # maintenance → production(3-tier)
docker exec -w /etc/caddy caddy caddy validate --config /etc/caddy/Caddyfile
docker exec -w /etc/caddy caddy caddy reload   --config /etc/caddy/Caddyfile
```
즉시 revert: `cp Caddyfile.maintenance ...` 후 validate+reload. `[LEGACY]` 단일 업스트림 블록은 DR과 무관하게 롤백용 유지.

---

## 5. 영역 A — 헬스 + active_site 게이트 (split-brain 토대, 모든 것의 선행)

### 5.1 `SiteControl` 싱글톤 (NET-NEW) — `apps/core/models.py`
```python
class SiteControl(models.Model):
    # 어느 서버가 '권위(write/celery/scheduler 가능)'인가의 유일한 진실 (DB 단일 행)
    active_site = models.CharField(max_length=32)            # 'colo' | 'office' | 'azure'
    epoch = models.BigIntegerField(default=1)               # 펜싱 토큰: 전환마다 +1
    mode = models.CharField(max_length=16, default='live')  # 'live' | 'maintenance'
    restore_complete = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        db_table = 'site_control'
    def save(self, *a, **k):
        self.pk = 1; super().save(*a, **k)                  # 싱글톤 강제
    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults={...})
        return obj
```
- `SITE_ID = config('SITE_ID', default='colo')` (base.py). 콜로=colo, 사무실=office, Azure=azure.
- 헬퍼 `apps/core/site_control.py`: `is_active_site()` = `settings.SITE_ID == SiteControl.load().active_site`. **5초 캐시(redis /1)하되 캐시 미스 시 DB fallback** (Redis 손실이 게이트를 깨면 안 됨).
- **epoch = 펜싱**: 모든 전환에서 +1. 되살아난 stale 콜로(epoch N)가 `(office, N+1)`을 보면 self-passivate.

### 5.2 헬스 split (NET-NEW) — `apps/core/views.py`
- `live(request)`: 의존성 0, `{status:'live', site:SITE_ID}` 200. **Docker tier 헬스체크를 이걸로** 전환(passive 컨테이너가 ready=503로 재시작 루프 도는 것 방지).
- `ready(request)`: 아래 전부 통과 → 200, 아니면 통일 503 `{success:false,error:{code:503,details:{failed_check:...}}}`:
  1. DB `SELECT 1` (기존 `healthz` 재사용)
  2. Redis 왕복 `cache.set/get('healthz:ping')`
  3. 마이그레이션 최신 `MigrationExecutor(connection).migration_plan(targets)` 비어 있음
  4. `active_site == SITE_ID` AND `mode == 'live'`
  5. `restore_complete is True`
  6. (선택) 스케줄러 heartbeat 신선도 — `READY_REQUIRE_SCHEDULER_HEARTBEAT` 기본 **False**(초기 alert-only, 자기유발 failover 방지)
- 기존 `healthz`는 `ready` alias로 back-compat. 라우팅: `config/api_urls.py`에 `healthz/live`, `healthz/ready` 추가.

### 5.3 `ActiveSiteGateMiddleware` (NET-NEW) — `apps/core/middleware.py`
- 등록: base.py MIDDLEWARE에서 `LoggingMiddleware` **직후**, `InsightsDisabledMiddleware` 앞.
- 동작: `is_active_site() and mode=='live'` → 통과. 아니면 **모든 쓰기(POST/PUT/PATCH/DELETE) 503** (`reason: not_active_site`). 안전 메서드는 `PASSIVE_ALLOW_READS`(기본 **False** = fully-dark)일 때만 통과.
- **하드 예외(막으면 failover 깨짐):** `/api/v1/healthz*`, `/api/v1/internal/scheduler/*`. 테스트로 우회 보장.
- 3 tier가 같은 이미지/settings 공유 → 한 번 등록으로 webhook/dashboard/external 전부 게이트(서버 단위 전환과 일치).

### 5.4 Celery task 게이트 (NET-NEW) — `config/celery.py` `task_prerun`
- 외부효과 태스크(`send_dm_task` [tasks.py:749], `process_messaging_event`, `billing.*`)는 passive에서 `Reject(requeue=False)`.
- 멱등 리컨실러(`reconcile_*`, `requeue_deferred_dms`, `enforce_campaign_schedules`, `poll_missed_comments`, `revive_failed_token_logs`)는 active에서 RUN 허용 — 복구 후 DB→Redis 재구성 엔진.
- **epoch 펜스:** prerun에서 현재 epoch 읽어 stale 메시지 drop(flip 후 실행 방지).

### 5.5 restore_complete 와이어링
- 스탠바이는 `restore_complete=False`로 부팅 → ready=503 유지(CF LB drain 지속).
- `mark_restore_complete.py --promote`: 한 트랜잭션 `SELECT FOR UPDATE` → `epoch+=1`, `active_site=SITE_ID`, `mode='live'`, `restore_complete=True`. **이 원자적 flip이 ready를 녹색으로.** `--demote`도 제공.

---

## 6. 영역 B — 스케줄러 외부화 (celery_beat SPOF 제거)

### 6.1 `ScheduledJob` (NET-NEW) — `apps/core/models.py`
- 행당 현 `CELERY_BEAT_SCHEDULE` 1개: `key`(unique), `task`, `interval_seconds`(nullable) 또는 `cron_minute/hour/...`, `queue`(nullable=route-by-name), `enabled`, `last_run_at`, `next_due_at`(권위 필드), `last_status`, `last_error`. index `(enabled, next_due_at)`.
- django-celery-beat DatabaseScheduler ❌ (미설치 + 장기실행 beat = SPOF 재도입 + stateless cron 구동 불가).
- 데이터 마이그레이션 `0002_seed_scheduled_jobs.py`: base.py CELERY_BEAT_SCHEDULE 14잡 정확 시드. **#3 결정: 30초 잡 2개도 60초**.

### 6.2 `POST /api/v1/internal/scheduler/tick` (NET-NEW) — `apps/core/views_internal.py`
계약:
1. **인증**: `X-Scheduler-Secret == settings.SCHEDULER_TICK_SECRET`(상수시간) + `SCHEDULER_TICK_ALLOWED_IPS` IP allowlist → 401/403.
2. **active_site 게이트**: `SITE_ID != active_site` → **409**(passive), active_site 불명 → **503**. 아무것도 fire 안 함.
3. **due 평가(DB 락)**: `transaction.atomic()` + `ScheduledJob.objects.select_for_update(skip_locked=True).filter(enabled=True, next_due_at__lte=now)` → `next_due_at` 전진 → `app.send_task(row.task, queue=row.queue or None)`로 **enqueue만**(인라인 실행 ❌).
4. Healthchecks.io ping(best-effort, 짧은 타임아웃).
- 응답 200 `{fired:[keys], skipped:int, now:iso}`. `@extend_schema`로 200/401/403/409/503 문서화(CLAUDE.md §7).
- **single-fire 불변식**: 동시 tick(CF + GCS)이 와도 첫 트랜잭션이 `next_due_at` 전진 → 두 번째는 due 없음. "윈도우당 1회"가 caller 수 무관 DB 불변식.

### 6.3 celery_beat = DISABLED fallback
`docker-compose.prod.yml:292` 정의는 보존, tick-authoritative 프로필에선 미기동. **상호배타**: tick 모드 = beat 정지 / 롤백 = beat 시작 + cron 일시정지. (co-run 금지 — sub-minute 잡 이중 enqueue. 단 전부 멱등이라 전환 중 짧은 중복은 무손실.)

### 6.4 dead-man 모니터링
성공 tick(active)마다 Healthchecks.io ping(가장 촘촘한 잡 기준 grace). cron 전부 죽거나 앱 다운 시 Healthchecks가 알림. overdue 잡 fire 시 "스케줄러 stall 후 부활" Telegram. `HEALTHCHECKS_TICK_URL` 신규.

---

## 7. 영역 C — 백업/복구 + catch-up + 거버너 재수화 + Action Block 영속화 (**#2 확정 설계**)

### 7.1 거버너 DB 재수화 (D-2)
**근거(코드 검증):** 거버너는 `ig_account_id=str(ig_conn.external_account_id)` 키([tasks.py:150]) `check()`는 `mark_submitting()` 직전 증가. `SentDMLog.submitted_at`("API 호출 시각" [models.py:1063])이 모든 실발송의 영구 기록. 경로 `SentDMLog→campaign→ig_connection.external_account_id`. 윈도우 일치(UTC `now//3600`/`now//60`). → **잃은 카운트를 DB에서 정확히 복원 가능.**

**신규 `rehydrate_from_db()` — `apps/integrations/rate_governor.py`:**
```python
def rehydrate_from_db():
    """Redis 손실/failover 후 계정별 거버너 카운터를 SentDMLog에서 재구성하고 즉시 재개시킨다."""
    from django.db.models import Count
    from django.utils import timezone
    from apps.integrations.models import SentDMLog
    now = int(time.time())
    hour_epoch, min_epoch = now // 3600, now // 60
    hour_start = timezone.datetime.fromtimestamp((now//3600)*3600, tz=timezone.utc)
    min_start  = timezone.datetime.fromtimestamp((now//60)*60,   tz=timezone.utc)
    # 시각 윈도우 계정별 발송 수 (submitted_at 이 윈도우 안 = 실제 Meta 호출 소비)
    for r in (SentDMLog.objects.filter(submitted_at__gte=hour_start)
              .values('campaign__ig_connection__external_account_id')
              .annotate(n=Count('id'))):
        acct = r['campaign__ig_connection__external_account_id']
        if acct: cache.set(f"dmrate:h:{acct}:{hour_epoch}", r['n'], timeout=3700)
    for r in (SentDMLog.objects.filter(submitted_at__gte=min_start)
              .values('campaign__ig_connection__external_account_id')
              .annotate(n=Count('id'))):
        acct = r['campaign__ig_connection__external_account_id']
        if acct: cache.set(f"dmrate:m:{acct}:{min_epoch}", r['n'], timeout=70)
    _rehydrate_action_blocks()                 # ↓ 7.2
    cache.set("dmrate:alive", 1, timeout=7*24*3600)
    cache.delete("dmrate:reset_until")          # 동결 해제
```
- **호출 시점:** ① `dr_catchup` STEP 0, ② Celery `worker_ready` 시그널(예기치 않은 Redis 재시작도 자동 복원). → `check()`는 정확한 카운트를 봐 **동결 0초**.
- **기존 동결은 fallback 유지:** 재수화가 안 돈 비상시(rehydrate 미실행) `check()`의 `redis_reset_failclosed`가 여전히 ban-safe 안전망.
- **단서:** DR failover(사무실)는 DB가 RPO(~1–2분) stale → 최근 1–2분 발송이 빠져 약간 적게 셈 → 약간 더 허용. 거버너의 700 캡(Meta 실제 750, 마진 50) + 분당 캡이 흡수. (동일서버 Redis 재시작은 DB 최신 → 100% 정확.)

### 7.2 Action Block 영속화 (#2 함께 설계) — `DMAccountBlock` (NET-NEW)
**문제:** `dm:ab:cooldown:*`/`dm:ab:level:*`가 Redis 전용 → 소실 시 차단됐던(Meta 368) 계정이 재개 → **차단 연장**. 카운터 재수화로는 복원 안 됨.

**설계: DB를 진실로, Redis는 fast-path 캐시.** 신규 모델 `apps/integrations/models.py`:
```python
class DMAccountBlock(models.Model):
    external_account_id = models.CharField(max_length=255, unique=True, db_index=True)
    cooldown_until = models.DateTimeField(null=True, blank=True)  # 이 시각까지 발송 차단
    level = models.IntegerField(default=0)                        # 에스컬레이션 횟수
    last_tripped_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        db_table = "dm_account_block"
```
`rate_governor.py` 변경(작음, 핫패스 영향 최소):
- `trip_action_block(acct)`: 기존 cache.set + **`DMAccountBlock` upsert**(cooldown_until, level, last_tripped_at) 추가.
- `action_block_cooldown_remaining(acct)`: cache.get; **miss 시 `DMAccountBlock` 조회 → 캐시 재프라임**(이후 호출은 캐시 히트, 정상 상태 per-send DB 히트 없음).
- `_rehydrate_action_blocks()`: `DMAccountBlock.filter(cooldown_until__gt=now)` → `dm:ab:cooldown/level` 캐시 재시드.
- **결과:** Action Block이 DM 원장만큼 내구. Redis 소실/failover 후에도 차단 계정이 정확히 차단 유지. (level 에스컬레이션 이력도 DB 보존.)

### 7.3 Layer-2 PITR 검증/활성화 (D-1) — §9.3 참조. RPO를 24h→60s로 떨어뜨리는 전제.

### 7.4 사내 웜스탠바이 복구 (NET-NEW) — `deploy/backups/restore_to_office.sh`
콜드박스용 `restore_to_new_box.sh`(docker 데몬 정지 + DATA_DIR 삭제)는 웜스탠바이엔 부적합 → **비파괴 변형**:
1. `stop db pgbouncer web_* celery_*` (redis는 유지, 어차피 fresh).
2. `pgbackrest --stanza=turnflow --type=time --target='<ts>' --delta restore --target-action=promote`.
3. `up -d db` + `pg_isready`/consistency 대기.
4. **migrate 체크 — pgbouncer 우회, db:5432 직결**(deploy.sh와 동일: `-e DB_HOST=db -e DB_PORT=5432 ... migrate --check`, skew면 `migrate --noinput`).
5. `up -d pgbouncer` → web tier. (transaction pool/CONN_MAX_AGE=0/DISABLE_SERVER_SIDE_CURSORS 그대로.)
6. restore start/end + PITR target-vs-now = RTO/RPO 기록.

### 7.5 `dr_catchup` 오케스트레이터 (NET-NEW) — `apps/core/management/commands/dr_catchup.py`
기존 멱등 태스크를 **순서대로 동기 호출**(신규 dedupe 없음):
- **STEP 0** `rate_governor.rehydrate_from_db()` (카운터 + Action Block 재수화)
- **STEP 1** `reconcile_stuck_submitting` (in-flight SUBMITTING 먼저, Conversations-API dedupe)
- **STEP 2** `reconcile_accepted_dms`
- **STEP 3** ACTIVE 연동별 `revive_failed_token_logs` (in-place revive, idempotency_key 재사용 안전)
- **STEP 4** `requeue_deferred_dms` (select_for_update skip_locked)
- **STEP 5** `enforce_campaign_schedules`
- **STEP 6** `poll_missed_comments` (마지막, idempotency_key + SeenComment ledger dedupe)
- 플래그: `--dry-run`, `--skip-poll`(RTO-critical 1차 패스 기본 skip), `--conn-batch N`. 종료 시 Telegram 요약.

### 7.6 월간 복구 드릴 — `deploy/backups/restore_pitr_drill.sh`
throwaway 컨테이너에 `--type=time --target=<5분전>` 복구 → sanity row counts + `migrate --check` → RPO=(now − 복구된 마지막 WAL 시각), RTO=스톱워치 → Telegram. cron `0 5 1 * *`.

---

## 8. 영역 D — CF LB + failover/failback + Azure

### 8.1 Cloudflare Load Balancer (운영)
`api.turnflow.clfy.ai.kr` → CF LB. 풀 2개(§4.3), failover 순서 colo→office, **session affinity OFF**(stateless+JWT), steering=failover, **proxied ON**(WAF 유지), 모니터 interval 15s/timeout 5s/consecutive_down 3/up 2(~45s 감지), 알림→Telegram. Terraform `cloudflare_load_balancer(_pool|_monitor)`.

### 8.2 failover.sh (반자동) — `deploy/dr/failover.sh`
PITR restore → compose up → gated migrations → dr_catchup(STEP0 재수화 포함) → `mark_restore_complete --promote`(active_site=office, epoch++) → Caddy maintenance→production reload.

### 8.3 failback.sh (**수동 승인 only**) — `deploy/dr/failback.sh`
콜로 write 펜스(stop colo stack/ufw) → 콜로 DB를 **사무실 백업에서 재시드** → 검증 → `FAILBACK_READY` → 사람 승인 → active_site=colo(epoch++) → CF LB 콜로 복귀 → 사무실 standby 전환. **자동 failback 절대 없음**(콜로 DB가 과거일 수 있음). pgBackRest promote는 새 타임라인 생성 → 콜로 demote/wipe 전 `archive_mode=on` 재기동 금지(타임라인 split 방지).

### 8.4 Azure 콜드 VM (운영 아티팩트) — `deploy/dr/azure/`
Terraform `azurerm_linux_virtual_machine` + NSG(443 CF only), cloud-init(docker/pgbackrest/gpg/awscli + 핀고정 태그 clone), `restore_from_r2.sh`, 시크릿스토어에서 `.env.production`/pgbackrest.conf pull, `start_stack.sh`, CF 풀/오리진 업데이트. **콜로+사무실 둘 다 잃을 때만.**

---

## 9. Cloudflare R2 연동 상세

R2는 DR에서 **세 가지**로 쓰인다: ① 미디어(이미 USE_R2), ② **pgBackRest 백업 repo(DR 핵심)**, ③ Azure/사무실 복구 시 백업 다운로드 소스.

### 9.1 R2 버킷 & 토큰 (Cloudflare 대시보드)
1. R2 → **버킷 생성**: 예 `turnflow-db-backup` (미디어 버킷과 **분리** — 권한·lifecycle 독립).
2. **R2 API 토큰 발급**: Object Read & Write, **해당 버킷에만** 스코프(최소권한). → `Access Key ID` / `Secret Access Key` / `Account ID` 확보.
3. S3 엔드포인트: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`, region = `auto`.
4. (선택) 별도 토큰: 사무실/Azure 복구 박스는 **읽기 전용** 토큰으로 분리.

### 9.2 pgBackRest → R2 설정 (`/etc/pgbackrest/pgbackrest.conf`)
```ini
[global]
repo1-type=s3
repo1-s3-endpoint=<ACCOUNT_ID>.r2.cloudflarestorage.com
repo1-s3-region=auto
repo1-s3-bucket=turnflow-db-backup
repo1-s3-uri-style=path                 # R2는 path-style 필수
repo1-s3-key=<R2_ACCESS_KEY_ID>
repo1-s3-key-secret=<R2_SECRET_ACCESS_KEY>
repo1-path=/pgbackrest
repo1-cipher-type=aes-256-cbc           # 클라이언트사이드 암호화
repo1-cipher-pass=<PGBACKREST_CIPHER_PASS>   # ★ 서버 밖 보관!
repo1-retention-full=4                  # 30일 정책에 맞춰 조정(+R2 lifecycle 병행)
start-fast=y
[turnflow]
pg1-path=/var/lib/postgresql/data
```
`postgresql.archive.conf` (db에 append, 1회 재시작):
```
archive_mode = on
archive_command = 'pgbackrest --stanza=turnflow archive-push %p'
archive_timeout = 60
wal_level = replica
max_wal_senders = 3
```
활성화: `pgbackrest --stanza=turnflow stanza-create` → db 재시작 → `pgbackrest --stanza=turnflow --type=full backup` → `pgbackrest --stanza=turnflow check`.

### 9.3 ★ 착수 전 검증 (D-1 게이트) — 콜로 prod에서
```sql
-- psql: WAL 아카이빙 ON?
SHOW archive_mode;     -- 'on'
SHOW archive_command;  -- 'pgbackrest archive-push ...'
SHOW archive_timeout;  -- '60s'
SELECT last_archived_wal, last_archived_time, failed_count, last_failed_time,
       now() - last_archived_time AS since_last_archive
FROM pg_stat_archiver;  -- since_last_archive < ~2분, failed_count 정지
```
```bash
pgbackrest --stanza=turnflow info     # 최근 full/diff + WAL min~max
pgbackrest --stanza=turnflow check    # 통과해야
# R2 객체 적재 확인 (rclone/aws-cli, R2 엔드포인트)
aws s3 ls s3://turnflow-db-backup/pgbackrest/archive/turnflow/ --endpoint-url https://<ACCT>.r2.cloudflarestorage.com | tail
aws s3 ls s3://turnflow-db-backup/pgbackrest/backup/turnflow/   --endpoint-url ...
# 기존 감시 태스크
docker compose -f docker-compose.prod.yml exec celery_billing \
  python manage.py shell -c "from apps.core.tasks import backup_health_check; print(backup_health_check())"
```
**판정:** 모두 정상 → RPO ≈ 분, failover/restore 테스트 진행 OK. 하나라도 ❌ → RPO=24h, **백업부터 수정**.
**키 보관:** Layer-1 GPG 개인키 + `PGBACKREST_CIPHER_PASS`를 **재해 전** 패스워드 매니저 + 사무실 복구박스에 보관(없으면 복구 자체 불가). 월간 드릴에서 decrypt 검증.

### 9.4 R2 lifecycle (30일 리텐션)
R2 버킷 lifecycle 규칙으로 `pgbackrest/` 프리픽스 30일 경과 객체 만료(또는 `repo1-retention-full` 상향). 미디어 버킷과 독립.

---

## 10. ✅ 전체 작업 리스트업

### 10.1 코드 (PR로 리뷰) — 착수 순서
**P0 (토대, 동작 무변화 — 안전)**
- [ ] `SITE_ID` 설정 + `.env.example`(colo) / `.env.production`(office) 분기
- [ ] `SiteControl` 모델 + migration 0001 + 데이터 마이그레이션(pk=1 seed) + `apps/core/site_control.py`
- [ ] `ScheduledJob` 모델 + migration + `0002_seed_scheduled_jobs`(14잡, 30s→60s)
- [ ] `/healthz/live` + `/healthz/ready`(5중 체크) + `healthz` alias + 라우팅
- [ ] `deploy/caddy/Caddyfile.maintenance` + `maintenance.html` + production Caddyfile `@health`/`@maint_health` 명시화 (+ `caddy validate`)

**P1 (DR 메커니즘)**
- [ ] `ActiveSiteGateMiddleware`(LoggingMiddleware 직후, `/healthz*`·`/internal/scheduler/*` 예외, fully-dark)
- [ ] Celery `task_prerun` 게이트 + epoch 펜스 (`send_dm_task`/`process_messaging_event` 등)
- [ ] `/api/v1/internal/scheduler/tick`(공유시크릿+IP allowlist, select_for_update due-eval, send_task) + `@extend_schema`
- [ ] **거버너 `rehydrate_from_db()` + `DMAccountBlock` 모델 + 듀얼라이트/캐시폴백 (#2)** + `worker_ready` 훅
- [ ] `dr_catchup.py` + `mark_restore_complete.py`(`--promote`/`--demote`) 관리 명령
- [ ] `restore_to_office.sh`(비파괴, db:5432 직결 migrate) + `deploy.sh`/restore에 `restore_complete` 와이어링

**P2 (컷오버·고도화)**
- [ ] `READY_PROBE_SIBLINGS` cross-tier fan-out
- [ ] `CELERY_BEAT_SCHEDULE` 주석화([LEGACY] 관례) — 외부 tick 안정 가동 후
- [ ] (선택) admin_api 읽기전용 ScheduledJob/SiteControl 뷰
- [ ] `restore_pitr_drill.sh` + 월간 cron

신규 settings 키: `SITE_ID`, `SCHEDULER_TICK_SECRET`, `SCHEDULER_TICK_ALLOWED_IPS`, `HEALTHCHECKS_TICK_URL`, `PASSIVE_ALLOW_READS`, `READY_REQUIRE_SCHEDULER_HEARTBEAT`, `READY_PROBE_SIBLINGS`, `DM_GOVERNOR_ENABLED`(기존).

### 10.2 운영 / 외부 서비스 (PR 아님)
- [ ] **Cloudflare R2**: `turnflow-db-backup` 버킷 + 최소권한 API 토큰(R/W) + 복구용 읽기전용 토큰 + lifecycle 30일 (§9.1/9.4)
- [ ] **pgBackRest → R2**: `pgbackrest.conf` 작성 + `postgresql.archive.conf` + stanza-create + 첫 full backup (§9.2)
- [ ] **D-1 검증**: pg_stat_archiver / pgbackrest check / R2 객체 / backup_health_check (§9.3)
- [ ] **암호화 키 off-box 보관 검증** (GPG 개인키 + PGBACKREST_CIPHER_PASS)
- [ ] **Cloudflare Load Balancer**: 풀 2개 + 모니터 2개 + failover + Telegram 알림 (§8.1)
- [ ] **외부 스케줄러**: CF Cron Worker(primary, 60s) + Google Cloud Scheduler(secondary) → 양 풀 `/tick` POST + egress IP를 `SCHEDULER_TICK_ALLOWED_IPS`에
- [ ] **Healthchecks.io**(또는 Better Stack): dead-man 체크 생성 → `HEALTHCHECKS_TICK_URL`
- [ ] **사내 웜스탠바이 박스**: docker + pgbackrest + `.env`/conf 스테이징 + maintenance Caddy 기동
- [ ] **`docker-compose.prod.yml`**: tier 헬스체크 → `/healthz/live`, celery_beat 미기동 프로필
- [ ] **Azure 콜드 VM**: `deploy/dr/azure/` Terraform/cloud-init/복구 래퍼
- [ ] **SERVER_RUNBOOK.md** DR 섹션: promote/demote/failover/failback/센티넬 정책/타임라인 펜스

---

## 11. 진행 체크리스트 (resumable)

| 단계 | 상태 | 비고 |
|---|---|---|
| 계획 문서(본 파일) | ✅ | |
| **P0 코드** | ✅ (2026-06-26) | SiteControl/ScheduledJob 모델·마이그레이션(적용·시드 15잡)·SITE_ID·DMAccountBlock·/healthz/live·ready·라우팅 |
| **P1 코드** | ✅ (2026-06-26) | ActiveSiteGateMiddleware·Celery task_prerun 게이트·worker_ready 재수화·/internal/scheduler/tick·거버너 rehydrate_from_db+DMAccountBlock 듀얼라이트·dr_catchup·mark_restore_complete·restore/failover/failback 스크립트·maintenance Caddyfile |
| 코드 검증 | ✅ (2026-06-26) | manage.py check 무이슈 · makemigrations --check 일치 · 마이그레이션 적용 · 15 pytest 통과 · Caddyfile×2 validate · 게이트 demote/promote E2E(epoch 1→2→3) |
| D-1 prod 백업 검증 | ⬜ | **사용자 작업** — failover/restore 테스트 전 필수 (§9.3) |
| R2 + pgBackRest 셋업 | ⬜ | **사용자 작업** (§9) |
| CF LB + 외부 cron 셋업 | ⬜ | **사용자 작업** (§8.1, §10.2) |
| 사무실 웜스탠바이 | ⬜ | **사용자 작업** |
| failover 드릴 E2E | ⬜ | |
| P2 컷오버(beat→tick) | ⬜ | 외부 tick 안정 가동 확인 후 celery_beat 정지 |
| Azure 콜드 + failback 드릴 | ⬜ | |

> 재개 시: 위 표에서 첫 ⬜ 단계부터. 코드 작업은 §10.1 체크박스, 운영은 §10.2 체크박스를 진행 단위로.

---

## 12. 테스트 계획 (코드 후, 마지막)

1. **단위/통합**: ready() 5중 체크 각 실패 케이스 503, 미들웨어 게이트(active=통과/passive=503, healthz·tick 예외), tick single-fire(동시 2회→1 fire), 거버너 `rehydrate_from_db()`가 SentDMLog 카운트와 일치, DMAccountBlock 듀얼라이트/폴백.
2. **failover 드릴 E2E** (스테이징/사무실): 콜로 ready 죽임 → CF ~45s 내 사무실 점검페이지 + Telegram → failover.sh(restore→migrate→dr_catchup→promote epoch++→Caddy 스왑) → 사무실 ready 200 → CF 사무실 앱 서빙 → **DM 재개(동결 0초 확인)** → 되돌아온 stale-epoch 콜로가 passive 유지(write 503/task Reject) 확인.
3. **restore 드릴**(월간): §7.6, RTO/RPO 기록.
4. **split-brain 테스트**: 콜로·사무실 동시 가동 시 한쪽만 write/celery/tick 동작 확인(epoch 펜스).

---

## 13. 리스크 & split-brain 방지

| 위험 | 완화 |
|---|---|
| **SPLIT-BRAIN(양 DB write)** | SiteControl 단일행 + write-gate(3티어) + Celery prerun + **epoch 펜싱**(stale 콜로 self-passivate) + failback 네트워크 펜스. **billing.* 이중실행이 최상위 위험** → active_site 권위성 절대조건 |
| **stale-콜로 재부착** | promote가 epoch++를 원자적·필수(FOR UPDATE) |
| **false failover** | scheduler heartbeat ready 하드체크 초기 제외, Redis 짧은 재시도, CF N회 연속 실패 후 전환 |
| **maintenance gap** | 사무실 풀은 `/maintenance-health`(항상 200) 모니터 + production도 200 |
| **조기 promote 손실** | `restore_complete=False`가 ready=503 유지, promote는 restore+migrate 후만 |
| **schema skew** | web 기동 전 `migrate --check`(db:5432 직결) |
| **🔑 거버너 동결/Action Block 소실** | **#2: SentDMLog 재수화(동결 제거) + DMAccountBlock 영속화(차단 유지)** |
| **RPO 손실** | D-1 검증(PITR 실제 ON) 통과 후에만 분 단위 신뢰 |
| **이중 스케줄링** | tick·beat 상호배타 + tick active_site 게이트 + 전 잡 멱등 |
| **암호화 키 소실=DR 총체실패** | 키/패스 재해 전 off-box 보관 + 월간 드릴 decrypt 검증 |
| **restore-through-pgbouncer** | 복구는 db 볼륨 직접, migrate는 db:5432 직결 |
| **internal tick 노출** | 공유시크릿(상수시간) + IP allowlist + POST-only 405 + Caddy rate-limit |
| **pgBackRest 타임라인 split** | promote 후 즉시 full backup + check, 콜로 demote/wipe 전 archive_mode 재기동 금지 |

---

## 14. 미해결 / 오너 결정
1. (D-1) Layer-2 PITR이 prod에 실제 ON인가 — §9.3 검증 결과로 확정.
2. DR-failover 시 거버너 재수화의 RPO-staleness 보수 버퍼를 둘지(기본: 700-vs-750 마진에 의존).
3. 3개 crontab(`*/6h`,`04:00`,`04:30`) `next_due_at` 계산: `celery.schedules.crontab` 재사용 vs `croniter` 추가(타임존 Asia/Seoul, DB UTC).
4. `backup_health_check`가 passive 스탠바이에서도 fire하는지(자체 DB 보유) — active_site 게이트 우회 유일 잡 여부 확인.
5. failback을 실제 수행할지(콜로 재시드) vs 콜로 영구폐기.

---

*본 계획은 코드 변경 없이 설계만 확정한 문서다. 착수는 §10.1 P0부터, failover/restore 테스트는 §9.3(D-1) 통과 후.*

---

## 15. 용량·백업 운영 계획 (2,000-인플루언서 스케일)

> 연동(런칭) 전에 확정·구현하기로 함. R2 비용은 무의미(수백 GB=월 $5~15, 대역폭 0)하므로 목표는
> "비용 절감"이 아니라 **라이브 DB·R2 용량을 *유계*로 묶는 것**. 진짜 병목은 R2가 아니라
> **SentDMLog 라이브 증가(~120 GB/월)**.

### 15.1 스케일 가정 & 산정
- 목표: **유료 인플루언서 2,000명** × **주 1캠페인** × **댓글/DM 1만** (추후 증가 가능).
- 파생: **2,000만 DM/주 ≈ 286만/일 ≈ 1,980 DM/분 평균**(군집 피크 1만+/분, 3-tier 설계 한도 내), **8,600만 DM/월**.

| 저장 대상 | 증가/정상상태 | 성격 | 행당(추정) |
|---|---|---|---|
| SentDMLog (라이브) | **~120 GB/월** | 영구·hot | ~1.4 KB (인덱스 11개) |
| EventInbox (prune 시 7일) | 정상 ~40 GB | 유계 | ~1.0 KB (echo+read 2행+JSON) |
| SeenComment | 정상 ~6 GB | TTL 10일 | ~0.2 KB |
| R2 WAL(압축) | ~250 GB/월 생성, 보존 ~2주 → 정상 ~125 GB | 유계 | — |
| R2 base backup | DB크기 비례 | retention 2 | message_sent 반복 → 압축 양호 |

R2 ops: WAL 세그먼트 월 ~5만~11만 PUT ≪ Class A 무료 100만 → ops 비용 0.

### 15.2 ① retention (확정) — `deploy/backups/pgbackrest.conf.example`
```ini
repo1-retention-full=2          # ①이번 주 ②지난 주 (폴백 안전망)
repo1-retention-diff=6          # 현재 full 안에서 일별 diff 6개(1주)
repo1-retention-archive=2       # ★ WAL을 full 2개 구간만 보관 → 이전 WAL 자동 expire
repo1-retention-archive-type=full
```
- 효과: 약 **2주 rewind 윈도우** + full 폴백 1개. 최근 복구(RPO=분)는 영향 없음.

### 15.3 ② 백업 cron (확정: 주1 full + 일 diff) — NET-NEW `deploy/backups/pgbackrest_backup.sh`
- WAL 아카이빙: 연속(이미 §9 계획). **건드리지 않음**.
- **full**: 주 1회, **최저 트래픽 새벽**(잠정 화 03:00 KST — 주말 인플루언서 포스팅 피크 회피, 관측 후 조정).
- **diff**: 매일 03:30 KST (변경분만 → 빠름).
- 래퍼: `docker compose exec -u postgres db pgbackrest --stanza=turnflow --type=<full|diff> backup` + Telegram 알림(pg_backup.sh 패턴), host crontab.
- ⚠️ **db 볼륨 디스크 헤드룸 ≥ 수십 GB**: R2 업로드 지연 시 WAL이 `pg_wal`에 백로그(피크 ~100 MB/분).
- ⚠️ full은 surge와 I/O 경쟁 → 반드시 저트래픽 창. `start-fast=y` 적용됨.

### 15.4 ③ EventInbox 정리 (확정: 7일 + 일별 파티션 DROP)
- **사유**: 하루 ~570만 행 → 단순 `DELETE`는 락·WAL 폭증. **일별 range 파티션 + 옛 파티션 `DROP`**(즉시, WAL≈0)이 정석.
- **UNIQUE 제약 영향**: 파티션 PK/unique는 파티션키 포함 필수 → `event_key` UNIQUE → `(event_key, received_at)` per-partition.
  - 위험 **낮음**: 중복 웹훅은 분 단위로 도착(같은 날=같은 파티션에서 잡힘). 일 경계의 드문 재전송은 **echo/read 재처리=멱등 status UPDATE**라 무해. (SentDMLog과 달리 실제 발송을 제어하지 않음)
- NET-NEW: 파티션 전환 마이그레이션 + `manage_partitions` 일일 태스크(차일 파티션 선생성 + 7일 초과 DROP).

### 15.5 ④ SentDMLog 월별 파티션 (확정: 지금 도입) — ★ idempotency 하드보증 충돌 해소 필요
**문제 (코드 스캔 결과):** 운영 `SentDMLog.objects.create()` **5곳**(tasks.py 513·714·2009·2403·2453)이 모두
`create() → except IntegrityError → 기존 row fetch` 패턴 = **전역 UNIQUE(idempotency_key)** 에 의존(모델 §1480 "정확히 한 번의 하드 보증").
PG 네이티브 range 파티션(by `created_at`)은 모든 UNIQUE/PK가 **파티션키를 포함**해야 함 →
`idempotency_key` UNIQUE → `(idempotency_key, created_at)` **per-partition** 으로 약화 → **월 경계 중복 발송 가능**(이론).

**해소 옵션 (오너 결정 필요):**
- **(A) 전역 dedup 레저 (권장 — 하드보증 유지):** NET-NEW 비파티션 테이블 `DMDedupKey(idempotency_key PK, created_at)`.
  5개 create 사이트가 **먼저 `DMDedupKey` INSERT(ON CONFLICT)** 로 멱등 판정 → SentDMLog는 자유롭게 파티션/아카이브.
  레저는 재트리거 윈도우(~30~45일) 밖이면 prune·파티션 가능 → 유계. **단 hot DM 경로 5곳 + 신규테이블 + 테스트** 필요.
- **(B) per-partition unique + SeenComment 실무 dedup (가벼움):** 제약만 변경, create 사이트 무수정(같은 달 중복은 여전히 IntegrityError).
  월 경계의 드문 중복은 SeenComment(TTL 10일) 앵커가 사실상 커버. **단 문서화된 "DB 하드보증"이 "2-소프트레이어"로 다운그레이드.**
- (참고) (C) 비파티션 유지 + 배치 이관/삭제: 전역 unique 유지하나 대량 DELETE 부하·블로트 → 파티션보다 운영 무거움.

**파티션 운영(공통):** PK `id` → `(id, created_at)`. 월별 파티션. `manage_partitions` 태스크가 **익월 파티션 선생성** +
6개월 초과 파티션 **detach → R2 dump → drop**(콜드 아카이브) → hot DB "최근 6개월"로 유계 → full 백업도 빨라짐.
**런칭 전(빈 테이블)이 마이그레이션 최적기.**

> 곁가지(나중): SentDMLog 인덱스 11개 = WAL 증폭 주범. 중복 인덱스(`comment_id` 단독 + `(campaign, comment_id)`) 정리 시 쓰기↓.

### 15.6 실행 순서 (2 워크스트림)
- **WS-1 (안전·즉시 가능, 상관관계 없음):** ① retention 2줄 + ② 백업 cron 스크립트 + ③ EventInbox 일별 파티션·prune.
  → 정확성 리스크 0. 먼저 머지/배포 가능.
- **WS-2 (런칭 전 수술, hot-path):** ④ SentDMLog 파티션 + dedup(옵션 A 권장) + `manage_partitions` + 아카이브.
  → `loadtest_dm.py`로 파티션·dedup 부하/정합 검증 후 머지.

### 15.7 미해결 (오너 결정)
1. ~~④ dedup: (A) 레저 vs (B)~~ → **§15.8 에서 개정**(하이브리드 확정, 레저 제거).
2. full 백업 창 요일/시각(잠정 화 03:00 KST) — 실제 트래픽 관측 후 확정.
3. SentDMLog 배치 아카이브 보존 hot 기간(잠정 6개월) + 아카이브 포맷(R2 Parquet/CSV/COPY).
4. EventInbox 파티션 유지일 7일 확정(재처리 멱등성 재확인).

### 15.8 ★ 결정 개정 (REVISED) — 하이브리드 확정, Django 업그레이드 보류, dedup 레저 제거

**배경:** ④ SentDMLog 파티셔닝을 파려다 두 가지 제약 발견:
1. **Django 5.0 은 `CompositePrimaryKey` 미지원**(5.2+). PG 네이티브 파티션은 모든 PK/UNIQUE 가
   파티션키 포함 필수 → 모델(단일 PK)과 DB(복합 PK) 불일치.
2. **SentDMLog 에 self-FK `parent_log`(SET_NULL)** 존재. 복합 PK 모델로의 FK 는 Django 5.2 에서도
   미지원이고, PG 레벨에서도 파티션 테이블로의 FK 는 파티션키 포함을 요구 → 버전 무관 걸림돌.
   (단 `parent_log` 은 코드에서 거의 `parent_log_id`(child 플래그/링크)로만 쓰여 강등은 저영향.)

**Django 업그레이드 검토 → 보류:** 5.2 로 올리면 (1)은 풀리나 (a) Django 5.0→5.2 + **DRF 3.14→3.16**
연쇄 업그레이드로 앱 전체 회귀 위험(런칭 직전), (b) `CompositePrimaryKey` 도 **복합 PK 로의 FK 미지원**이라
(2) self-FK 는 *여전히* 안 풀림. → **업그레이드는 런칭 후 별도 정비 과제로 분리.**

**확정 — 하이브리드:**
| 테이블 | 방식 | 이유 |
|---|---|---|
| **EventInbox** (일 570만 행) | **일별 range 파티션 + DROP** | 들어오는 FK 없음 → 깔끔. 고회전 → 즉시 DROP 필수. (모델은 id-PK 유지, DB 만 복합 PK; `SeparateDatabaseAndState` 로 hand-roll RunSQL) |
| **SentDMLog** (영구·self-FK) | **배치 아카이브** (파티션 X) | self-FK·복합 PK 문제 **원천 회피**. 월 증가 느리고 업무기록이라 월 1회 배치 DELETE 감내 가능. **전역 `UNIQUE(idempotency_key)` 유지** |

**귀결 — DMDedupKey 레저 제거:** SentDMLog 가 전역 UNIQUE 를 유지하므로 증분 1 의 `DMDedupKey`(전역 레저)는
**redundant**(DM 당 INSERT 1회 + 월 86M 행 고회전 테이블, 무이득). → **레저 제거**, `create_idempotent` 헬퍼는
**전역 UNIQUE 백엔드**로 되돌리되 무손실 하드닝(비-키 IntegrityError 전파)은 유지. 마이그레이션 0029 가
`DMDedupKey` DROP. (향후 Django 업그레이드 후 SentDMLog 파티셔닝을 정말 하게 되면 그때 레저 재도입.)

**증분 2 작업 (개정):**
- (a) `DMDedupKey` 제거(0029 DeleteModel) + `create_idempotent` 전역-UNIQUE 백엔드로 재작성(하드닝 유지) + 테스트 갱신.
- (b) EventInbox 일별 파티션 전환(RunSQL + SeparateDatabaseAndState) — id-PK 모델 유지, `(event_key, received_at)` per-partition unique.
- (c) `maintenance` 태스크: EventInbox 차일 파티션 선생성 + 7일 초과 DROP, SentDMLog 6개월 초과 배치 아카이브(R2 COPY → 배치 DELETE).
- (d) `loadtest_dm` 로 부하/정합 검증.
