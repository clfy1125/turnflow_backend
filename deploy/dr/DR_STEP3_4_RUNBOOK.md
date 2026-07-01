# DR 활성화 — Step 3 & 4 실행 런북 (나중에 진행)

> 사내(office) 박스가 준비되면 이 문서대로 진행한다. 설계 배경은 `DR_IMPLEMENTATION_PLAN.md` §4(Caddy↔DR), §8(LB/failover), §7.4(office 복구) 참조. 이 문서는 **실행 순서/명령**에 집중.

## 0. 전제 — 이미 완료된 것 (✅)
- **Step 1 (PITR)**: 콜로 db 에 pgBackRest 내장, WAL → R2 연속 아카이빙(RPO=분), 주1 full/일 diff cron. (`deploy/backups/pgbackrest.conf`, `pgbackrest_backup.sh`)
- **Step 2 (외부 스케줄러)**: CF Worker → `POST /api/v1/internal/scheduler/tick` 단독 스케줄러. `celery_beat` 은퇴(`profiles:[fallback]`). 시크릿 = `.env.production:SCHEDULER_TICK_SECRET`.
- DR 코드 일체 배포됨: `SiteControl`(active_site/epoch/mode/restore_complete), `ScheduledJob`, `/healthz/live`·`/healthz/ready`, `ActiveSiteGateMiddleware`, `dr_catchup`, `mark_restore_complete`, rate-governor `rehydrate_from_db`.

## 0.1 핵심 개념 (3줄)
- **active_site / epoch**: 어느 사이트가 권위인지 + 단조 증가 펜스. promote 시 `active_site=이박스, epoch++`.
- **헬스 2종**: `/api/v1/healthz/ready`(deep — active 일 때만 200) vs `/maintenance-health`(maintenance Caddy 에서 항상 200).
- **모드 스왑**: passive=`Caddyfile.maintenance`(점검페이지+ready 503), active=`Caddyfile`(3-tier 앱). `caddy reload` 로 ~2초 무중단 전환.

---

# Step 3 — 사내 웜스탠바이 + Cloudflare Load Balancer

## 3-A) 사내(office) 박스 준비
**사양**: prod 부하 감당 수준(최소 콜로의 절반 권장) + docker/compose + 디스크 여유(DB+WAL, 수십 GB↑). 고정 공인 IP 또는 CF 가 닿는 경로.

1. **레포 + 이미지 준비** (콜로와 동일 태그 핀고정):
   ```bash
   sudo git clone <repo> /opt/turnflow_backend && cd /opt/turnflow_backend
   git checkout hardening/dm-surge   # 콜로와 동일 브랜치/커밋
   ```
2. **시크릿/설정 배치** (콜로 .env.production 과 거의 동일, **SITE_ID 만 다름**):
   - `/opt/turnflow_backend/.env.production` — 콜로 것 복사 후 **`SITE_ID=office`** 로 변경. `SCHEDULER_TICK_SECRET`·`SECRET_KEY`·DB 비번·META/IG 토큰·R2 등은 **동일** 유지.
   - `/opt/turnflow_backend/deploy/backups/pgbackrest.conf` — 콜로 것과 동일(R2 repo 읽기 가능해야 함. 읽기전용 토큰 권장). cipher pass 동일.
3. **maintenance Caddy 로 기동** (passive 상태 — 점검페이지만):
   ```bash
   # Caddy 컨테이너가 /etc/caddy 를 보도록 구성(콜로와 동일 토폴로지)
   cp deploy/caddy/Caddyfile.maintenance /etc/caddy/Caddyfile
   # maintenance.html 도 Caddy 가 서빙하는 경로에 배치(deploy/caddy/maintenance.html)
   # Caddy + (선택) redis 정도만 띄움. db/web/celery 는 failover 때 restore_to_office.sh 가 기동.
   ```
   확인: `curl -s -o /dev/null -w "%{http_code}" http://localhost/maintenance-health` → **200**, `/api/v1/healthz/ready` → **503**.
4. **(선택, RTO 단축) 주기적 사전 복구**: office 에서 `restore_to_office.sh` 를 야간 cron 으로 돌려 DB 를 최신 근처로 유지 → failover 때 `--delta` 가 최근 WAL 만 재생(복구 빠름). 단 복구 중엔 office db 가 잠깐 바뀌므로 passive(트래픽 0)일 때만.

## 3-B) Cloudflare Load Balancer
`turnflow-api.clfy.ai.kr` 를 단일 오리진 DNS → **CF Load Balancer** 로 전환. (Terraform: `cloudflare_load_balancer(_pool|_monitor)`)

| 풀 | 오리진 | 모니터 경로 | 비고 |
|---|---|---|---|
| `colo-production` | 콜로 IP | `/api/v1/healthz/ready` (deep) | 200 기대 |
| `office-fallback` | 사내 IP | `/maintenance-health` | passive 에도 항상 200 (안 그러면 CF 가 office 를 DOWN 처리해 점검페이지 대신 CF 에러) |

- **steering = failover**, 순서 **colo → office** (colo primary).
- **session affinity OFF** (stateless + JWT).
- **proxied ON** (WAF/Caddy 하드닝 유지).
- 모니터: interval 15s / timeout 5s / consecutive_down 3 / up 2 (~45s 감지).
- 알림 → Telegram.
- ⚠️ office 풀 모니터는 **`/maintenance-health`** (절대 `/healthz/ready` 아님 — passive 시 503 이라 DOWN 처리됨). production Caddyfile 에도 `/maintenance-health→200` 이 있어 **failover 후에도 모니터 경로를 안 바꿔도 됨**.

