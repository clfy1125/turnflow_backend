# `api.turnflow.clfy.ai.kr` DNS A 레코드 삭제 (2026-08-12)

옛 API 호스트 은퇴의 마지막 단계. 2026-08-04 에 API 서빙을 끊고 308 리다이렉트만 남겼고,
소비자가 전부 신 호스트로 이행된 것을 확인한 뒤 DNS 레코드를 삭제했다.

## 🔙 복구 레시피 (되돌려야 할 때 이대로 다시 추가)

| 항목 | 값 |
|---|---|
| 이름 | `api.turnflow` (FQDN `api.turnflow.clfy.ai.kr`) |
| 형식 | **A** |
| 콘텐츠 | `121.126.99.70` |
| 프록시 상태 | **DNS 전용** (회색 구름 — 프록시 켜면 TLS 핸드셰이크 실패, 아래 참고) |
| TTL | 자동 |
| 주석 | `턴플로우 백엔드 api` |

Caddy 쪽 은퇴 블록(`redir … 308` + `log retired_host`)은 그대로 두었으므로, 레코드만 다시 추가하면
즉시 원상 복구된다.

> **프록시를 켜지 말 것** — 2단계 서브도메인이라 Cloudflare Universal SSL(`*.clfy.ai.kr`)이 덮지 못한다.
> 프록시하면 엣지에서 TLS alert 40 (handshake_failure)로 죽는다(실측). 같은 zone 의
> `link.turnflow.clfy.ai.kr` 이 그 상태다. 덮으려면 Advanced Certificate Manager 가 필요하다.

## 삭제 근거 (2026-08-12 실측)

옛 호스트 전용 액세스 로거(`log retired_host`, 2026-08-04 설치) 기준.

### 소비자별 마지막 요청

| 출처 | 총 건수 | 마지막 요청 | 상태 |
|---|---:|---|---|
| `turnflowlink.pages.dev` (고객 앱 SSR) | 1,105 | **08-10 10:49:22** | ✅ 0 |
| `clfy1125.workers.dev` (어드민 콘솔) | 69 | 08-07 11:17:38 | ✅ 0 |
| 봇 · 직접 | 300+ | 계속 | 스캐너뿐 |

### 일자별

```
날짜     turnflowlink   clfy1125   봇/직접
08-08          95           0         3
08-09         180           0       140
08-10          54           0        13
08-11           0           0         5     ← 프론트 배포 반영
08-12           0           0         0
```

최근 24시간 5건은 전부 봇(`CMS-Checker` · `NetcraftSurveyAgent`)이 `GET /` 를 친 것이고 전부 308.
`/media` 0건 · `/api/v1/pages` 0건 — 프론트가 제시한 판정 기준을 양쪽 다 충족.

### 로거 생존 검증 (0건이 고장 때문이 아님을 증명)

"0건" 을 믿기 전에 검출기가 살아 있는지 먼저 확인했다:

```bash
curl -s -o /dev/null "https://api.turnflow.clfy.ai.kr/logger-alive-probe-151910"
# → 308
# 로그: 08-12 06:19:10 | host=api.turnflow.clfy.ai.kr | status=308  ← 즉시 기록됨
```

### 안전 점검

| 점검 | 결과 |
|---|---|
| `INSTAGRAM_REDIRECT_URI` | `https://turnflow-api.clfy.ai.kr/...` — **신 호스트** ✅ |
| 옛 호스트로 온 OAuth 콜백 | 08-08 07:27 **1건** — 아래 참고 |
| 옛 호스트로 온 웹훅 | 0건 |
| `CORS_ALLOWED_ORIGINS` | 옛 호스트 0개 |
| `CSRF_TRUSTED_ORIGINS` / `ALLOWED_HOSTS` | 각 1개 잔존 (무해, 정리 대상) |
| 오리진 IP 노출 | 이 레코드가 **유일한 DNS 전용(비프록시)** 레코드였다 |

**08-08 OAuth 콜백 1건** — Mac Safari · 한국 IP · `code`+`state` 보유라 실사용자로 보였으나,
그 시각 IG 연동 생성·갱신이 **0건**이고 Referer 도 없었다. 브라우저 방문기록에서 옛 콜백 URL 을
다시 연 것(`code` 는 이미 소모됨)으로 판정. 서버가 만드는 모든 신규 플로우는
`INSTAGRAM_REDIRECT_URI`(신 호스트) 하나만 쓰므로 옛 호스트로 갈 경로가 없다.

## 남은 정리 (급하지 않음)

- [ ] Caddy `api.turnflow.clfy.ai.kr` 은퇴 블록 + `log retired_host` 제거 → 복구 가능성이 사라진 뒤
- [ ] `ALLOWED_HOSTS` · `CSRF_TRUSTED_ORIGINS` 의 옛 호스트 항목 제거
- [ ] Meta 앱 "Valid OAuth Redirect URIs" 에 남은 옛 콜백 URL 제거

관련: [RETIRE_OLD_API_HOST_ROUND2.md](../frontend/RETIRE_OLD_API_HOST_ROUND2.md) ·
[PROD_HARDENING_2026-08-04.md](PROD_HARDENING_2026-08-04.md)
