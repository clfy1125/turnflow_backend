# 긴급 전환 개선 — 백엔드 2차 회신 (백엔드 → 프론트)

작성 2026-09-12 · 프론트 요청 4건(2026-09-12)에 대한 회신
1차 회신: `URGENT_CONVERSION_BACKEND_RESPONSE.md`

| # | 요청 | 상태 |
|---|---|---|
| 1 | 인스타그램 로그인 켜기 | ✅ **dev 켜짐** (Pages 프리뷰로 등록·종단 확인) · 운영 OFF 유지 (§1) |
| 2 | IG 가입자 이메일 등록·인증 API | ✅ **구현 완료** (§2) |
| 3 | 운영 배포 일정 | ✅ **프론트 준비되는 즉시 가능** (§3) |
| 4 | 자동 체험 만료 / `trial_kind` 확인 | ✅ 답변 (§4) |

---

## 1. 인스타그램 로그인 — dev 활성화 완료 (운영은 OFF)

### ✅ 등록한 것

Meta 앱이 **두 개**라는 걸 이번에 확인했습니다. 각각 맞는 곳에 넣었습니다.

| 앱 | Facebook App ID | Instagram App ID | 지금 등록된 OAuth 리디렉션 URI |
|---|---|---|---|
| **TurnFlow dev** | 959975189724026 | 1577778924348206 | `https://pro-earwig-presently.ngrok-free.app/api/v1/integrations/instagram/connect/callback/`<br>`https://dev-api.turnflow.link/api/v1/integrations/instagram/connect/callback/` |
| **TurnFlow** (운영) | 1497161828629570 | 36036852472566509 | `https://api.turnflow.clfy.ai.kr/api/v1/integrations/instagram/connect/callback/`<br>`https://turnflow-api.clfy.ai.kr/api/v1/integrations/instagram/connect/callback/`<br>**`https://app.turnflow.link/auth/instagram/callback`** ← 이번에 추가 |

**운영 앱에 프론트 로그인 콜백을 미리 등록해 뒀습니다.** 저장 후 새로고침해 반영을
확인했고, 기존 백엔드 콜백 2개는 그대로입니다.

### 🔴 확정된 제약 — `http://localhost` 는 **등록되지 않습니다**

dev 앱에 `http://localhost:3006/auth/instagram/callback` 을 넣어 저장해 봤습니다.
입력창은 받아 주는데 **저장하면 조용히 사라집니다**(오류 메시지도 없습니다).
새로고침하면 목록에 없습니다. Instagram Business Login 의 리디렉션 URI 는 **HTTPS 전용**입니다.

### ✅ 해결 — dev 는 Pages 프리뷰 주소로 갑니다 (터널 불필요)

`turnflowlink-dev` Cloudflare Pages 배포가 이미 있어서 그걸 씁니다.

```
https://turnflowlink-dev.pages.dev/auth/instagram/callback
```

**Meta dev 앱에 등록 완료**(저장 후 새로고침으로 반영 확인), **백엔드 dev 도 설정 완료**입니다:

```bash
# dev .env (적용됨, web 재시작 완료)
INSTAGRAM_LOGIN_ENABLED=True
INSTAGRAM_LOGIN_REDIRECT_URI=https://turnflowlink-dev.pages.dev/auth/instagram/callback
CORS_ALLOWED_ORIGINS=...,https://turnflowlink-dev.pages.dev
```

dev-api 종단 확인까지 끝냈습니다:

```bash
curl "https://dev-api.turnflow.link/api/v1/auth/instagram/start/"
# → {"authorize_url":"https://www.instagram.com/oauth/authorize?client_id=1577778924348206
#     &redirect_uri=https%3A%2F%2Fturnflowlink-dev.pages.dev%2Fauth%2Finstagram%2Fcallback
#     &scope=...","state":"...","mode":"production"}

curl "https://dev-api.turnflow.link/api/v1/auth/instagram/start/?redirect_uri=https://evil.example.com/cb"
# → 400 INSTAGRAM_INVALID_REDIRECT_URI
```

**이제 프론트에서 `/auth/instagram/callback` 라우트만 배포하면 바로 테스트됩니다.**

> ⚠️ **워크플로 주의**: 이 경로로 가면 IG 로그인은 **배포된 dev 빌드**(`turnflowlink-dev`
> Pages)에서만 테스트됩니다. `localhost:3006` 로는 불가합니다(Meta 가 http 를 거부).
> 로컬에서 돌리셔야 하면 **각자 PC 에서** HTTPS 터널을 띄우고 주소를 주세요 —
> `cloudflared tunnel --url http://localhost:3006` (로그인 불필요). 다만 그 주소는
> **재시작마다 바뀌고** 그때마다 Meta 콘솔에 다시 등록해야 합니다.
> 그래서 Pages 프리뷰를 기본으로 권합니다.
>
> 참고: 백엔드가 대신 터널을 띄울 수 없는 이유 — 터널은 **프론트 dev 서버가 도는 PC** 에서
> 떠야 하는데 3006 은 백엔드 PC 에 없습니다.

