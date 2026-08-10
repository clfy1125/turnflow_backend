# IG 연결 건강 진단 · 웹훅 재구독 — 프론트 연동 가이드

연결된 Instagram 계정의 **연결 상태를 사용자가 직접 점검·복구**하는 2개 엔드포인트.

## 0. 배경 — 왜 필요한가

캠페인이 "조용히 멈추는" 두 가지 원인이 있다:

1. **웹훅 auto-disable** — Meta 는 우리 콜백이 반복 실패하면(엣지 장애·DR 컷오버 등)
   해당 IG 계정의 웹훅 구독(comments, messages)을 **자동 해제**한다. 그러면 새 댓글이
   서버로 오지 않아 자동 DM 이 안 나간다. (서버측 beat 가 주기적으로 재구독하지만,
   사용자가 즉시 복구할 수단이 없었다.)
2. **토큰 만료** — long-lived 토큰은 약 60일 후 만료된다. 만료되면 발송/조회가 막힌다.

이 진단으로 사용자가 설정 화면에서 상태를 확인하고, 웹훅 문제는 버튼 한 번으로 복구한다.
토큰 문제는 재연동(OAuth)으로 유도한다.

---

## 1. 엔드포인트

| 메서드 | 경로 | 용도 | 스로틀(기본) |
|--------|------|------|------|
| GET | `/api/v1/integrations/instagram/connections/{ig_connection_id}/health/` | 종합 진단 | 사용자별 20/min |
| POST | `/api/v1/integrations/instagram/connections/{ig_connection_id}/resubscribe-webhooks/` | 웹훅 수동 재구독 | 사용자별 6/hour |

- 인증: `Authorization: Bearer <access_token>` 필수. 해당 연동이 속한 워크스페이스 멤버여야 함.
- 스로틀 초과 시 **429**. 재구독은 6/hour 로 넉넉하지 않으므로, 버튼을 연타로 막고
  429 면 "잠시 후 다시 시도" 안내.

---

## 2. GET health — 응답

```json
{
  "success": true,
  "data": {
    "connection": { "id": "uuid", "username": "myshop", "status": "active", "is_active": true },
    "token": {
      "valid": true,
      "expires_at": "2026-09-01T00:00:00Z",
      "is_expired": false,
      "expires_in_days": 47,
      "last_verified_at": "2026-07-16T09:00:00Z"
    },
    "webhook": { "subscribed": true, "fields": ["comments", "messages"], "missing_fields": [] },
    "healthy": true,
    "issues": [],
    "checked_at": "2026-07-16T09:00:00Z",
    "mode": "live"
  }
}
```

### 필드

| 필드 | 타입 | 설명 |
|------|------|------|
| `connection.status` | string | `active` / `expired` / `revoked` / `error` |
| `connection.is_active` | bool | 소프트 활성 플래그(요금제 슬롯). false 면 기능에서 제외 중 |
| `token.valid` | bool\|null | `true`=라이브 /me 통과, `false`=사망, `null`=판정불가(일시 통신/mock) |
| `token.is_expired` | bool | 만료일 경과 여부 |
| `token.expires_in_days` | int\|null | 만료까지 남은 일수(음수=이미 만료). D-day 뱃지에 활용 |
| `webhook.subscribed` | bool\|null | `true`=필수 필드 구독됨, `false`=미구독/누락, `null`=조회 실패 |
| `webhook.missing_fields` | string[] | 빠진 필수 필드(예: `["messages"]`) |
| `healthy` | bool | 모든 신호 정상이면 true (배지 색 결정) |
| `issues` | array | 감지된 문제 + 권장 액션 (아래 표) |
| `mode` | string | `live` / `mock` (개발환경) |

> **부작용 없음(report-only)**: 진단은 연동을 죽이지 않는다. 토큰이 살아있으면
> `last_verified_at` 만 갱신하고 status 는 그대로 둔다. Meta 통신 오류도 5xx 가 아니라
> **200 + `META_API_UNREACHABLE` issue** 로 응답한다 → 프론트는 항상 200 을 기대하면 된다.

---

## 3. issues 코드 → CTA 매핑

각 issue 는 `{ code, message(한국어), action }`. `action` 으로 버튼을 분기한다.

| code | 의미 | action | 프론트 CTA |
|------|------|--------|-----------|
| `TOKEN_INVALID` | 토큰 만료/회수(라이브 확인) | `reconnect` | "다시 연동하기" → OAuth 재연동 |
| `TOKEN_EXPIRED` | 만료일 지남 | `reconnect` | "다시 연동하기" |
| `TOKEN_UNVERIFIED` | 판정 불가(일시 통신) | `retry` | "다시 점검" |
| `WEBHOOK_NOT_SUBSCRIBED` | 웹훅 완전 미구독 | `resubscribe` | "실시간 수신 복구" → POST resubscribe |
| `WEBHOOK_FIELDS_MISSING` | 필수 필드 일부 누락 | `resubscribe` | "실시간 수신 복구" |
| `CONNECTION_REVOKED` | 연동 해제됨 | `reconnect` | "다시 연동하기" |
| `CONNECTION_ERROR` | 연동 오류 status | `reconnect` | "다시 연동하기" |
| `CONNECTION_INACTIVE` | 비활성(요금제 슬롯) | `activate` | "활성 계정으로 선택" → IG 계정 활성화 화면 |
| `META_API_UNREACHABLE` | Meta 통신 실패 | `retry` | "다시 점검" |

