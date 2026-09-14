/**
 * TurnFlow DR — Cloudflare Cron Worker: 외부 스케줄러 tick + 회색지대 장애 감지기.
 *
 * 이 워커는 DR Step 2 의 tick 워커를 **대체(superset)** 한다:
 *   1) 매 분 POST {ORIGIN}/api/v1/internal/scheduler/tick (기존 동작 유지 — 서버가 Healthchecks ping)
 *   2) GET /healthz/live + /healthz/diag 를 프로빙해 §A3 신호표로 채점
 *   3) 상태기계(HEALTHY→DEGRADED→SUSPECTED_DOWN→CONFIRMED_DOWN)를 KV 에 영속
 *   4) 전이 시 Telegram 경보
 *
 * Phase A 범위: **감지 + 경보까지만**. 자동 복구(GCP Cloud Run 깨우기 / 트리거)는 Phase B.
 *   - CONFIRMED_DOWN 은 "사람이 failover 하라"는 경보를 낼 뿐, 여기서 트래픽을 바꾸지 않는다(사람승인 컷오버).
 *   - 2-vantage 정족수: GCP 측 vantage(WAKE_URL)가 붙기 전까지는 CF 단독 vantage 로 **경보만** 한다
 *     (단독 vantage 로는 절대 자동 트리거하지 않음 — 오탐 방어).
 *
 * 설계: 계획서 Phase A2/A3/A4, DR_IMPLEMENTATION_PLAN.md §5/§6.
 */

const KV_KEY = "detector_state";

// ── 설정(wrangler vars, 문자열로 들어오므로 num() 로 변환) ───────────────────
function cfg(env) {
  return {
    origin: env.ORIGIN || "https://turnflow-api.clfy.ai.kr",
    expectedActiveSite: env.EXPECTED_ACTIVE_SITE || "colo",
    tSuspect: num(env.T_SUSPECT_SECONDS, 180), // DEGRADED→SUSPECTED (지속)
    tWindow: num(env.T_WINDOW_SECONDS, 1800), // SUSPECTED→CONFIRMED (30분 지속창)
    queueWarn: num(env.QUEUE_WARN, 5000),
    workerStale: num(env.WORKER_STALE_SECONDS, 600),
    deferredAge: num(env.DEFERRED_AGE_SECONDS, 3600),
    dbLatencyMax: num(env.DB_LATENCY_MS, 2000),
    probeTimeout: num(env.PROBE_TIMEOUT_MS, 5000),
    probeRetryDelay: num(env.PROBE_RETRY_DELAY_MS, 1500), // live 프로브 재시도 간격
    recoverHysteresis: num(env.RECOVER_HEALTHY_POLLS, 2),
  };
}

function num(v, dflt) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : dflt;
}