검증: CF LB 대시보드에서 colo 풀 Healthy, office 풀 Healthy(점검 200). 평상시 트래픽은 colo 로만.

---

# Step 4 — Failover 드릴 (E2E)

> ⚠️ 실 prod 에서 하면 짧은 단절 발생 → **저트래픽 창** 또는 스테이징에서. 메커니즘 검증이 목적.

1. **콜로 ready 죽이기**(장애 모사): 콜로 `/api/v1/healthz/ready` 가 503 되게 — 예: 콜로 web 정지 또는 `mark_restore_complete --demote`(mode≠live). → CF LB 가 ~45s 내 **office 풀로 전환**, 사용자는 office **점검페이지** 봄.
2. **사내 박스에서 failover 실행**:
   ```bash
   cd /opt/turnflow_backend
   bash deploy/dr/failover.sh ["YYYY-MM-DD HH:MM:SS+09"]   # 시각 생략=최신
   ```
   내부 흐름(=`failover.sh`): `restore_to_office.sh`(PITR 복구→migrate→앱 기동) → `dr_catchup --skip-poll`(rate-governor/Action Block **DB 재수화** → DM 동결 0초) → `mark_restore_complete --promote`(active_site=office, **epoch++**) → Caddy maintenance→production `reload`.
3. **검증**:
   ```bash
   curl -fsS https://turnflow-api.clfy.ai.kr/api/v1/healthz/ready   # office 가 200 → CF 가 실앱 서빙
   ```
   - 프론트 로그인 / 공개페이지 / **DM 발송 재개** 확인(외부 cron tick 이 LB 타고 office 로 들어와 스케줄러 가동).
   - `scheduled_job.last_run_at` 갱신, deferred DM requeue 동작 확인.
4. **⚠️ 콜로 펜스 (split-brain 방지 — 가장 중요)**: 콜로가 되살아나도 **트래픽 받으면 안 됨**(콜로 DB 는 과거 → 데이터 분기).
   - CF LB 에서 **colo 풀을 비활성/디스에이블**(office 가 사실상 primary 가 되게). 자동 failback 절대 금지.
   - 되살아난 콜로 스택은 `restart: unless-stopped` 로 자동 기동될 수 있으니, **콜로 stack 정지 + ufw 로 443 차단**(failback 전까지 write 펜스).
   - colo 박스의 `is_active_site()`는 colo 자기 DB 만 보므로 자동으로 못 막는다 → **운영자가 CF LB + 네트워크로 펜스**해야 함.

---

# Appendix A — Failback (office → colo) : **수동 승인 전용, 가장 위험**
자동 금지. office 가 active 인 동안 **office DB 가 최신 원본**. 절차(`deploy/dr/failback.sh` 가 승인 게이트 골격 제공):
1. 콜로 write 펜스 유지(stack 정지 + ufw).
2. 콜로의 과거 DB 격리/폐기.
3. **office 백업(R2)에서 콜로 DB 재시드**(`restore_to_new_box.sh` 또는 `pgbackrest restore`).
4. 콜로 `migrate --check` + `/healthz/ready` 검증 → `FAILBACK_READY`.
5. 사람 승인 → 콜로 `mark_restore_complete --promote`(epoch++) → CF LB colo 복귀 → office `--demote` standby.
- ⚠️ **pgBackRest 타임라인 split**: office promote 가 새 타임라인 생성 → 콜로 재시드 후 새 full backup + `pgbackrest check` 로 정합화, 콜로 `archive_mode=on` 재기동은 **그 후**.

# Appendix B — Azure 콜드 (콜로+사내 둘 다 상실 시에만)
`deploy/dr/azure/` (Terraform VM + NSG 443 CF-only + cloud-init: docker/pgbackrest/gpg/awscli + 핀고정 clone + `restore_from_r2.sh` + 시크릿스토어에서 .env/pgbackrest.conf pull + `start_stack.sh` + CF 풀 업데이트). 상세 §8.4.

---

# 막히기 쉬운 지점 / 체크리스트
- [ ] office `.env.production` 의 **SITE_ID=office** (콜로는 colo). 나머지 시크릿 동일.
- [ ] office `pgbackrest.conf` 가 R2 repo **읽기** 가능(`pgbackrest --stanza=turnflow info` 로 콜로 백업 보이는지 확인).
- [ ] office Caddy 가 **maintenance 모드**로 시작(점검 200 / ready 503).
- [ ] CF LB office 풀 모니터 = **`/maintenance-health`** (`/healthz/ready` 아님).
- [ ] failover 후 **콜로 펜스**(CF LB colo 풀 disable + ufw) — split-brain 1순위 위험.
- [ ] **자동 failback 없음** — 항상 수동 + 콜로 DB 재시드 후.
- [ ] 암호화 키(cipher/GPG private)는 **양 사이트 밖**에도 보관(분실=복구 불가).
- [ ] 외부 cron 시크릿/도메인은 그대로 — tick 이 LB 타고 active 사이트로 자동 라우팅(전용 도메인 만들지 말 것).

> 사내 박스 확보되면 3-A부터. 각 단계 후 검증 출력 확인하며 진행. 드릴(Step 4)은 반드시 저트래픽/스테이징에서 1회 리허설.