> `action=reconnect` 는 기존 계정 재인증이므로, `connect/start` 에
> `reconnect_connection_id` 를 실어 호출한다(§5, 아래 부록의 재연결 흐름).

---

## 4. healthy 배지 UI 가이드

- `healthy=true` → 초록 "정상" 배지.
- `healthy=false` → 노랑/빨강 배지 + `issues[0].message` 를 요약으로, 첫 issue 의 `action` 버튼 노출.
- 여러 issue 가 있으면 우선순위: reconnect > resubscribe > activate > retry.

---

## 5. POST resubscribe — 웹훅 재구독

```javascript
const res = await fetch(
  `/api/v1/integrations/instagram/connections/${connId}/resubscribe-webhooks/`,
  { method: 'POST', headers: { Authorization: `Bearer ${token}` } }
);
const body = await res.json();
if (body.success) applyHealth(body.data);  // 재구독 직후의 최신 헬스로 UI 갱신
```

### 응답 (성공, 200)

```json
{ "success": true, "resubscribed": true, "data": { /* GET health 와 동일한 헬스 페이로드 */ } }
```

- **재구독 후 헬스를 다시 계산해 `data` 로 함께 반환**한다 → 프론트는 추가 GET 없이
  `body.data` 로 배지·issues 를 즉시 갱신하면 된다.

### 에러

| 코드 | 상황 | 처리 |
|------|------|------|
| 409 | 연동 해제/토큰 없음/만료 (`error.action="reconnect"`) | 재구독 대신 재연동 유도 |
| 429 | 스로틀(6/hour) 초과 | "잠시 후 다시 시도" |
| 502 | Meta 재구독 호출 실패 | "일시 오류, 잠시 후 다시" |

---

## 6. mock 모드 (개발환경)

`INSTAGRAM_MOCK_MODE` 또는 mock 토큰이면 Meta 를 호출하지 않고 시뮬레이션 응답
(`mode: "mock"`, 웹훅 구독됨·토큰 valid)을 준다. 로컬에서 UI 개발 시 항상 healthy 로 보인다.

---

## 7. 폴링 지침

- **주기 폴링 금지.** 설정/연동 화면 **진입 시 1회** + 사용자의 **수동 새로고침**만.
- 헬스체크는 요청당 Meta 라이브 2콜이라 비용이 있다(스로틀 20/min).

---

## 부록 A. 재연결(재인증) 흐름 — `connect/start`

토큰 문제(`action=reconnect`)는 **기존 계정의 토큰만 재인증**하는 것이다. 기본 한도(1개)를
채운 사용자도 재인증은 가능해야 한다. 백엔드가 이를 보장하는 방식:

- **자동(프론트 추가 작업 불필요)**: owner 가 살아있는 연동을 1개 이상 보유하면
  `connect/start` 가 파라미터 없이도 200 을 준다. → 한도 찬 사용자의 "재연동"에 429 가
  뜨지 않는다. (기존 "재연결 제한" 임시 모달은 429 에서만 떴으므로 자연히 사라짐.)
- **명시(선택)**: `reconnect_connection_id` 를 실으면 의도를 확실히 표시(권장):
  ```javascript
  await fetch(`/api/v1/integrations/instagram/workspaces/${wsId}/connect/start/`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ reconnect_connection_id: connId }),
  });
  ```
  값이 이 워크스페이스 소속이 아니거나 이미 해제(revoked)된 연동이면 **400**.
- ⚠️ 한도 우회는 불가: OAuth 에서 **다른(신규) 계정**을 인증하면 콜백에서 플랜 게이트가
  `PLAN_LIMIT_EXCEEDED` 로 거절한다. start 는 더 이상 사전 429 를 주지 않으므로,
  "새 계정 추가"는 프론트가 구독 정보로 미리 업셀 UI 를 띄우는 걸 권장.

## 부록 B. `ALREADY_CONNECTED_ELSEWHERE` — 중복 연동 차단 (postMessage)

하나의 Instagram 계정은 **하나의 워크스페이스에만** 연결된다. 이미 다른 워크스페이스가
그 계정을 점유 중이면 OAuth 콜백 팝업이 아래 에러를 부모 창에 전달한다:

```javascript
// window.addEventListener('message', ...) 로 수신
{
  type: 'INSTAGRAM_ERROR',
  success: false,
  errorCode: 'ALREADY_CONNECTED_ELSEWHERE',
  message: '이 Instagram 계정은 이미 다른 워크스페이스(si***@clfy.ai.kr)에 연결되어 있습니다. 기존 연결을 해제한 후 다시 시도해 주세요.'
}
```

- 상대 계정 이메일은 **마스킹**되어 `message` 안에만 담긴다(별도 구조화 필드 없음).
- 전용 모달로 안내 — **업그레이드/구매 CTA 아님**. "기존 워크스페이스에서 연결 해제 후 다시 시도"만.