// ── HTTP 프로브(타임아웃 포함) ──────────────────────────────────────────────
async function probe(url, opts, timeoutMs) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...opts, signal: ctrl.signal });
    let body = null;
    try {
      body = await res.json();
    } catch (_) {
      body = null;
    }
    return { ok: true, status: res.status, body };
  } catch (e) {
    return { ok: false, status: 0, body: null, error: String(e) };
  } finally {
    clearTimeout(t);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * live 프로브는 **1회 재시도한 뒤에** 채점한다.
 *
 * 이유(2026-09-14 05:59 KST 실사례): 서버 gunicorn 은 `--max-requests 5000 --max-requests-jitter 500`
 * 으로 워커를 재활용한다(대시보드 컨테이너 기준 하루 8~10회). 바로 앞 tick 요청이 그 카운터를 채우면
 * 워커가 응답 직후 종료하고, Caddy 가 그 워커로 열어둔 keep-alive 커넥션을 다음 요청(= 이 프로브)에
 * 재사용하다 `connection reset by peer` → 502 를 한 번 낸다. 실장애가 아닌데 S1(HOST_DOWN)로 채점돼
 * DEGRADED 로 떨어졌고, 3분 안에 회복돼 🟢 회복 알림만 단독으로 나갔다.
 *
 * 실장애는 재시도도 실패하므로 감지가 늦어지지 않는다(최대 +probeRetryDelay +probeTimeout).
 */
async function probeLiveWithRetry(c) {
  const first = await probe(`${c.origin}/api/v1/healthz/live`, { method: "GET" }, c.probeTimeout);
  if (first.ok && first.status < 500) return { res: first, retried: false };
  await sleep(c.probeRetryDelay);
  const second = await probe(`${c.origin}/api/v1/healthz/live`, { method: "GET" }, c.probeTimeout);
  return { res: second, retried: true };
}

/**
 * /healthz/live + /healthz/diag 채점.
 * 반환: { healthy, klass, reasons[], passive, diagReachable }
 *   healthy=false → 이 poll 은 "다운"으로 카운트(단, passive 면 제외).
 */
function score(liveRes, diagRes, c) {
  const reasons = [];

  // S1 — 호스트 도달성: live 가 응답을 못 하면 호스트 다운(가장 강한 신호).
  if (!liveRes.ok || liveRes.status >= 500) {
    return { healthy: false, klass: "HOST_DOWN", reasons: ["live unreachable"], passive: false, diagReachable: false };
  }

  // diag 가 닿지 않으면 깊은 채점 불가 — 단독으로 '확정 다운'은 아님(소프트).
  if (!diagRes.ok || !diagRes.body) {
    return { healthy: true, klass: "DIAG_UNREACHABLE", reasons: ["diag unreachable (soft)"], passive: false, diagReachable: false };
  }

  const d = diagRes.body;

  // 이미 failover 됐거나 passive 인 박스 → 감지기 disarm(이 박스를 다운으로 세지 않음).
  if (d.active_site && d.active_site !== c.expectedActiveSite) {
    return { healthy: true, klass: "PASSIVE", reasons: [`active_site=${d.active_site}`], passive: true, diagReachable: true };
  }

  // hard 신호 카운트 (S2/S3/S4 + migrations)
  let hard = 0;
  if (d.db_ok === false) { hard++; reasons.push("db_ok=false"); }
  if (d.redis_ok === false) { hard++; reasons.push("redis_ok=false"); }
  if (typeof d.db_latency_ms === "number" && d.db_latency_ms > c.dbLatencyMax) { hard++; reasons.push(`db_latency=${d.db_latency_ms}ms`); }
  if (d.migrations_pending === true) { hard++; reasons.push("migrations_pending"); }

  // STALL 상관: 큐 적체 AND 워커 소비 증거 없음 (둘 다여야 — 무트래픽 오탐 방지)
  const dmDepth = d.queue_depths && typeof d.queue_depths.dm_send === "number" ? d.queue_depths.dm_send : 0;
  const whAge = typeof d.worker_heartbeat_age_s === "number" ? d.worker_heartbeat_age_s : null;
  // heartbeat 가 stale(>workerStale) 또는 부재(null=소비 증거 없음) → starved.
  // null 도 starved 로 보되, 30분 지속창이 배포 중 워커 재시작(초 단위)을 걸러내 오탐을 막는다.
  const workerStarved = whAge === null || whAge > c.workerStale;
  const stallCorrelated = dmDepth > c.queueWarn && workerStarved;
  if (stallCorrelated) reasons.push(`stall dm_send=${dmDepth} worker_age=${whAge === null ? "none" : whAge + "s"}`);

  // deferred DM 적체(밀린 시간) — 단독 STALL 신호
  const deferredStall = typeof d.oldest_deferred_dm_age_s === "number" && d.oldest_deferred_dm_age_s > c.deferredAge;
  if (deferredStall) reasons.push(`deferred_dm_age=${d.oldest_deferred_dm_age_s}s`);

  // WAL 아카이버는 **경보전용**(단독 트리거 금지) — failover 가 RPO 를 악화시키므로.
  const walBroken = d.wal && d.wal.broken === true;
  if (walBroken) reasons.push("WAL archiving broken (alert-only)");

  // UNHEALTHY 판정: db_ok/redis_ok 단독 OR hard>=2 OR (큐 적체 AND 워커 stall) OR deferred 적체
  const unhealthy =
    d.db_ok === false || d.redis_ok === false || hard >= 2 || stallCorrelated || deferredStall;

  let klass = "OK";
  if (unhealthy) klass = stallCorrelated || deferredStall ? "STALL" : "APP_WEDGED";
  else if (walBroken) klass = "DATA_RISK";

  return { healthy: !unhealthy, klass, reasons, passive: false, diagReachable: true, walBroken };
}

// ── 에피소드 기록(최근 10건) ───────────────────────────────────────────────
// KV 의 `last` 는 매 분 덮어써져서, 경보 없이 지나간 깜빡임의 사유가 사후에 남지 않았다
// (2026-09-14 조사에서 caddy/gunicorn 로그로 역추적해야 했던 이유). 이 배열이 그 기록이다.
const EPISODE_KEEP = 10;

function openEpisode(s, verdict, now) {
  const list = (s.episodes || []).slice(-(EPISODE_KEEP - 1));
  list.push({ start_ts: now, klass: verdict.klass, reasons: (verdict.reasons || []).slice(0, 5), polls: 1 });
  s.episodes = list;
}

function bumpEpisode(s) {
  const cur = s.episodes && s.episodes[s.episodes.length - 1];
  if (cur && !cur.end_ts) cur.polls = (cur.polls || 1) + 1;
}

function closeEpisode(s, now, peakState, notified) {
  const cur = s.episodes && s.episodes[s.episodes.length - 1];
  if (cur && !cur.end_ts) {
    cur.end_ts = now;
    cur.peak_state = peakState;
    cur.notified = notified;
  }
}

// ── 상태기계 ────────────────────────────────────────────────────────────────
function transition(state, verdict, now, c) {
  const s = state || { state: "HEALTHY", since_ts: null, healthy_streak: 0, alerted: {} };
  s.alerted = s.alerted || {};
  const events = [];

  if (verdict.healthy) {
    s.healthy_streak = (s.healthy_streak || 0) + 1;
    if (s.state !== "HEALTHY" && s.healthy_streak >= c.recoverHysteresis) {
      // 회복 경보는 **경보를 실제로 보낸 에피소드에만** 보낸다. DEGRADED 에는 경보가 없으므로
      // (🟠=3분 지속, 🔴=30분 지속) 종전엔 3분 미만 깜빡임이 🟢 회복 알림만 단독 발신해
      // "경보도 없었는데 왜 회복 알림이 오지?" 가 됐다. 기록은 episodes 에 남는다.
      const notify = !!(s.alerted.suspect || s.alerted.confirm);
      closeEpisode(s, now, s.state, notify);
      events.push({ type: "RECOVERED", from: s.state, notify });
      s.state = "HEALTHY";
      s.since_ts = null;
      s.alerted = {};
    }
    return { state: s, events };
  }

  // unhealthy poll
  s.healthy_streak = 0;
  if (!s.since_ts) s.since_ts = now;
  const sustained = now - s.since_ts;

  if (s.state === "HEALTHY") {
    s.state = "DEGRADED";
    openEpisode(s, verdict, now);
  } else {
    bumpEpisode(s);
  }
  if (s.state === "DEGRADED" && sustained >= c.tSuspect) s.state = "SUSPECTED_DOWN";
  if (s.state === "SUSPECTED_DOWN" && sustained >= c.tWindow) s.state = "CONFIRMED_DOWN";

  if (s.state === "SUSPECTED_DOWN" && !s.alerted.suspect) {
    s.alerted.suspect = true;
    events.push({ type: "SUSPECTED", sustained, notify: true });
  }
  if (s.state === "CONFIRMED_DOWN" && !s.alerted.confirm) {
    s.alerted.confirm = true;
    events.push({ type: "CONFIRMED", sustained, notify: true });
  }
  return { state: s, events };
}

// ── Telegram ────────────────────────────────────────────────────────────────
async function telegram(env, text) {
  const token = env.TELEGRAM_BOT_TOKEN;
  const chat = env.TELEGRAM_CHAT_ID;
  if (!token || !chat) return;
  try {
    await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chat, text: text.slice(0, 4000), parse_mode: "Markdown", disable_web_page_preview: true }),
    });
  } catch (_) {
    /* best-effort */
  }
}

