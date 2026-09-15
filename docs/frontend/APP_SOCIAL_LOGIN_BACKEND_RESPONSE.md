# 앱(Capacitor) 카카오·인스타 로그인 — 백엔드 회신 (2026-09-15)

프론트 요청서 「앱에서 카카오·인스타그램 로그인이 안 됩니다 — 확인·요청 4건」에 대한 회신.
운영 커밋 **`7b4e114`** 배포 완료 · 에러 로그 0건.

| # | 요청 | 상태 |
|---|---|---|
| ① | 인스타 start 에 「앱에서 시작」 표시 | ✅ **배포 완료** — `?client=app` → state `app_` 접두어 |
| ② | dev-api 가 `app.turnflow.link` 콜백 허용하는지 | ✅ **이미 허용됨** (추가 작업 없음) |
| ③ | 카카오 콘솔 Redirect URI 목록 확인 | ✅ **그대로** (5개, 변경 없음) |
| ④ | 인스타 state 유효 시간 | ✅ **10분** · 1회용 |

프론트가 정리하신 방식(브라우저에서 OAuth → 웹 콜백 → 커스텀 스킴으로 앱 복귀)에
**동의합니다.** 웹뷰 안에서 끝내는 방식은 말씀대로 iOS(`capacitor://localhost`)를
Meta·카카오가 받지 않아 불가합니다.

---

## ① 인스타 start — `client=app` (제안하신 (가)안)

```
GET /api/v1/auth/instagram/start/?client=app
  → { "state": "app_Y8VJvxOlsKyy…", "authorize_url": "…&state=app_Y8VJvxOlsKyy…" }

GET /api/v1/auth/instagram/start/            (웹, 기존 그대로)
  → { "state": "web_7LdKpignUqMq…", … }
```

웹 콜백 페이지는 **`state.startsWith("app_")`** 만 보시면 됩니다. 왕복 없습니다.

**교환은 계약 변경이 없습니다** — `POST /api/v1/auth/instagram/` 에 접두어 붙은 state 를
그대로 보내면 됩니다. 요청 origin 이 앱(`https://localhost` / `capacitor://localhost`)이어도
무관합니다(state 는 서버 저장값으로 검증하고 `redirect_uri` 도 저장값을 씁니다).

### ⚠️ 한 가지 바뀐 점 — 웹 state 에도 `web_` 접두어가 붙습니다

앱에만 접두어를 붙이면 `secrets.token_urlsafe` 난수가 **우연히 `app_` 로 시작**할 수
있습니다(알파벳이 `[A-Za-z0-9_-]` 라 약 **1/1,670만**). 그러면 **웹 로그인이 앱으로
튕깁니다** — 재현이 거의 불가능한 유령 버그가 됩니다. 양쪽에 붙이면 구조적으로 없어집니다.

**프론트 판정식(`startsWith("app_")`)은 그대로 쓰시면 됩니다.** 다만 웹 콜백에서
"접두어 없는 state = 웹" 으로 가정하신 코드가 있다면 `web_` 도 웹으로 보도록 해 주세요.

기타: `client` 는 **대소문자 무시**(`APP`·`App` 모두 인정), 모르는 값은 조용히 웹으로
봅니다 — 오타 하나로 앱 복귀가 깨지는 것이 400 보다 나쁩니다.

### 운영 실측 (2026-09-15, 배포 후)

```bash
curl -H "Origin: https://localhost" \
  "https://turnflow-api.clfy.ai.kr/api/v1/auth/instagram/start/?client=app"
# → state: app_Y8VJvxOlsKyy…  (authorize_url 에도 동일하게 실림)

curl "https://turnflow-api.clfy.ai.kr/api/v1/auth/instagram/start/"
# → state: web_7LdKpignUqMq…

curl -H "Origin: capacitor://localhost" ".../auth/instagram/start/?client=app"
# → 200 · access-control-allow-origin: capacitor://localhost
```

---

## ② dev-api 의 `app.turnflow.link` 허용 — 이미 됩니다

확인하신 대로 운영은 `turnflow.link` 만 200 이고 `app.turnflow.link` 는 400 입니다
(= `app.turnflow.link` 는 dev 빌드라 의도대로입니다). **dev-api 는 둘 다 200 입니다.**

