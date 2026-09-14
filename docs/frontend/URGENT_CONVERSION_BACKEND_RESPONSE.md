# 긴급 전환 개선 — 백엔드 회신서 (백엔드 → 프론트)

작성 2026-09-12 · 근거: 프론트 요청서(2026-09-11, 워크트리 `worktree-kakao-login`) ·
하이클래스에듀 병목 진단(30p) · 프로 체험 팝업 개발요청서(7p) · 2026-09-10 내부 회의

요청 **8건 전부 처리 완료**(§1 은 콘솔 등록까지 끝냈습니다).
경로가 요청서와 다른 것이 **2건**(§5, §7)이니 프론트 상수만 바꿔 주세요.

| # | 요청 | 상태 | 경로 |
|---|---|---|---|
| 1 | 카카오 콘솔 Redirect URI `localhost:3006` | ✅ **등록 완료** | §1 |
| 2 | 카드 없는 프로 30일 자동 적용 | ✅ 완료 | `POST /api/v1/billing/trial/auto-grant/` |
| 3 | 자동 체험의 종료 표기값 | ✅ 완료 | `subscription.trial_last_day` |
| 4 | 퍼널 이벤트 수집 | ✅ 완료 | `POST /api/v1/track/funnel-event/` |
| 5 | 팝업 노출 상태 서버 저장 | ✅ 완료 | **`/api/v1/auth/me/popup-state/`** (경로 변경) |
| 6 | CAPI StartTrial `trial_kind` | ✅ 완료 | `custom_data.trial_kind` |
| 7 | 인스타그램 로그인 3종 | ✅ 완료 (**기본 OFF**) | `/auth/instagram/start/` · `/auth/instagram/` (경로 변경) |
| 8 | `is_new_user` + `user.id`/`date_joined` | ✅ 완료 | 전 경로 |

---

## 1. 카카오 콜백 URI (dev 3006) — ✅ 등록 완료 (2026-09-12 20:46)

`http://localhost:3006/auth/kakao/callback` 을 콘솔에 **등록·저장·재확인**했습니다.
바로 쓰시면 됩니다(등록 즉시 반영, 서버 배포 불필요 — 백엔드는 `redirect_uri` 를 그대로
카카오에 넘겨 교환합니다).

현재 등록된 5개:

| # | Redirect URI |
|---|---|
| 1 | `https://app.turnflow.link/auth/kakao/callback` |
| 2 | `https://turnflow.link/auth/kakao/callback` |
| 3 | `http://localhost:3000/auth/kakao/callback` |
| 4 | `http://localhost:5173/auth/kakao/callback` |
| 5 | **`http://localhost:3006/auth/kakao/callback`** ← 이번에 추가 |

> 📍 **콘솔에서 찾는 위치가 바뀌었습니다.** 예전 문서의 "카카오 로그인 > 일반" 이 아니라
> **앱 설정 > 앱 > 플랫폼 키 > REST API 키 카드 > `로그인 리다이렉트 URI`** 입니다.
> (`/console/app/1573264/config/platform-key` → REST API 키 수정 화면)
> "카카오 로그인 > 고급" 에 있는 것은 **로그아웃** 리다이렉트라 다른 값입니다.

서버 `.env` 의 `KAKAO_REST_API_KEY`·`KAKAO_CLIENT_SECRET` 이 콘솔 값과 일치하는 것도
함께 확인했습니다(JS 키 오설정 아님).

> ⚠️ 다시 강조: `Kakao.Auth.authorize()` (JS SDK) 경로는 쓰지 마세요. JS 키에는 클라이언트
> 시크릿이 없어 서버 토큰 교환이 깨집니다. authorize·토큰교환의 `client_id` 는 **REST API 키로
> 통일**돼 있습니다 (`docs/frontend/KAKAO_LOGIN_FRONTEND.md`).

---

## 2. 카드 없는 프로 30일 자동 적용 — `POST /api/v1/billing/trial/auto-grant/`

### 정책 확정 내용 (2026-09-12)

| 항목 | 결정 | 비고 |
|---|---|---|
| **대상** | **체험을 쓴 적 없는 사용자 전원** | 광고 귀속 판정 안 함 |
| 부여 시점 | **프론트가 호출할 때만** | 서버 자동 부여 없음 |
| 기간 | 30일 | `AUTO_PRO_TRIAL_DAYS` |
| 만료 후 | **결제 없이** 무료 플랜 복귀 | `trial_used_at` 은 남음 |
| 서버 킬스위치 | `AUTO_PRO_TRIAL_ENABLED` = **기본 ON** | 프론트 `autoProTrial` 과 **둘 다** 켜져야 지급 |