function alertText(ev, verdict, c) {
  const r = verdict.reasons.join(", ") || "n/a";
  if (ev.type === "SUSPECTED")
    return `🟠 *DR 감지* — colo *SUSPECTED_DOWN* (${Math.round(ev.sustained / 60)}분 지속, class=${verdict.klass})\n사유: ${r}\n→ ${c.tWindow / 60}분까지 지속되면 CONFIRMED. 대시보드/서버 점검 권장.`;
  if (ev.type === "CONFIRMED")
    return `🔴 *DR 감지* — colo *CONFIRMED_DOWN* (${Math.round(ev.sustained / 60)}분 지속, class=${verdict.klass})\n사유: ${r}\n→ **수동 failover 또는 GCP DR 트리거 검토**(사람승인 컷오버).`;
  if (ev.type === "RECOVERED") return `🟢 *DR 감지* — colo 회복(${ev.from} → HEALTHY).`;
  return `DR 감지 이벤트: ${ev.type}`;
}

// ── 메인 tick(매 분) ──────────────────────────────────────────────────────────
async function runTick(env) {
  const c = cfg(env);
  const secret = env.SCHEDULER_TICK_SECRET || "";
  const now = Math.floor(Date.now() / 1000);

  // 1) 스케줄러 tick (기존 동작 — 서버가 due 잡 enqueue + Healthchecks ping)
  if (secret) {
    await probe(`${c.origin}/api/v1/internal/scheduler/tick`, { method: "POST", headers: { "X-Scheduler-Secret": secret } }, c.probeTimeout);
  }

  // 2) 프로빙
  const live = await probeLiveWithRetry(c);
  const liveRes = live.res;
  const diagRes = await probe(`${c.origin}/api/v1/healthz/diag`, { method: "GET", headers: { "X-Scheduler-Secret": secret } }, c.probeTimeout);

  // 3) 채점
  const verdict = score(liveRes, diagRes, c);

  // 4) 상태기계(KV)
  let prev = null;
  try {
    const raw = await env.DR_STATE.get(KV_KEY);
    prev = raw ? JSON.parse(raw) : null;
  } catch (_) {
    prev = null;
  }
  const { state, events } = transition(prev, verdict, now, c);
  state.last = { ts: now, healthy: verdict.healthy, klass: verdict.klass, reasons: verdict.reasons, live_retried: live.retried };
  try {
    await env.DR_STATE.put(KV_KEY, JSON.stringify(state), { expirationTtl: 7 * 24 * 3600 });
  } catch (_) {
    /* KV 장애는 무시 */
  }

  // 5) 경보 + (WAL broken 은 별도 경보전용)
  for (const ev of events) {
    // notify=false → 경보 없이 지나간 깜빡임의 회복. 알리지 않고 episodes 기록만 남긴다.
    if (ev.notify === false) continue;
    await telegram(env, alertText(ev, verdict, c));
  }
  if (verdict.walBroken && (!prev || !prev.walAlerted)) {
    state.walAlerted = true;
    await telegram(env, "🟡 *DR 감지* — colo WAL 아카이빙 깨짐(경보전용, failover 트리거 아님). 백업 상태 점검 필요.");
    try { await env.DR_STATE.put(KV_KEY, JSON.stringify(state), { expirationTtl: 7 * 24 * 3600 }); } catch (_) {}
  } else if (!verdict.walBroken && prev && prev.walAlerted) {
    state.walAlerted = false;
    try { await env.DR_STATE.put(KV_KEY, JSON.stringify(state), { expirationTtl: 7 * 24 * 3600 }); } catch (_) {}
  }

  return { state: state.state, verdict };
}

