# [백엔드 회신] iOS IG 연동 — `return_to` 추가 + postMessage 계약 정정

회신 2026-08-04 · 요청서 `backend-ig-oauth-ios-app-handoff.md` 에 대한 답. **요청 2·3 은 구현 완료**,
**요청 1 은 하면 안 되는 이유**를 아래에 적었습니다. 프론트에서 이미 배포한 완화 5건은 모두 적절했고,
특히 "타임아웃이 없어 이후 모든 연동 버튼이 무반응" 은 실제로 심각한 버그였습니다 — 잘 잡으셨습니다.

---

## 0. 먼저 — 요청서의 전제 하나가 우리 문서 탓에 틀렸습니다 (죄송합니다)

> 현재 값: `https://www.facebook.com/v24.0/dialog/oauth?...` (connect/start 응답)

**실제 응답은 그게 아닙니다.** prod 실측:

```
https://www.instagram.com/oauth/authorize?client_id=...&scope=instagram_business_basic%2C...
```

`facebook.com/v24.0/dialog/oauth` 는 **우리 Swagger 문서의 "응답 예시"가 낡아서** 그렇게 적혀 있던
것입니다(코드에는 그런 URL 을 만드는 곳이 없습니다). 권한 목록(`pages_show_list`, `instagram_basic` …)도
Facebook Login 시절 값이 그대로 남아 있었습니다. **문서를 전부 정정했습니다** — 다시 열어 보시면
실제 값이 나옵니다. 잘못된 문서를 읽고 조사하시게 만든 건 저희 잘못입니다.

---

## 1. ❌ 요청 1 (authorize URL 호스트 변경) — 불가능합니다

증상 보고는 정확합니다. **원인 호스트만 다릅니다.** 그리고 제안된 방법들은 적용할 수 없습니다:

| 제안 | 판단 |
|---|---|
| `www.facebook.com` → `web.facebook.com` / `m.facebook.com` | ❌ **OAuth 가 깨집니다.** 우리는 **Instagram Business Login** 을 씁니다 — `client_id` 가 Instagram 앱 ID 이고 토큰 교환이 `api.instagram.com/oauth/access_token` 입니다. Facebook 대화상자가 발급한 code 는 이 엔드포인트에서 교환되지 않습니다 |
| `display=popup` | ❌ Facebook Login 대화상자 파라미터입니다. Instagram Business Login 에는 문서화돼 있지 않아 무시될 가능성이 큽니다 |
| 옵션 파라미터로 위 형태 생성 | ❌ 위가 성립하지 않으므로 무의미 |

Meta 가 문서화한 Instagram Business Login authorize 호스트는 **`www.instagram.com` 하나뿐**입니다
(`m.instagram.com` 같은 대체 호스트는 없습니다).

**그런데 근본 문제는 그대로 실재합니다.** `instagram.com` 역시 iOS 에서 Instagram 앱이 유니버설
링크로 claim 하며, 오히려 facebook.com 보다 더 적극적입니다. 즉 **호스트를 바꿔서는 못 풉니다.**
→ 요청 2 가 유일한 해법이라는 판단에 동의합니다.

---

## 2. ✅ 요청 2 (`return_to`) — 구현 완료

### 요청

```
POST /connect/start/   body: { "return_to": "https://app.turnflow.link/settings?ig=done" }
```

### 스펙

```http
POST /api/v1/integrations/instagram/workspaces/{workspace_id}/connect/start/
Authorization: Bearer <token>
Content-Type: application/json

{ "return_to": "https://turnflow.link/settings?ig=done" }
```

```json
200 OK
{
  "authorization_url": "https://www.instagram.com/oauth/authorize?client_id=...",
  "state": "abc123...",
  "mode": "production"
}
```

그 다음은 **같은 탭으로 이동**하시면 됩니다:

```js
const res = await fetch(startUrl, {
  method: 'POST',
  headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ return_to: 'https://turnflow.link/settings?ig=done' }),
});
const { authorization_url } = await res.json();
window.location.assign(authorization_url);   // 팝업 없음
```

### 콜백이 돌려보내는 형태

```
302 Location: https://turnflow.link/settings?ig=done&ig_result=connected
302 Location: https://turnflow.link/settings?ig=done&ig_result=failed&reason=PLAN_LIMIT_EXCEEDED
```

