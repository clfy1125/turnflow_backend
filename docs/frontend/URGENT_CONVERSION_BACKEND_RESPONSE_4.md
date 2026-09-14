# 긴급 전환 개선 — 백엔드 4차 회신 (운영 배포 완료 + 견적 버그 수정)

작성 2026-09-14 · 프론트 요청 2건(운영 설정 확인 / 구독 견적 버그)에 대한 회신
운영 커밋: **`43ab213`** (직전 `fb22421`) · 에러 로그 0건 · DB 블립 없음

| # | 요청 | 상태 |
|---|---|---|
| 1 | 카카오 `KAKAO_NOT_CONFIGURED(503)` | ✅ **고침 — 켜셔도 됩니다** |
| 2 | 인스타 로그인 켜기 전 3단계 | ①② 완료 · ③은 대기(프론트 dev 확인 후) |
| 3 | env·마이그레이션 운영 적용 확인 | ✅ 전부 적용 |
| 4 | 견적 `attach_only` 추가계정 미합산 | ✅ **고침** (원인 2곳이었습니다) |

---

## 1. 카카오 503 — 백엔드 누락이었습니다

**배포는 정상이었고, 운영 `.env` 에 카카오 키가 아예 없었습니다.** 인스타 로그인 env 를
넣으면서 카카오 env 를 빠뜨렸습니다. 코드만 올리고 설정을 안 올린 것입니다.

dev 와 **같은 앱(1573264)** 이라 같은 값을 넣었습니다:

| 키 | 상태 |
|---|---|
| `KAKAO_REST_API_KEY` | ✅ 콘솔 REST API 키와 일치 확인 |
| `KAKAO_CLIENT_SECRET` | ✅ 콘솔 시크릿과 일치 확인 |
| `KAKAO_APP_ID` | ✅ |

**검증 (운영 실호출)**

```bash
POST https://turnflow-api.clfy.ai.kr/api/v1/auth/kakao/
     {"code":"dummy_invalid_code","redirect_uri":"https://turnflow.link/auth/kakao/callback"}

# 이전: 503 KAKAO_NOT_CONFIGURED
# 지금: 400 KAKAO_CODE_INVALID   ← dev 와 동일 = 설정 완료
```

**콘솔 Redirect URI** — `https://turnflow.link/auth/kakao/callback` **이미 등록돼 있습니다.**
현재 5개: `app.turnflow.link` · `turnflow.link` · `localhost:3000` · `localhost:5173` ·
`localhost:3006`.

→ **`loginKakao` 플래그 켜셔도 됩니다.**

---

## 2. 인스타그램 로그인 — ①② 완료, ③만 남음

### ① 운영 Meta 앱 콜백 — ✅ 고쳤습니다

지적하신 대로였습니다. **번들 해시로 직접 확인했고 프론트 진단이 정확했습니다:**

| 호스트 | 번들 | 호출 API |
|---|---|---|
| `turnflowlink-dev.pages.dev` | `index-DUCu6nwh.js` | `dev-api.turnflow.link` |
| `app.turnflow.link` | **같은** `index-DUCu6nwh.js` | `dev-api.turnflow.link` ← dev 빌드 |
| `turnflow.link` | `index-DwHLnwjP.js` | `turnflow-api.clfy.ai.kr` ← 운영 |

조치:
- **운영 앱(36036852472566509)**: `https://turnflow.link/auth/instagram/callback` **추가**,
  제가 잘못 넣었던 `app.turnflow.link` **제거**
- **dev 앱(1577778924348206)**: `https://app.turnflow.link/auth/instagram/callback` **추가**
  (요청 ② — 사장님·QA 가 그 주소로 보시니까)

현재 등록 상태:

```
운영 앱  api.turnflow.clfy.ai.kr/...connect/callback/      (연동)
        turnflow-api.clfy.ai.kr/...connect/callback/      (연동)
        turnflow.link/auth/instagram/callback             (로그인) ← 추가

dev 앱   pro-earwig-presently.ngrok-free.app/...callback/ (연동)
        dev-api.turnflow.link/...connect/callback/        (연동)
        turnflowlink-dev.pages.dev/auth/instagram/callback (로그인)
        app.turnflow.link/auth/instagram/callback          (로그인) ← 추가
```

### ② 운영 `.env` 앱 자격증명 — ✅ 운영 앱 값이 맞습니다

```
INSTAGRAM_APP_ID       = 36036852472566509   ← 운영 앱 값 ✅ (dev 는 1577778924348206)
META_APP_ID            = 1497161828629570    ← 운영 FB 앱 ✅
INSTAGRAM_REDIRECT_URI = https://turnflow-api.clfy.ai.kr/...connect/callback/  ← 등록값과 일치 ✅
```

### ③ `INSTAGRAM_LOGIN_ENABLED=True` — 대기 중

운영 `.env` 에 값은 미리 넣어 뒀고 **False 유지**입니다:

```
INSTAGRAM_LOGIN_ENABLED=False
INSTAGRAM_LOGIN_REDIRECT_URI=https://turnflow.link/auth/instagram/callback
```

운영 `GET /auth/instagram/start/` → **404 `INSTAGRAM_LOGIN_DISABLED`** (의도대로).

**dev 에서 확인하신 뒤 한 줄 주시면 켜겠습니다.** dev 는 이미 켜져 있습니다
(`turnflowlink-dev.pages.dev` 기준, 종단 확인 완료).

---

## 3. env·마이그레이션 운영 적용 — ✅ 전부

**미적용 마이그레이션 0건.** 적용 확인:

