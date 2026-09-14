# Cloudflare Workers — DR 감지기 + (선택) 점검페이지

DR 회색지대 감지의 **CF 엣지 vantage**. `wrangler` 로 배포한다.

| 파일 | 역할 | prod 경로 영향 |
|---|---|---|
| `detector-worker.js` + `wrangler.toml` | Cron(매 분): tick + `/healthz/live`·`/healthz/diag` 프로빙 → 채점 → 상태기계(KV) → Telegram 경보. **DR Step 2 tick 워커를 대체.** | 없음(out-of-band) |
| `maintenance-worker.js` | (선택) 라우트 바인딩: 원본 5xx/불가 시 엣지 점검페이지(503). | **있음**(모든 요청 통과) — Phase B/컷오버와 함께 권장 |

> Phase A 범위 = **감지 + 경보까지만**. CONFIRMED_DOWN 은 "사람이 failover 검토" 경보일 뿐, 트래픽을
> 바꾸지 않는다(사람승인 컷오버). 자동 트리거/2-vantage 정족수는 Phase B(GCP Cloud Run)에서 붙는다.

---

## 1. 감지기(Cron Worker) 배포 — 먼저, 안전

```bash
cd deploy/dr/cloudflare
npm i -g wrangler            # 미설치 시
wrangler login

# 1) KV 네임스페이스 생성 → 출력된 id 를 wrangler.toml 의 <KV_NAMESPACE_ID> 에 기입
wrangler kv namespace create DR_STATE

# 2) 시크릿 등록 (vars 가 아니라 secret 으로 — 평문 노출 금지)
wrangler secret put SCHEDULER_TICK_SECRET  # = 서버 .env.production 의 동일 시크릿(기존 tick 워커가 이미 보유)
wrangler secret put TELEGRAM_BOT_TOKEN   # 서버와 동일 봇
wrangler secret put TELEGRAM_CHAT_ID
# (선택) wrangler secret put HEALTHCHECKS_TICK_URL   # 서버가 tick 시 ping 하므로 대개 불필요

# 3) 배포 (cron "* * * * *" 자동 등록)
wrangler deploy
```

> ⚠️ **이미 가동 중인 워커를 갱신하는 경우**(= 지금 상태) 두 가지를 반드시 확인한다.
> 1. **이름** — 실가동 워커는 `turnflow-scheduler-tick`(tick 워커를 덮어써서 대체했다).
>    `wrangler.toml` 의 `name` 이 이것과 다르면 **새 워커가 하나 더 생겨** 옛 코드가 계속 돌고
>    tick 이 분당 2회 발사된다.
> 2. **KV id** — `wrangler kv namespace list` 로 기존 `DR_STATE` id 를 확인해 넣는다. 새로
>    create 하면 상태가 초기화돼 진행 중인 에피소드·중복경보 억제(`alerted`)가 풀린다.
>
> 시크릿(`wrangler secret put` 으로 넣은 것)은 배포해도 유지되지만, `[vars]` 는 **toml 값으로
> 덮어쓰인다** — 대시보드에서 임계값을 손댔다면 배포 전에 toml 에 반영할 것.

확인:
- `wrangler tail` 로그에서 매 분 실행 확인.
- 서버 web_dashboard 액세스로그에 `POST /api/v1/internal/scheduler/tick 200` 이 계속 오는지(= tick 정상 이관).
- 현재 감지 상태: `curl -H "X-Scheduler-Secret: <secret>" https://<worker-url>/?debug=1` → `{state, last, ...}` JSON.

> **기존 Step 2 tick 워커는 비활성/삭제**한다(이 워커가 tick 을 포함하므로 이중 발사 방지). 둘 다 두면 분당 2회 tick → 무해(단일발사 불변식)지만 정리 권장.

### 임계 튜닝(`wrangler.toml [vars]`)
- `T_WINDOW_SECONDS=1800` — 30분 지속돼야 CONFIRMED(오탐 방어 핵심). 더 빠른 RTO 원하면 줄이되 오탐 위험↑.
- `QUEUE_WARN`/`WORKER_STALE_SECONDS` — 둘 다 충족해야 STALL(무트래픽 오탐 방지).
- `EXPECTED_ACTIVE_SITE=colo` — 이 값과 diag 의 `active_site` 가 다르면 감지기 disarm(이미 failover 로 판단).

---

## 2. 점검페이지(선택) — 컷오버 기계와 함께

```bash
# 별도 워커로 배포(라우트 바인딩 필요). prod 경로가 바뀌므로 저트래픽 창에.
wrangler deploy maintenance-worker.js --name turnflow-dr-maintenance \
  --route "turnflow-api.clfy.ai.kr/*"
```
- 헬스/내부/웹훅 경로는 패스스루 예외(LB 모니터·IG 웹훅 영향 없음).
- 원본 정상이면 투명 패스스루, 5xx/불가면 503 점검페이지.
- **Phase A 단독(자동복구 없음) 단계에선 선택** — colo 다운 시 점검페이지는 보여주나 복구는 수동. Phase B 컷오버와 함께 켜는 걸 권장.

---

## 감지 신호/상태기계 요약 (detector-worker.js)
- 신호: S1 도달성(live), S2 앱 미준비, S3/S4 DB·Redis, migrations, S6/S7 큐적체×워커stall, S8 deferred DM, S9 WAL(경보전용).
- 판정: `db_ok|redis_ok 단독` OR `hard≥2` OR `(큐적체 AND 워커stall)` OR `deferred 적체` → UNHEALTHY.
- 상태기계: `HEALTHY → DEGRADED(첫 unhealthy) → SUSPECTED_DOWN(≥T_SUSPECT) → CONFIRMED_DOWN(≥T_WINDOW)`. healthy poll 2회면 회복.
- **live 프로브는 1회 재시도 후 채점**(2026-09-14 추가). 서버 gunicorn 이 `--max-requests` 로
  워커를 재활용(하루 8~10회)할 때 Caddy 의 keep-alive 커넥션이 reset 되어 502 한 번이 뜨는데,
  이게 S1(HOST_DOWN)으로 채점돼 DEGRADED 오탐을 냈다. 실장애는 재시도도 실패하므로 감지는
  `PROBE_RETRY_DELAY_MS`+타임아웃 만큼만 늦어진다.
- **🟢 회복 경보는 🟠/🔴 가 실제로 나갔던 에피소드에만 발신**(2026-09-14 추가). DEGRADED 에는
  경보가 없으므로, 종전엔 3분 미만 깜빡임이 "경보 없이 회복 알림만" 오는 형태가 됐다.
  조용히 지나간 깜빡임은 KV 의 `episodes`(최근 10건: 시작·종료·klass·사유·폴수·notified)에 남는다
  — `?debug=1` 로 조회. 종전에는 `last` 가 매분 덮어써져 사후 원인추적이 불가능했다.
- 30분 창은 KV 의 `since_ts` 타임스탬프로 강제(워커 재시작 견딤). active_site 불일치 / passive 는 다운으로 세지 않음.