- **경로·기존 쿼리는 보존**됩니다(`ig=done` 그대로).
- `ig_result` 는 `connected` | `failed`.
- `reason` 은 실패일 때만 붙고, **팝업 방식의 `errorCode` 와 동일한 어휘**입니다:

| `reason` | 의미 |
|---|---|
| `OAUTH_AUTHORIZATION_FAILED` | 사용자가 권한 승인 취소 / Instagram 이 error 반환 |
| `INSTAGRAM_API_ERROR` | 토큰 교환·계정 조회 실패 |
| `PLAN_LIMIT_EXCEEDED` | 요금제 IG 계정 수 초과 |
| `ALREADY_CONNECTED_ELSEWHERE` | 이 IG 계정이 이미 다른 워크스페이스에 연결됨 |
| `INTERNAL_ERROR` | 서버 오류 |

- 성공/실패 모두 **연동 상태는 서버가 이미 확정**한 뒤 돌려보냅니다 → 복귀 후 `ig_result` 를 믿고
  바로 화면을 갱신하시면 되고, 확인이 필요하면 기존 연결 목록 API 를 한 번 더 부르시면 됩니다.

### ⚠️ `return_to` 는 허용목록 완전일치입니다 (오픈 리다이렉트 방어)

요청서에서 먼저 지적해 주신 부분 그대로 구현했습니다. **origin(scheme+host+port) 완전일치**만
통과합니다 — `startswith` 비교였다면 `https://turnflow.link.evil.com` 이 뚫리므로 그렇게 하지 않았습니다.

거부되면:

```json
400 Bad Request
{
  "success": false,
  "error": {
    "code": 400,
    "message": "return_to 가 허용되지 않은 주소입니다. 허용된 프론트 origin 과 정확히 일치해야 합니다.",
    "details": {
      "code": "INVALID_RETURN_TO",
      "reason": "origin_not_allowed",
      "allowed_origins": ["https://turnflow.link", "..."]
    }
  }
}
```

`details.allowed_origins` 에 **현재 허용 목록이 그대로 담겨** 나오니, 개발 중 무엇이 허용되는지
바로 확인하실 수 있습니다. `reason` 값: `origin_not_allowed` / `scheme_not_allowed` /
`userinfo_not_allowed` / `illegal_characters` / `too_long` / `unparsable` / `empty`.

거부되는 예 (전부 테스트로 고정해 뒀습니다):
`https://turnflow.link.evil.com/x` · `https://sub.turnflow.link/x`(서브도메인은 별개 origin) ·
`http://turnflow.link/x`(scheme 다운그레이드) · `https://turnflow.link:8443/x`(포트 불일치) ·
`javascript:` · `data:` · `//evil.com/x` · `https://evil.com@turnflow.link/x`

### 🔴 확인 부탁 — `app.turnflow.link` 를 prod 로 쓰실 건가요?

요청서 예시가 `https://app.turnflow.link/...` 인데, 현재 상태는 이렇습니다:

| 항목 | 현재 |
|---|---|
| `app.turnflow.link` 배포 번들의 API 호스트 | **`https://dev-api.turnflow.link`** (dev 를 향함) |
| prod `CORS_ALLOWED_ORIGINS` | `turnflow.link`, `turnflow.clfy.ai.kr`, `link.turnflow.clfy.ai.kr`, `admin.turnflow.link` — **`app.turnflow.link` 없음** |
| `app.turnflow.link` 오리진의 prod API 프리플라이트 | `access-control-allow-origin` 헤더 **없음 = 브라우저 호출 차단** |

즉 `app.turnflow.link` 는 지금 **dev 를 향한 별도 배포**입니다. 이걸 prod 프론트로 쓰실 계획이면
저희 쪽에서 **CORS 허용목록 추가**가 필요하고(그러면 `return_to` 도 자동 허용됩니다),
그쪽 번들의 API 호스트도 prod 로 바꾸셔야 합니다.

**prod 프론트 origin 을 확정해서 알려주세요.** 알려주시면 즉시 허용목록에 넣겠습니다.
현재 허용목록은 위 CORS 목록과 동일하며(별도 지정이 없으면 CORS 목록을 그대로 따릅니다),
`https://turnflow.link` 는 지금 바로 쓰실 수 있습니다.