export default {
  // Cron 트리거(매 분)
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runTick(env));
  },
  // 수동 디버그: GET ?debug=1 로 현재 KV 상태 확인(시크릿 헤더 필요)
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.searchParams.get("debug") === "1") {
      if ((request.headers.get("X-Scheduler-Secret") || "") !== (env.SCHEDULER_TICK_SECRET || "")) {
        return new Response("forbidden", { status: 403 });
      }
      const raw = (await env.DR_STATE.get(KV_KEY)) || "null";
      return new Response(raw, { headers: { "Content-Type": "application/json" } });
    }
    // 수동 텔레그램 테스트: GET ?test=alert (시크릿 헤더 필요) — 워커의 실제 telegram() + 시크릿으로
    // 샘플 SUSPECTED 경보를 발신. 상태기계/KV 와 무관, 실제 장애 유발 없음.
    if (url.searchParams.get("test") === "alert") {
      if ((request.headers.get("X-Scheduler-Secret") || "") !== (env.SCHEDULER_TICK_SECRET || "")) {
        return new Response("forbidden", { status: 403 });
      }
      const c = cfg(env);
      await telegram(
        env,
        alertText(
          { type: "SUSPECTED", sustained: 180 },
          { reasons: ["TEST — 실제 장애 아님(수동 테스트)"], klass: "TEST" },
          c
        )
      );
      return new Response("test alert sent — check Telegram", { status: 200 });
    }
    return new Response("turnflow-dr-detector (cron worker)", { status: 200 });
  },
};