```
billing        0026_usersubscription_trial_kind
authentication 0006_user_kakao_id · 0007_user_popup_state
               0008_instagramloginstate_user_instagram_user_id · 0009_user_pending_email
analytics      0007·0008_funnelevent·0009
core           0018 · 0019 · 0020_seed_funnel_event_cleanup_job
integrations   0055 · home 0001
```

운영 env:

| 키 | 값 | 비고 |
|---|---|---|
| `AUTO_PRO_TRIAL_ENABLED` | **True** | 서버 ON. 실제 지급은 프론트 `autoProTrial` 이 켜져야 일어납니다 |
| `INSTAGRAM_LOGIN_ENABLED` | False | §2-③ |
| 카카오 3종 | 설정됨 | §1 |

엔드포인트 실호출(운영): `auto-grant` 401 · `popup-state` 401 · `me/email/` 401 ·
`funnel-event` **204** · `healthz` 200.

> ⚠️ **주기 잡 관련해 하나 고쳤습니다.** 운영에는 celery beat 가 없고 CF cron →
> `/internal/scheduler/tick` → `ScheduledJob` DB 로 돕니다. 퍼널 이벤트 정리 잡을
> `CELERY_BEAT_SCHEDULE` 에만 넣었으면 **운영에서 영영 안 돌 뻔했습니다.**
> `core 0020` 으로 시드했고 03:40 KST 로 잡힌 것까지 확인했습니다.

---

## 4. 구독 견적 버그 — ✅ 고쳤습니다 (원인이 **두 곳**이었습니다)

제보 정확했습니다. 그리고 파 보니 ②도 이미 깨져 있었습니다.

### 원인

| 곳 | 문제 |
|---|---|
| `preview_subscription` (attach_only) | `sub.renewal_amount`(= **저장된** 추가 계정 수, 자동 지급 직후엔 0)로 금액 계산. 요청한 개수는 응답에 되돌려주기만 함 |
| `confirm_billing` (attach_only) | 빌링키만 저장하고 **`extra_ig_accounts` 를 확정하지 않음** → 5개를 골라 카드를 등록해도 1개만 쓰이고 첫 결제도 정가 |

`attach_only` 는 원래 무카드 레퍼럴 전용의 드문 경로였는데, **카드 없는 프로 30일이
켜지면서 모든 자동 체험자가 지나가는 길**이 됐습니다. 그래서 이제 드러났습니다.

### ① 견적 — 요청 개수로 합산됩니다

```http
GET /api/v1/billing/subscription/preview/?plan_name=pro&extra_ig_accounts=4
```
```jsonc
{ "scenario": "attach_only", "extra_ig_accounts": 4, "extra_ig_account_price": 9900,
  "first_charge_amount": 54500,   // 14,900 + 4×9,900
  "recurring_amount": 54500 }
```

계산은 `UserSubscription.renewal_amount_for(extra)` **단일 소스**로 했습니다.
견적이 `정가 + N×단가` 로 따로 계산하면 **스냅샷 그랜드파더링·리텐션 할인·축소 예약**이
빠져 고지와 실청구가 또 갈립니다. 그 금액이 그대로 `PaymentConsent.disclosed_amount` 가
되므로 갈리는 순간 허위 고지입니다.

### ② confirm — 고른 개수가 확정됩니다

`POST /billing/toss/confirm/` 에 `extra_ig_accounts` 를 실으면(견적과 같은 수):

- `subscription.extra_ig_accounts` 가 그 수로 **확정**
- 체험 **기간은 불변** (카드를 붙여도 짧아지거나 길어지지 않음)
- 체험 중 증가분은 **0원** (`compute_extra_accounts_charge` — TRIALING → 0)
- **첫 결제(체험 종료)부터 총액 합산** → 54,500원

테스트로 고정했습니다: `apps/billing/test_auto_trial.py::TestAttachOnlyExtraAccounts`
(견적 합산 · confirm 확정+기간 불변 · 갱신 청구액 일치).

→ **"계정 1개로 등록 뒤 설정에서 늘리기" 안내 화면은 이제 걷으셔도 됩니다.**

### ③ 무카드 체험 중 `extra-accounts/` 400 — **의도가 맞습니다**

유지합니다. 카드 없이 계정을 늘릴 수 있으면 **5개를 0원으로 잡고 첫 결제 전에 사라질 수**
있습니다(체험 중 증가분이 0원이라). 추가 계정은 **카드 등록(confirm) 흐름으로만** 정해지고,
이제 그 경로가 실제로 동작하므로 ②가 유일한 길입니다.

참고로 카드를 **등록한 뒤**에는 체험 중에도 `extra-accounts/` 가 열립니다(0원 즉시 추가,
2026-08-21 결정). 즉 순서가 `confirm(카드+개수)` → 이후 변경은 `extra-accounts/` 입니다.

---

## 운영 현재 상태 요약

| 기능 | 서버 | 프론트 플래그 |
|---|---|---|
| 카카오 로그인 | ✅ **설정 완료** | 켜셔도 됩니다 |
| 자동 체험(카드 없는 프로 30일) | ✅ ON | `autoProTrial` 대기 |
| 퍼널 이벤트 | ✅ ON | `funnelBeacon` 대기 |
| 팝업 상태 · 이메일 등록 | ✅ ON | 플래그 없음 |
| 추가 계정 견적·확정 | ✅ **고침** | 안내 화면 철거 가능 |
| 인스타 로그인 | ⏸ OFF | dev 확인 후 한 줄 주세요 |

배포 검증: 커밋 `43ab213`, 전 컨테이너 동일 이미지(스큐 없음), web 3종 + celery 3종
**에러 로그 0건**, db·redis·pgbouncer 무중단, 스케줄러 기한 지난 잡 0건.