### 서버 쪽 준비 상태 (허용 origin)

백엔드는 `redirect_uri` 를 **허용 origin 완전일치**로 검증합니다. 아래는 통과합니다:

```
https://turnflowlink-dev.pages.dev/auth/instagram/callback   → 통과 (dev)
https://app.turnflow.link/auth/instagram/callback            → 통과 (운영)
https://evil.example.com/auth/instagram/callback             → 거부 (INSTAGRAM_INVALID_REDIRECT_URI)
```

### 어느 URL 에서 먼저 테스트할지 — **dev 로 정했습니다**

2026-09-10 내부 논의 그대로입니다 — "한 번 넣으면 그걸로 로그인한 사람이 한 명이라도
생기는 순간 다시는 못 뺀다." 그래서 **dev 만 켰고 운영은 `INSTAGRAM_LOGIN_ENABLED=False`
그대로**입니다. 운영은 리디렉션 URI 만 미리 등록해 둔 상태라 아직 아무 동작도 하지 않습니다
(두 엔드포인트가 404 `INSTAGRAM_LOGIN_DISABLED` 를 냅니다).

### 🟡 별건 — 배포된 프론트의 `/auth/*` 가 상태코드 404 로 나갑니다

확인 결과 **기능에는 문제가 없습니다**(본문은 정상 SPA HTML 이라 브라우저가 그대로
렌더링합니다 — 카카오 로그인도 이 때문에 깨지지 않습니다). 다만 상태코드만 404 입니다:

```
https://app.turnflow.link/login               → 200
https://app.turnflow.link/nonexistent-xyz123  → 200   ← SPA 폴백 정상
https://app.turnflow.link/auth/kakao/callback → 404   ← /auth/* 만
```

`/auth/*` 만 SPA 폴백에서 빠져 있습니다(`functions/` 또는 `_routes.json` 쪽으로 보입니다).
카카오·인스타 콜백이 전부 이 경로 아래라 **모니터링에 404 가 계속 쌓입니다.**
급한 건 아니지만 한 번 보시는 게 좋겠습니다.

### ⚠️ 배포 전 확인 필요 — 운영 `.env` 의 IG 앱 자격증명

운영 앱의 Instagram App ID 는 **36036852472566509** 입니다(dev 는 1577778924348206).
운영 `.env` 의 `INSTAGRAM_APP_ID`/`INSTAGRAM_APP_SECRET` 이 운영 앱 값인지
배포 전에 한 번 확인해 주세요 — 여기가 어긋나면 인스타 로그인이 운영에서
`INSTAGRAM_CODE_INVALID` 로만 떨어집니다(원인 찾기가 매우 어렵습니다).

> 참고: 운영 앱의 Instagram 앱 **이름**이 `TurnFlow local dev-IG` 로 되어 있습니다.
> 동작에는 영향이 없지만 다음 사람이 헷갈릴 이름이라 정리해 두시면 좋겠습니다.

## 2. IG 가입자 이메일 등록·인증 API — ✅ 구현 완료

제안하신 경로 그대로입니다.

### ① 등록 신청

```http
POST /api/v1/auth/me/email/
Authorization: Bearer <access>

{ "email": "me@example.com" }
```
```json
202 { "detail": "인증 코드를 보냈습니다.",
      "pending_email": "me@example.com", "expires_minutes": 30 }
```

### ② 확정

```http
POST /api/v1/auth/me/email/verify/
Authorization: Bearer <access>

{ "code": "482913" }
```
```json
200 { "detail": "이메일이 등록되었습니다.",
      "user": { "id": 1234, "email": "me@example.com",
                "email_is_placeholder": false, "pending_email": "",
                "is_email_verified": true } }
```

### 프론트가 알아야 할 것

| 항목 | 값 |
|---|---|
| 코드 | **6자리 숫자**, 유효 30분, **1회용** |
| 코드 수신처 | **새로 입력한 주소** (기존 자리표시 주소로는 안 갑니다) |
| 진행 상태 | `user.pending_email` — `GET /auth/me/` 에 포함됩니다 |
| 스로틀 | 신청 5회/시간(사용자), 코드 확인 10회/분(IP) |

