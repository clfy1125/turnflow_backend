# 앱 인스타 로그인 「안 됨」 원인 진단 (백엔드 → 프론트, 2026-09-16)

**결론 먼저: 백엔드 4건은 정상 동작 중이고, 막힌 곳은 앱·웹 프론트 2곳입니다.**
운영 로그·DB·배포된 번들을 실측했습니다. 아래 증거만 보시면 바로 재현됩니다.

---

## 0. 먼저 — 인스타 로그인은 이미 되고 있습니다 (웹)

운영에서 **5명이 인스타로 가입 완료**했습니다. 그중 2명은 이메일 등록까지 마쳤습니다.

```
instagram_user_id 보유 계정 : 5
자리표시(@ig.invalid) 계정  : 3   ← 나머지 2명은 실제 이메일 등록 완료
소비된 state(로그인 성공)   : 5   ← 전부 웹
```

즉 **서버·Meta 설정·정책은 살아 있습니다.** 앱만 못 들어오고 있습니다.

---

## 1. 앱은 3번 시도해서 0번 성공했습니다

state 테이블 실측(운영):

```
APP state(app_ 접두어) 발급 : 3건
그중 교환까지 간 것          : 0건
```

세 번이 두 갈래로 갈립니다.

### (A) 앱이 아직 `https://localhost` 를 redirect_uri 로 보냅니다 — 2건

운영 액세스 로그 (2026-09-15 22:02:37, 22:32:22 · SM-S926N / Android 16 / Capacitor webview):

```
GET /api/v1/auth/instagram/start/
      ?redirect_uri=https%3A%2F%2Flocalhost%2Fauth%2Finstagram%2Fcallback
      &client=app
→ 400 INSTAGRAM_INVALID_REDIRECT_URI
         ^^^^^^^^^^^^^^^^^^^^^ 여기가 원인
```

**`client=app` 은 붙었는데 `redirect_uri` 는 여전히 `window.location.origin`(= `https://localhost`)
으로 계산되는 경로가 남아 있습니다.** 확정안대로라면 이 값은
`https://turnflow.link/auth/instagram/callback` **고정**이어야 합니다.

> 이 400 은 **의도된 거부**입니다. 앱 origin(`https://localhost`·`capacitor://localhost`)은
> Meta 가 리디렉션 URI 로 받아 주지 않으므로(HTTPS 공개 주소만), 서버가 먼저 막습니다.
> 여기서 통과시키면 인스타 authorize 단계에서 더 알아보기 어려운 오류로 죽습니다.

### (B) 제대로 보낸 1건도 교환까지 오지 못했습니다 — 1건

2026-09-15 21:56:46, 같은 기기:

```
GET /start/?redirect_uri=https%3A%2F%2Fturnflow.link%2Fauth%2Finstagram%2Fcallback&client=app
→ 200  (state = app_… 정상 발급)
```

그런데 **그 state 는 끝내 소비되지 않았습니다**(`consumed_at = null`).
`POST /api/v1/auth/instagram/` 요청은 **운영에 단 한 건도 들어온 적이 없습니다.**

→ 브라우저에서 앱으로 **되돌아오는 단계**에서 끊긴 것입니다.

---

## 2. 왜 못 돌아오는가 — 웹 번들에 그 코드가 없습니다

운영 프론트 번들(`https://turnflow.link` → `/assets/index-GRORrrj7.js`)을 받아 확인했습니다:

| 찾은 문자열 | 결과 |
|---|---|
| `link.turnflow.app` (커스텀 스킴) | **0회** |
| `appUrlOpen` | **0회** |
| `client=app` | **0회** |
| `capacitor` | 2회 (무관한 용도로 보임) |
| `/.well-known/assetlinks.json` | **HTTP 404** |

`client=app` 이 **웹 번들에는 없는데 서버 로그에는 찍혔습니다.** 즉 그 코드는
**앱 번들(APK) 안에만** 있고, **웹(`turnflow.link`)은 예전 번들 그대로**입니다.

웹 콜백 페이지가 「`state.startsWith("app_")` 이면 커스텀 스킴으로 앱에 되돌린다」를
해 줘야 하는데, 그 페이지가 아직 배포되지 않았습니다.

---

## 3. 단계별 현재 상태

| 단계 | 담당 | 상태 |
|---|---|---|
| 1. 앱 → `start?client=app` | 앱 | ⚠️ 절반 — `redirect_uri` 가 localhost 인 경로 잔존 |
| 2. 브라우저 → 인스타 승인 | — | ✅ |
| 3. `turnflow.link/auth/instagram/callback` 도착 | — | ✅ 도착함 |
| 4. **웹 페이지 → 커스텀 스킴으로 앱 복귀** | **웹** | ❌ **코드가 운영에 없음** |
| 5. 앱 → `POST /auth/instagram/` 교환 | 앱 | ❌ 4 가 안 돼 도달 못 함 |

---

## 4. 프론트가 할 일 (3가지)

1. **`redirect_uri` 고정** — 앱에서 `start` 를 부를 때
   `https://turnflow.link/auth/instagram/callback` 으로 **하드코딩**.
   `window.location.origin` 을 쓰는 경로를 없애 주세요. (현재 2/3 요청이 이것 때문에 400)

2. **웹 번들 배포** — `turnflow.link` 의 콜백 페이지에
   `state.startsWith("app_")` → 커스텀 스킴 되돌리기 로직을 올려 주세요.
   지금 운영 번들에는 없습니다. **이게 (B)의 유일한 원인입니다.**

3. **App Links 를 쓸 계획이면** `/.well-known/assetlinks.json` 이 404 입니다.
   커스텀 스킴(`link.turnflow.app://`)만 쓰실 거면 무시하셔도 됩니다.

---

## 5. 백엔드 쪽은 손댈 것이 없습니다 (재확인)

| # | 회신서 항목 | 실측 |
|---|---|---|
| ① | `?client=app` → `app_` 접두어 | ✅ 로그의 APP state 3건이 그 증거 |
| ② | dev-api 가 `app.turnflow.link` 허용 | ✅ 200 |
| ③ | 카카오 콘솔 Redirect URI 5개 | ✅ 변경 없음, `turnflow.link` 포함 |
| ④ | state TTL 10분·1회용 | ✅ |

운영 커밋 `7b4e114` · 인스타 로그인 ON · 에러 로그 0건.

---

## 6. 재현·확인 방법

고치신 뒤 앱에서 한 번 눌러 보시고 알려 주세요. 저희 쪽에서 **state 테이블로 어디까지
왔는지 한 번에 보입니다**:

- `start` 가 안 왔다 → 앱이 호출을 못 함
- APP state 는 생겼는데 `consumed_at` 이 비어 있다 → **브라우저→앱 복귀 실패** (4단계)
- `consumed_at` 이 찍혔다 → 로그인 성공

막히면 **시각(분 단위)과 기기**만 주시면 그 한 건을 추적해 드립니다.

---

## 참고 — 카카오도 같은 구조입니다

카카오는 state 를 프론트가 만들기 때문에 백엔드 변경이 없었지만,
**4단계(웹 콜백 → 앱 복귀)는 인스타와 똑같이 필요합니다.** 지금 웹 번들에 그 코드가
없으므로 카카오도 앱에서는 같은 지점에서 멈춥니다. 2번을 고치면 둘 다 풀립니다.