> **`not_ad_attributed` 는 기본 정책에서 나오지 않습니다.**
> 요청서가 가정한 광고 귀속 판정은 코드에 넣어 뒀지만 **꺼 둔 상태**입니다
> (`AUTO_PRO_TRIAL_REQUIRE_AD_ATTRIBUTION=False`). 인앱 브라우저(인스타·카카오)에서 UTM 이
> 자주 유실돼, 켜면 "광고 보고 들어왔는데 못 받는" 억울한 미지급이 생기기 때문입니다.
> 나중에 켜고 싶으면 환경변수 하나로 됩니다.

> **`not_new_user` 도 기본값에서는 나오지 않습니다.**
> 가입 후 허용 창(`AUTO_PRO_TRIAL_SIGNUP_WINDOW_DAYS`)이 **0 = 제한 없음**입니다.
> 이유: 프로 체험 팝업의 실제 타깃이 "이미 가입했지만 체험을 안 켠 사용자"(08-25~09-09
> 기준 499명)라, 창을 좁히면 그 팝업이 지급을 못 해 정책의 절반이 죽습니다.
> 실질 상한은 `trial_used_at`(1인 1회)이 담당합니다.

### 요청

```http
POST /api/v1/billing/trial/auto-grant/
Authorization: Bearer <access>
Content-Type: application/json

{ "source": "signup", "provider": "email" | "google" | "kakao" | "instagram" }
```

`source`/`provider` 는 **로그·분석용**이며 자격 판정에 영향이 없습니다. 생략 가능합니다.
팝업에서 호출할 때는 `source: "pro_trial_popup"` 처럼 구분해 주시면 나중에 경로별 효율을 봅니다.

### 응답 — 지급 성공/실패 **둘 다 200**

```jsonc
// 지급됨
{
  "granted": true,
  "reason": null,
  "subscription": {
    "status": "trialing",
    "plan": { "name": "pro", "display_name": "프로" },
    "has_billing_key": false,
    "current_period_end": "2026-10-12T18:09:34+09:00",
    "trial_last_day": "2026-10-11",      // ← 화면 문구는 이 값
    "trial_kind": "auto",
    "trial_used_at": "2026-09-12T18:09:34+09:00",
    "trial_total_days": 30
  }
}

// 지급 안 됨 (에러가 아닙니다 — 200)
{ "granted": false, "reason": "trial_used", "subscription": { ...현재 구독 전체... } }
```

`reason` 값:

| 값 | 뜻 | 기본 정책에서 발생? |
|---|---|:---:|
| `trial_used` | 이미 체험을 썼다 (카드·쿠폰·자동 통틀어 1인 1회) | ✅ |
| `has_billing_key` | 카드가 이미 등록돼 있다 | ✅ |
| `already_pro` | 무료·active 가 아니다 (유료·체험·정지·해지예약·미납) | ✅ |
| `disabled` | 서버 킬스위치 OFF | 운영 판단 시 |
| `account_pending_deletion` | 탈퇴 유예 중 | 드물게 |
| `not_new_user` | 가입 후 허용 창 초과 | ❌ (창=0) |
| `not_ad_attributed` | 광고 귀속 아님 | ❌ (판정 OFF) |
| `plan_unavailable` | 대상 플랜이 DB 에 없음 (운영 사고) | ❌ |

**멱등합니다.** 네트워크 재시도나 팝업 더블클릭으로 두 번 호출돼도 기간이 60일이 되지 않습니다
(락 안에서 자격을 다시 봅니다). `granted:false` 응답에도 **현재 구독 전체**가 담기니
프론트는 성공/실패 구분 없이 그 값으로 구독 상태를 갱신하면 됩니다.

### 요청서 확인 항목에 대한 답

> "`preview` 가 `attach_only` 를 이 상태에서 정확히 돌려주는지 확인 부탁드립니다."

**확인했고, 테스트로 고정해 뒀습니다** (`apps/billing/test_auto_trial.py::test_preview_returns_attach_only_during_auto_trial`).