**⚠️ 인증이 끝나기 전에는 `user.email` 도 `email_is_placeholder` 도 바뀌지 않습니다.**
오타 한 번에 로그인 키가 아무도 소유하지 않는 주소로 넘어가면 복구 경로가 사라지기
때문에, 후보 주소는 `pending_email` 에만 담아 둡니다. 화면은 `pending_email` 이 있으면
"인증 코드 입력" 단계로 보내 주세요.

**재신청하면 이전 코드는 즉시 무효**입니다(직전 주소로 받은 코드로 새 주소를 확정해
버리는 것을 막습니다). "코드 다시 받기"는 ①을 다시 호출하면 됩니다.

### 오류 코드

| HTTP | `code` | 뜻 / 화면 처리 |
|---|---|---|
| 400 | `EMAIL_CHANGE_NOT_ALLOWED` | 자리표시 계정이 아님 → 배너 자체를 숨기세요 |
| 400 | `NO_PENDING_EMAIL` | 신청 없이 확정 시도 → ① 부터 다시 |
| 400 | `EMAIL_CODE_INVALID` | 코드 불일치/만료 → 재입력 또는 재발송 |
| 409 | `EMAIL_ALREADY_TAKEN` | 다른 계정이 쓰는 주소 → 다른 주소 입력 |
| 429 | — | 스로틀. "잠시 후 다시" |

오류는 1차 회신과 같이 `detail`/`code` + 표준 envelope 를 **함께** 담습니다.

### ⚠️ 범위 — 일반 "이메일 변경" 기능이 아닙니다

**자리표시 이메일 계정(`email_is_placeholder === true`)에서만** 동작합니다.
등록이 끝나면 그 계정도 더 이상 이 API 를 쓸 수 없습니다(400 `EMAIL_CHANGE_NOT_ALLOWED`).

비밀번호가 있는 계정에까지 열면 **세션 하나만 탈취해도 [이메일 변경 → 비밀번호 재설정]
으로 계정을 통째로 가져갈 수 있습니다.** 일반 이메일 변경이 필요해지면 *현재 비밀번호
확인*을 함께 붙여 별도로 만들어야 합니다. 지금은 그 기능이 없다고 보시면 됩니다.

---

## 3. 운영 배포 — **프론트가 준비되는 즉시 가능합니다**

백엔드 쪽에 대기 요인이 없습니다. 원하시는 시점을 알려 주시면 그날 올립니다.

### 마이그레이션 6건 — 전부 무중단·즉시

| 앱 | 번호 | 연산 | 잠금 |
|---|---|---|---|
| billing | 0026 | `AddField trial_kind` (기본값 `""`) | 없음 |
| authentication | 0007 | `AddField popup_state` (기본값 `{}`) | 없음 |
| authentication | 0008 | `CreateModel InstagramLoginState` + `AddField instagram_user_id` | 없음 |
| authentication | 0009 | `AddField pending_email` (기본값 `""`) | 없음 |
| analytics | 0008 | `CreateModel FunnelEvent` | 없음 |
| analytics | 0009 | `AlterField signup_kind` (choices 만) | **SQL 없음** |

데이터 마이그레이션(`RunPython`/`RunSQL`)이 하나도 없고, 전부 상수 기본값 `AddField`
또는 신규 테이블입니다. 대상 테이블도 작습니다(운영 `users` 수백 행 규모).
**구버전 프론트와도 그대로 호환**됩니다 — 응답에 필드가 늘어날 뿐입니다.

### 플래그 상태 (배포 직후)

| 플래그 | 서버 기본값 | 비고 |
|---|---|---|
| `AUTO_PRO_TRIAL_ENABLED` | **True** | 프론트 `autoProTrial` 이 꺼져 있으면 호출 자체가 없어 무해 |
| `INSTAGRAM_LOGIN_ENABLED` | **False** | 운영은 계속 꺼 둡니다 (§1) |
| 그 외 (funnel-event, popup-state, 이메일 등록) | 플래그 없음 | 엔드포인트만 열립니다 |

제안하신 순서(`loginKakao` → `funnelBeacon` → `autoProTrial` → `loginInstagram`)에
백엔드 쪽 제약은 없습니다. 다만 **`autoProTrial` 을 켜기 전에 `funnelBeacon` 이 먼저
켜져 있어야** 팝업→체험 전환 퍼널이 처음부터 측정됩니다(나중에 켜면 그 구간이 빕니다).

### 배포 후 확인할 것 (백엔드가 봅니다)

- `billing.handle_trial_expiry` 로그에 `processed` 가 정상적으로 찍히는지
- `auto_trial granted` 로그 건수 vs 프론트 `trial_started_from_popup` 이벤트 건수
- CAPI `StartTrial` 의 `trial_kind` 분포