---

## 3. ✅ 요청 3 (postMessage) — 이미 보내고 있었습니다. 계약이 안 맞았습니다

이게 이번 검토에서 가장 중요한 발견입니다. 백엔드는 **모든 분기에서 이미 `postMessage` 를
보내고 있었습니다.** 그런데 서로 못 만났습니다:

| | 기존 |
|---|---|
| 백엔드가 보낸 것 | `{ type: 'INSTAGRAM_CONNECTED', success: true, connection: {...} }` |
| `targetOrigin` | `'*'` |
| 프론트가 듣는 조건 | `{ source: 'ig-connect' }` + **자기 origin 에서 온 것만** 신뢰 |

**두 가지 이유로 한 번도 전달되지 않았습니다** — ① `source` 필드가 없었고 ② 콜백 페이지는
API 오리진에서 서빙되므로 `event.origin` 이 프론트 자기 origin 이 아닙니다. 즉 요청서의
"성공/실패 신호도 없으니 플로우가 끝나지 않습니다" 에는 **iOS 와 무관한 두 번째 원인**이 있었습니다.

### 고친 내용

```js
// 성공
{ source: 'ig-connect', type: 'INSTAGRAM_CONNECTED', success: true,  connection: { ... } }
// 실패
{ source: 'ig-connect', type: 'INSTAGRAM_ERROR',     success: false, errorCode: '...', message: '...' }
```

- **`source: 'ig-connect'` 를 추가**했습니다 — 물어보신 그대로, 기존 필드는 그대로 두었으니
  `type` 으로 듣던 코드도 계속 동작합니다.
- **`targetOrigin: '*'` 을 없앴습니다.** 이건 그 자체로 보안 결함이었습니다(어떤 opener 에게든
  `connection` 페이로드가 브로드캐스트됨). 이제 허용목록 origin 에만 순차로 보냅니다 —
  브라우저는 실제 opener origin 과 일치할 때만 전달하므로 정상 케이스는 전부 커버되고 유출은 없습니다.

### 프론트에서 한 줄 바꿔주실 것 — 어느 origin 인지

물어보신 답입니다. 콜백 페이지는 **API 오리진**에서 서빙됩니다:

```js
window.addEventListener('message', (e) => {
  if (e.origin !== 'https://turnflow-api.clfy.ai.kr') return;   // ← 이 값
  if (e.data?.source !== 'ig-connect') return;
  if (e.data.success) { /* e.data.connection */ } else { /* e.data.errorCode */ }
});
```

`errorCode` 값은 §2 의 `reason` 표와 동일한 7종입니다(+ `MISSING_PARAMETERS`, `INVALID_STATE`).

---

## 4. 권장 사용 방식

| 환경 | 방식 |
|---|---|
| **모바일(iOS/Android)** | `return_to` + `window.location.assign` — 팝업·유니버설 링크·팝업차단 전부 회피 |
| **데스크탑** | 기존 팝업 + `postMessage`(이제 실제로 도착합니다). `return_to` 를 써도 무방합니다 |

`return_to` 를 **생략하면 동작이 이전과 완전히 동일**합니다(opt-in) — 데스크탑 경로를 건드리지
않았으니 단계적으로 적용하셔도 됩니다.

---

## 5. 배포 상태

| 항목 | 상태 |
|---|---|
| 코드 | ✅ 완료 (마이그레이션 `integrations.0047` 1건 — 순수 컬럼 추가, 되돌리기 안전) |
| 테스트 | ✅ 신규 66개 + 기존 OAuth 테스트 무회귀 (합 115개 통과) |
| dev 서버 | ✅ 적용됨 — 지금 `dev-api.turnflow.link` 로 검증 가능합니다 |
| prod | ⏳ **미배포** — 확인 후 배포합니다 |

**dev 에서 먼저 테스트해 보시고**, 실기기(iOS)에서 같은-탭 플로우가 의도대로 도는지 확인되면
prod 에 올리겠습니다. 실기기 검증이 필요하다고 요청서에 쓰신 부분에 동의합니다 —
이 경로는 시뮬레이터로는 유니버설 링크 동작이 재현되지 않습니다.

막히는 부분 있으면 알려주세요.
