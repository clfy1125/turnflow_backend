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
    origin: env.ORIGIN || "https://api.turnflow.clfy.ai.kr",
    expectedActiveSite: env.EXPECTED_ACTIVE_SITE || "colo",
    tSuspect: num(env.T_SUSPECT_SECONDS, 180), // DEGRADED→SUSPECTED (지속)
    tWindow: num(env.T_WINDOW_SECONDS, 1800), // SUSPECTED→CONFIRMED (30분 지속창)
    queueWarn: num(env.QUEUE_WARN, 5000),
    workerStale: num(env.WORKER_STALE_SECONDS, 600),
    deferredAge: num(env.DEFERRED_AGE_SECONDS, 3600),
    dbLatencyMax: num(env.DB_LATENCY_MS, 2000),
    probeTimeout: num(env.PROBE_TIMEOUT_MS, 5000),
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

// ── 상태기계 ────────────────────────────────────────────────────────────────
function transition(state, verdict, now, c) {
  const s = state || { state: "HEALTHY", since_ts: null, healthy_streak: 0, alerted: {} };
  s.alerted = s.alerted || {};
  const events = [];

  if (verdict.healthy) {
    s.healthy_streak = (s.healthy_streak || 0) + 1;
    if (s.state !== "HEALTHY" && s.healthy_streak >= c.recoverHysteresis) {
      events.push({ type: "RECOVERED", from: s.state });
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

  if (s.state === "HEALTHY") s.state = "DEGRADED";
  if (s.state === "DEGRADED" && sustained >= c.tSuspect) s.state = "SUSPECTED_DOWN";
  if (s.state === "SUSPECTED_DOWN" && sustained >= c.tWindow) s.state = "CONFIRMED_DOWN";

  if (s.state === "SUSPECTED_DOWN" && !s.alerted.suspect) {
    s.alerted.suspect = true;
    events.push({ type: "SUSPECTED", sustained });
  }
  if (s.state === "CONFIRMED_DOWN" && !s.alerted.confirm) {
    s.alerted.confirm = true;
    events.push({ type: "CONFIRMED", sustained });
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
  const liveRes = await probe(`${c.origin}/api/v1/healthz/live`, { method: "GET" }, c.probeTimeout);
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
  state.last = { ts: now, healthy: verdict.healthy, klass: verdict.klass, reasons: verdict.reasons };
  try {
    await env.DR_STATE.put(KV_KEY, JSON.stringify(state), { expirationTtl: 7 * 24 * 3600 });
  } catch (_) {
    /* KV 장애는 무시 */
  }

  // 5) 경보 + (WAL broken 은 별도 경보전용)
  for (const ev of events) await telegram(env, alertText(ev, verdict, c));
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
