/**
 * TurnFlow DR — (선택) 점검페이지 Worker.
 *
 * 라우트 `api.turnflow.clfy.ai.kr/*` 에 바인딩하면, 원본(colo)이 5xx/도달불가일 때 엣지에서
 * 점검페이지(503 + Retry-After)를 서빙한다. 원본이 정상이면 **투명 패스스루**.
 * → 은퇴한 office 점검 Caddy 를 대체. colo 다운 ~ GCP DNS 스왑 사이 사용자가 raw 에러 대신 점검페이지를 봄.
 *
 * ⚠️ 주의: 이 워커를 라우트에 바인딩하면 **모든 prod 요청이 워커를 통과**한다(서브-ms지만 경로 변경).
 *   - 감지기(detector-worker.js)는 out-of-band(cron)라 prod 경로 영향 0 → 먼저 배포 권장.
 *   - 이 점검 워커는 컷오버 기계(Phase B)와 함께 켜는 걸 권장(Phase A 단독 경보 단계에선 선택).
 *   - 헬스/내부/웹훅 경로는 패스스루 예외로 두어 LB/모니터/IG 웹훅에 영향 없게 한다.
 */

const MAINT_HTML = `<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>서비스 점검 중</title>
<style>body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e6e8eb;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.box{text-align:center;max-width:420px;padding:32px}h1{font-size:20px;margin:0 0 12px}
p{color:#9aa0a6;line-height:1.6;margin:0}</style></head>
<body><div class="box"><h1>🛠️ 서비스 점검 중</h1>
<p>일시적인 점검으로 잠시 접속이 원활하지 않습니다.<br>잠시 후 자동으로 복구됩니다. 데이터는 안전합니다.</p>
</div></body></html>`;

// 점검페이지로 가리면 안 되는 경로(엣지에서 가로채지 않고 항상 원본으로) — 헬스/내부/웹훅.
const PASSTHROUGH_PREFIXES = ["/api/v1/healthz", "/api/v1/internal/", "/api/v1/integrations/webhook"];

function isPassthrough(pathname) {
  return PASSTHROUGH_PREFIXES.some((p) => pathname.startsWith(p));
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // 항상 원본으로 보내야 하는 경로는 그대로 프록시(가로채기 실패해도 원본 응답 반환).
    if (isPassthrough(url.pathname)) {
      return fetch(request);
    }

    try {
      const res = await fetch(request);
      // 원본이 5xx → 점검페이지(503). 그 외는 투명 패스스루.
      if (res.status >= 500) {
        return new Response(MAINT_HTML, {
          status: 503,
          headers: { "Content-Type": "text/html; charset=utf-8", "Retry-After": "120", "Cache-Control": "no-store" },
        });
      }
      return res;
    } catch (e) {
      // 원본 도달 불가(연결 거부/타임아웃) → 점검페이지.
      return new Response(MAINT_HTML, {
        status: 503,
        headers: { "Content-Type": "text/html; charset=utf-8", "Retry-After": "120", "Cache-Control": "no-store" },
      });
    }
  },
};