```http
GET /api/v1/billing/subscription/preview/?plan_name=pro
```
```json
{ "scenario": "attach_only", "is_trial": true, "trial_days": 30,
  "trial_ends_at": "2026-10-12T18:09:34+09:00", "trial_last_day": "2026-10-11",
  "first_charge_at": "2026-10-12T18:09:34+09:00", "first_charge_amount": 14900 }
```

즉 **체험 중에 카드를 등록하면 기간이 그대로 유지되고** 만료일에 첫 결제가 나갑니다.
체험이 **끝난 뒤** 등록하면 `charge_now`(즉시 결제)입니다 — 이미 30일을 썼기 때문입니다.

> "만료 후 무료 복귀 시 `trial_used_at` 이 남아 있어야 합니다."

남습니다. `trial_used_at`·`trial_plan`·`trial_kind` 는 다운그레이드에서 지우지 않는 **내구
기록**입니다. 만료 경로도 테스트로 고정했습니다(과금 0건 + 무료 복귀 + 재체험 거부).

---

## 3. 자동 체험의 날짜 표기 — `subscription.trial_last_day`

`GET /billing/my-subscription/` 과 `auto-grant` 응답의 `subscription` 에 추가했습니다.

```jsonc
"trial_last_day": "2026-10-11"   // KST 날짜, 체험 중이 아니면 null
```

**프론트의 `trial_ends_at − 1일` 역산을 지워 주세요.** 역산은 UTC 자정 근처에서 하루가
틀어지는데, 그 숫자가 곧 고지 문구라 틀리면 허위 고지가 됩니다.

정의는 결제 전 견적(`preview.trial_last_day`)과 **같은 계산**입니다 —
`KST(current_period_end) − 1일`. 카드 체험은 preview, 카드 없는 체험은 구독에서 읽지만
두 화면이 같은 날짜를 보여 줍니다.

`trial_kind` 도 같이 내려갑니다: `"card"`(카드 등록 체험) / `"auto"`(카드 없는 자동 지급) /
`""`(이 필드 도입 이전 = 전부 카드 체험).

---

## 4. 퍼널 이벤트 — `POST /api/v1/track/funnel-event/`

```http
POST /api/v1/track/funnel-event/
Content-Type: application/json
Authorization: Bearer <access>      // ← 선택. 없으면 익명으로 기록

{
  "event": "trial_popup_view",
  "payload": { "surface": "pc_modal", "attempt": 1, "user_id": 1234, "signup_at": "..." },
  "path": "/home",
  "ts": "2026-09-12T10:31:00+09:00",
  "visitor_id": "3f1c2b74-…",        // 랜딩 스니펫의 tf_vid (선택)
  "device": "ios",                    // ios | android | pc | unknown
  "in_app": true,
  "in_app_kind": "instagram"
}
```

- **항상 204**(바디 없음). 잘못된 페이로드·봇·DB 오류 전부 204 입니다 — 비콘이라
  사용자 화면을 절대 깨지 않습니다. 유일한 비-204 는 스로틀 **429**(기본 600/hour).
- **이벤트 이름·필드는 프론트가 정본**입니다. 서버는 화이트리스트를 두지 않습니다
  (두면 이벤트 추가할 때마다 백엔드 배포를 기다려야 하고 그사이 이벤트가 유실됩니다).
  제한은 `event` 60자, `payload` 4KB 뿐입니다.
- 요청서 §07 의 네 이벤트(`trial_popup_view/click/dismiss`, `trial_started_from_popup`)는
  이름·필드 그대로 받습니다.
- `ts` 는 저장하되 **집계는 서버 수신 시각**을 씁니다(기기 시계는 틀릴 수 있습니다).
- IP 는 SHA-256 해시만 저장합니다. 보존 180일(`FUNNEL_EVENT_RETENTION_DAYS`).

### ⚠️ `sendBeacon` 주의

`navigator.sendBeacon` 은 `Authorization` 헤더를 붙일 수 없어 **익명으로 기록**됩니다.
로그인 사용자를 묶어야 하는 이벤트(`user_id` 가 의미 있는 것 전부)는
`fetch(..., { keepalive: true })` 로 보내 주세요.

### `device` 를 프론트가 보내는 이유

서버는 User-Agent 로 `desktop/mobile/tablet` 까지만 가릅니다. 요청서가 요구하는
"기기별 분리 집계"는 **iOS / Android 를 갈라 봐야** 하고(인앱 브라우저의 OAuth 복귀
동작이 갈립니다), iPadOS 처럼 데스크톱을 사칭하는 UA 도 있어 서버 파생만으로는 부정확합니다.
서버 파생값(`ua_class`)도 같이 저장하니 둘이 어긋나는 경우를 나중에 볼 수 있습니다.