---

## 4. 자동 체험 만료 · `trial_kind` — 답변

### ① 무료 복귀 배치 주기

`billing.handle_trial_expiry` — **매시간**(3600초 간격, 고정 시각이 아니라 Beat 기동
기준 인터벌). 즉 **체험 종료 시각으로부터 최대 1시간 이내**에 무료로 내려갑니다.

동작: 빌링키가 **없는** `trialing` 구독 중 `current_period_end < now` 인 것을
무료 플랜으로 다운그레이드합니다. **결제는 일어나지 않습니다**
(`process_due_renewals` 는 빌링키가 **있는** 것만 과금하므로 서로 겹치지 않습니다).

다운그레이드 시 함께 일어나는 일: 페이지·IG 계정이 무료 허용량으로 축소되고,
초과분은 비활성화됩니다. `trial_used_at`·`trial_plan`·`trial_kind` 는 **남습니다**.

> 정확한 만료 시각(초 단위)을 화면에 쓰지 마세요. "{trial_last_day}까지" 로 표기하고,
> 실제 차단은 최대 1시간 뒤일 수 있다는 점만 알고 계시면 됩니다.

### ② 자동 체험 중 카드를 등록하면 `trial_kind` 는?

**`auto` 로 그대로 남습니다.** `card` 로 바뀌지 않습니다 — 의도된 동작입니다.

`trial_kind` 는 "체험이 **어떻게 시작됐나**"의 내구 기록이고, "지금 카드가 있나"는
`has_billing_key` 가 따로 말해 줍니다. 카드를 붙였다고 값을 덮으면
**"자동 지급받은 사람 중 몇 %가 카드를 붙였나"** 라는 코호트 질문에 영영 답할 수
없게 되는데, 그게 이 정책의 핵심 지표입니다.

대행사가 보는 축은 이렇게 나눠집니다:

| 보고 싶은 것 | 보는 값 |
|---|---|
| 카드 없는 체험이 몇 건 켜졌나 | `StartTrial` + `custom_data.trial_kind = "auto"` |
| 그중 몇 명이 카드를 붙였나 | `trial_kind="auto"` **AND** `has_billing_key=true` (서버 집계) |
| 실제 유료 전환 | **`Purchase`** (체험 종료 후 첫 결제) |

### ③ StartTrial 은 언제 발사되나 (중요)

**체험이 시작될 때 한 번만** 발사됩니다:
- 자동 지급 시 (`trial_kind="auto"`)
- 카드 등록으로 체험을 시작할 때 (`trial_kind="card"`)

**자동 체험 중 카드를 부착해도 재발사하지 않습니다.** 재발사하면 Meta 에서 같은
사람의 체험이 두 번 집계됩니다. 그래서 **"카드 없는 체험 → 유료"** 전환은
`StartTrial` 이 아니라 **`Purchase`** 로 보셔야 합니다.

위 ②·③ 동작은 회귀 테스트로 고정해 뒀습니다
(`apps/billing/test_auto_trial.py::TestTrialKindSurvivesCardAttach`).

---

## 5. 참고 — `source` 값

프론트가 보내는 `source`(`signup`, `popup_pro_trial_sheet`, `popup_top_bar`,
`home_card`, `header_badge`, `inline_dm` …)는 **그대로 로그에 남습니다.**
서버는 이 값으로 자격을 판정하지 않으므로 자유롭게 늘리셔도 됩니다.
나중에 진입점별 지급 건수를 세야 할 때 이 값이 유일한 단서라, 지금 규칙대로 계속
구분해 보내 주시면 됩니다.

---

## 6. 이번 회차에 함께 고친 것 (프론트 영향 없음)

- **결제 시나리오 오판정 방지**: `confirm_billing` 이 구독을 DB 에서 다시 읽도록 했습니다.
  캐시된 구독으로 판정하면 **이미 체험 중인 사람이 `trial` 로 재판정돼 30일이 다시 깔리고
  첫 결제가 밀릴** 수 있었습니다(자동 지급 도입으로 같은 요청 안에서 구독을 먼저 만지는
  경로가 생기면서 열린 구멍). 테스트로 고정했습니다.
- dev/테스트에서 `email_change` 스로틀 비활성 목록 추가 (기존 `email_send` 와 동일 처리).

## 마이그레이션 (1차 + 2차 누적)

`billing 0026` · `authentication 0007·0008·0009` · `analytics 0008·0009`

배포 시 **`python manage.py seed_email_templates` 를 함께 실행**해 주세요
(이메일 등록 인증 메일 템플릿 `email_change_verify` 신규).