```bash
# dev-api
?redirect_uri=https://app.turnflow.link/auth/instagram/callback        → 200 ✅
?redirect_uri=https://turnflowlink-dev.pages.dev/auth/instagram/callback → 200 ✅

# 운영 API
?redirect_uri=https://app.turnflow.link/auth/instagram/callback        → 400 (의도)
?redirect_uri=https://turnflow.link/auth/instagram/callback            → 200 ✅
```

Meta **dev 앱**의 OAuth 리디렉션 URI 에도 `app.turnflow.link/auth/instagram/callback` 이
등록돼 있습니다(2026-09-14 추가). 스테이징 채널에서 바로 시험 가능합니다.

---

## ③ 카카오 콘솔 Redirect URI — 그대로입니다 (조회만, 변경 없음)

앱 1573264 · REST API 키 기준 **5개**:

```
https://app.turnflow.link/auth/kakao/callback
https://turnflow.link/auth/kakao/callback      ← 앱이 쓸 주소
http://localhost:3000/auth/kakao/callback
http://localhost:5173/auth/kakao/callback
http://localhost:3006/auth/kakao/callback      ← 워크트리 dev (2026-09-12 추가)
```

말씀대로 **추가 등록은 없습니다.** `https://localhost/…` 도 넣지 않았습니다.

> 참고: 콘솔에서 이 목록이 있는 위치가 바뀌었습니다 —
> **앱 설정 > 앱 > 플랫폼 키 > REST API 키 카드 > `로그인 리다이렉트 URI`**
> ("카카오 로그인 > 고급" 에 있는 것은 **로그아웃** 리다이렉트로 다른 값입니다.)

---

## ④ 인스타 state 유효 시간 — **10분**, 1회용

`STATE_TTL_MINUTES = 10`. 브라우저 ↔️ 앱 왕복을 감안해도 충분합니다.

⚠️ **1회용**입니다(`consumed_at`). 콜백 화면에서 새로고침하거나 같은 state 로 두 번
교환하면 **400 `INSTAGRAM_STATE_USED`** 입니다. 앱 복귀 과정에서 `appUrlOpen` 이 중복
발화하면 두 번째가 실패하니, **교환은 한 번만** 부르도록 가드를 걸어 주세요.

관련 오류 코드:

| `code` | 뜻 |
|---|---|
| `INSTAGRAM_STATE_USED` | 이미 교환된 state (중복 호출·새로고침) |
| `INSTAGRAM_STATE_EXPIRED` | 10분 초과 |
| `INSTAGRAM_STATE_INVALID` | 없는 state |

셋 다 "다시 시도" 버튼은 **`start` 부터** 다시 밟게 해 주세요.

---

## 카카오는 백엔드 변경이 없습니다

요청서대로입니다. `redirect_uri` 는 서버가 따로 검증하지 않고 카카오로 그대로 넘깁니다
(카카오가 콘솔 등록값과 대조합니다). state 도 프론트가 만들어 쓰므로 「앱 표시」를 직접
넣으시면 됩니다.

실측하신 ③(`https://localhost/…` 로 보내도 `KAKAO_CODE_INVALID`)은 **코드가 가짜라 거기서
끝난 것**입니다. 실제 인가 코드로는 카카오가 "authorize 때 쓴 redirect_uri 와 같은가" 를
보므로, 교환 때도 **authorize 에 쓴 값(`https://turnflow.link/auth/kakao/callback`)을
그대로** 실어 주세요.

---

## 확인 순서 제안

1. 앱에서 인스타 버튼 → `start?client=app` → 브라우저 → 승인
2. `turnflow.link/auth/instagram/callback` 도착 → `state.startsWith("app_")` → 커스텀 스킴으로 복귀
3. 앱에서 `POST /auth/instagram/` (code·state) → 로그인 완료
4. 카카오도 같은 흐름(단, state 는 프론트가 만든 것)

막히는 지점이 있으면 **어느 단계에서 어떤 `code` 가 왔는지**만 주시면 바로 봅니다.

---

## 배포 상태

| | |
|---|---|
| 운영 커밋 | `7b4e114` (전 컨테이너 동일 이미지, 스큐 없음) |
| 에러 로그 | web 3종 + celery 0건 |
| 인스타 로그인 | ✅ 운영 ON (`INSTAGRAM_LOGIN_ENABLED=True`, 2026-09-14) |
| 카카오 로그인 | ✅ 운영 설정 완료 |
| 테스트 | auth/billing/analytics/emails **601 passed** |