---

## 5. 팝업 노출 상태 — ⚠️ 경로가 다릅니다

**요청**: `GET/PATCH /api/v1/users/me/popup-state/`
**실제**: `GET/PATCH /api/v1/auth/me/popup-state/`

이 저장소의 "나" 리소스는 전부 `/api/v1/auth/me/` 아래에 있습니다(`auth/me/`,
`auth/me/delete/`). `users/` prefix 를 새로 만들면 같은 리소스가 두 위치에 생겨서
기존 규칙을 따랐습니다. 프론트 상수 한 곳만 바꿔 주세요.

```jsonc
// GET → 200
{ "popup_state": {
    "signup":       { "shows": 2, "dismisses": 1, "lastShownAt": "...", "converted": false },
    "pro_trial":    { "shows": 1, "converted": true },
    "trial_top_bar":{ "shows": 5 }
} }

// PATCH → 200 (갱신 후 전체 상태)
```

### ⚠️ 병합 규칙 — 여기만 읽으셔도 됩니다

병합은 **최상위 키 단위(shallow)** 입니다. 한 팝업 객체를 보내면 그 팝업 상태가 **통째로
교체**됩니다.

```
저장된 값: {"pro_trial": {"shows": 2, "dismisses": 1}}
PATCH     {"pro_trial": {"converted": true}}
결과      {"pro_trial": {"converted": true}}        ← shows/dismisses 가 사라짐
```

→ **읽은 객체를 펼쳐서 통째로 다시 보내세요**: `{...prev.pro_trial, converted: true}`

깊은 병합을 하지 않는 이유는, 서버가 필드 의미를 모르는 상태에서 깊게 병합하면
"카운터를 0으로 되돌리는" 의도적인 리셋을 표현할 방법이 사라지기 때문입니다.

키 **삭제**는 값으로 `null`: `{"pro_trial": null}`.

기타: 키 이름·내부 필드는 자유(서버가 강제하지 않음) · 전체 8KB · 최상위 32키 ·
동시 요청은 last-write-wins(카운터 1 차이는 노출 규칙에 영향 없어 락을 걸지 않았습니다).

> 이 값은 **현재 상태**만 담습니다. 노출/클릭/닫기의 *시점* 기록은 §4 퍼널 이벤트로 보내세요.

---

## 6. CAPI StartTrial `trial_kind`

서버 CAPI 의 `StartTrial` 에 `custom_data.trial_kind` 를 실어 보냅니다:

| 값 | 의미 |
|---|---|
| `card` | 카드 등록 체험 (`toss/confirm` scenario=trial) — 만료 시 첫 과금 |
| `auto` | 카드 없는 자동 지급 — 만료 시 과금 없이 무료 복귀 |

프론트 픽셀은 요청서대로 **카드 체험에서만** `StartTrial` 을 쏘고, 자동 지급은 커스텀
`auto_trial_granted` 로 보내시면 됩니다. 서버 CAPI 는 양쪽 다 `StartTrial` 로 보내되
`trial_kind` 로 갈라 놓으니, 대행사가 두 경로의 유료 전환율을 따로 볼 수 있습니다.
(이 필드가 없으면 Meta 쪽에서 둘이 뭉쳐서 카드 없는 체험이 전환율을 통째로 희석시킵니다.)

`event_id` 규약은 그대로 `str(subscription.id)` 입니다.

---

## 7. 인스타그램 로그인 — ⚠️ 경로가 다르고, **기본 OFF** 입니다

요청서는 P2 로 내리셨지만 **구현을 완료했습니다.** 가입과 IG 연동을 한 번에 끝내는 것이
병목 진단 1순위라서입니다.

| 요청 | 실제 |
|---|---|
| `GET /api/v1/auth/instagram/start/` → `{authorize_url, state}` | ✅ 그대로 |
| `GET /auth/instagram/callback` (프론트 라우트) | ✅ 그대로 |
| `POST /api/v1/auth/instagram/` `{code, state}` | ✅ 그대로 (단 `redirect_uri` 는 **보내지 마세요**) |

### 🔴 켜기 전 필요한 것 (백엔드 배포만으로는 동작하지 않습니다)

1. **Meta 앱 대시보드 > Instagram > Business login settings > OAuth redirect URI** 에
   프론트 콜백 주소를 **글자 그대로** 추가
   (dev: `http://localhost:3006/auth/instagram/callback`, prod: `https://app.turnflow.link/auth/instagram/callback`)
2. 서버 환경변수 `INSTAGRAM_LOGIN_ENABLED=True` + `INSTAGRAM_LOGIN_REDIRECT_URI=<위 주소>`

**끄면 두 엔드포인트 모두 404 + `code: "INSTAGRAM_LOGIN_DISABLED"`** 입니다.
기본을 끈 이유는 2026-09-10 내부 논의 때문입니다 — "한 번 넣으면 그걸로 로그인한 사람이
한 명이라도 생기는 순간 다시는 못 뺀다." 테스트 URL 에서 먼저 써 보고 켜기로 했습니다.

### 흐름

```javascript
// 1) 시작
const { authorize_url, state } = await fetch(
  '/api/v1/auth/instagram/start/?redirect_uri=' + encodeURIComponent(CALLBACK)
).then(r => r.json());
location.href = authorize_url;          // ← 팝업 말고 같은 탭 (아래 주의)

// 2) 콜백 라우트에서
const { code, state } = parseQuery();
const res = await fetch('/api/v1/auth/instagram/', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ code, state, attribution: readAttribution() }),
});
```

`redirect_uri` 는 **허용된 프론트 origin 과 완전일치**해야 합니다(오픈 리다이렉트 방어).
생략하면 서버 기본값을 씁니다. 교환(`POST`)에는 **보내지 마세요** — `state` 에 저장된 값을
씁니다(authorize 때와 한 글자만 달라도 Meta 가 거부하는데 원인 찾기가 매우 어렵습니다).

### 응답 (200)

```jsonc
{
  "user": { "id": 1234, "email": "ig_17841400000@ig.invalid",
            "email_is_placeholder": true, "is_email_verified": false, ... },
  "is_new_user": true,
  "tokens": { "access": "...", "refresh": "..." },
  "ig_connection": { "id": "...", "username": "turnflow", "status": "active" },
  "ig_connection_error": null
}
```

**신규 가입이면 이 한 번의 호출로 계정 + 워크스페이스 + IG 연동이 전부 만들어집니다.
별도 연동 단계가 없습니다.**

### 🔴 이메일이 없습니다 (프론트 대응 필요)

Instagram Business Login 은 **이메일을 주지 않습니다**(스코프에 없고 추가할 방법도 없습니다.
Facebook Login 이면 가능하지만 별도 심사 + 한 달 이상이라 회의에서 보류했습니다).
우리 로그인 키가 이메일이라 비워 둘 수 없어 `ig_<instagram_user_id>@ig.invalid` 를 발급합니다.

- 응답의 **`user.email_is_placeholder === true`** → "알림 받을 이메일을 등록해 주세요"
  배너를 띄워 주세요. 이 사용자는 체험 종료 안내·연결 끊김 알림 메일을 **못 받습니다**.
- 서버는 이 주소로 **메일을 보내지 않습니다**(발송 계층에서 차단).
- `.invalid` 는 RFC 2606 예약 TLD 라 실수로 발송돼도 바운스가 생기지 않습니다.
- **미구현**: 이메일 변경 API. 현재 `PATCH /auth/me/` 는 `full_name`·`marketing_opt_in` 만
  받습니다. 배너를 실제로 동작시키려면 이메일 등록+인증 플로우가 추가로 필요합니다
  (백엔드 후속 작업, §10 참고).

### 계정 매칭 (같은 사람에게 계정이 두 개 생기지 않게)

1. `User.instagram_user_id` 일치 → 그 사용자로 로그인
2. **그 IG 계정을 이미 연동해 둔 워크스페이스의 소유자** → 그 계정으로 로그인 (구글/이메일로
   가입해 연동까지 끝낸 기존 사용자가 나중에 "인스타로 로그인"을 누르는 경우)
3. 둘 다 없으면 신규 가입

`username` 으로는 절대 찾지 않습니다 — IG 핸들은 언제든 바뀌고 남이 이어받을 수 있습니다.

### 주의

- **iOS 는 `www.instagram.com/oauth/authorize` 를 유니버설 링크로 판정해 인스타 앱을
  띄웁니다.** 팝업 방식이면 부모 창이 결과를 영영 못 받습니다 → **같은 탭 이동**을 쓰세요.
- `state` 는 **1회용·10분**입니다. 콜백 화면에서 새로고침하면 `INSTAGRAM_STATE_USED` 400 →
  "다시 시도" 버튼은 `start` 부터 다시 밟게 하세요.
- `ig_connection` 이 `null` + `ig_connection_error` 가 채워질 수 있습니다
  (`PLAN_LIMIT_EXCEEDED`, `ALREADY_CONNECTED_ELSEWHERE`, `CONNECT_FAILED`).
  **로그인 자체는 성공**이므로 200 입니다 — 설정 화면으로 안내해 주세요.
- 탈퇴 유예 계정은 **409** (구글·카카오와 동일).

---

## 8. 응답 필드 확인

| 항목 | 상태 |
|---|---|
| `POST /auth/google/` `is_new_user` | ✅ 이미 있었음 (유지) |
| `POST /auth/kakao/` `is_new_user` | ✅ 이미 있었음 (유지) |
| `POST /auth/register/` `is_new_user` | ✅ **추가함** (201 이면 항상 `true`) |
| `user.id`, `user.date_joined` | ✅ 이미 `UserSerializer` 에 있음 |

`register` 에도 값을 내려주는 이유는, 프론트 전환 이벤트 코드가 가입 수단마다 분기를
세 벌 들지 않도록 하기 위해서입니다.

`UserSerializer` 에 **`email_is_placeholder`(bool)** 가 새로 추가됐습니다(§7).

---

## 9. 덤으로 고친 것 — 제휴코드(44일 쿠폰) 사망 방지

자동 지급이 켜지면 가입 직후 이미 `trialing` 이라, 제휴코드를 받던 `scenario="trial"` 에
영영 도달하지 못합니다. **그대로 뒀으면 44일 쿠폰이 항상 400 이 되어 통째로 죽었습니다.**

- `POST /billing/toss/confirm/` 의 `referral_code` 가 **체험 중(attach_only)에도** 통합니다
  → 남은 체험 끝에 보너스 일수를 이어 붙입니다.
- `POST /billing/referral/redeem/` 이 **연장 전용으로 부활**했습니다(카드 불필요).
  - 체험 중이 아니면 `400 REFERRAL_NOT_TRIALING` — 이 경로로 체험을 **시작**할 수는 없습니다.
    (폐지 사유였던 "base 30일 누락" 결함은 연장에서는 구조적으로 재발할 수 없습니다.)
  - 응답: `{ success, referral_code, bonus_days, trial_ends_at, trial_last_day, total_trial_days, detail }`
- `GET /billing/subscription/preview/` 에 `referral_code` 를 얹으면 연장 후 날짜로 견적이 나옵니다.
- 표기는 여전히 **`total_trial_days`(44)** 를 쓰세요. `bonus_days`(14)만 노출하면 혜택이
  1/3로 축소돼 보입니다.

---

## 10. 남은 것 / 백엔드 후속

| 항목 | 상태 |
|---|---|
| Meta 앱 IG 로그인 redirect URI 등록 | ⏳ 콘솔 작업 (§7) |
| `INSTAGRAM_LOGIN_ENABLED=True` 전환 | ⏳ 테스트 후 운영 판단 |
| **이메일 변경/등록 API** (IG 가입자용) | ❌ 미구현 — §7 배너의 착지점이 아직 없음 |
| 카카오 연결 해제 웹훅 | ❌ 미구현 (콘솔이 개인정보 처리 누락 경고 중) |

## 참고 — 서버 환경변수

```bash
# 카드 없는 프로 30일
AUTO_PRO_TRIAL_ENABLED=True                    # 기본 ON
AUTO_PRO_TRIAL_DAYS=30
AUTO_PRO_TRIAL_SIGNUP_WINDOW_DAYS=0            # 0=제한 없음
AUTO_PRO_TRIAL_REQUIRE_AD_ATTRIBUTION=False    # 광고 귀속 판정 OFF

# 인스타 로그인 (기본 OFF)
INSTAGRAM_LOGIN_ENABLED=False
INSTAGRAM_LOGIN_REDIRECT_URI=https://app.turnflow.link/auth/instagram/callback

# 퍼널 이벤트
FUNNEL_EVENT_RETENTION_DAYS=180
THROTTLE_FUNNEL_EVENT=600/hour
THROTTLE_AUTH_INSTAGRAM=20/min
```

## 마이그레이션

`billing 0026` · `authentication 0007·0008` · `analytics 0008·0009`
