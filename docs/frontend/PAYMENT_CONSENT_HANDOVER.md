# [프론트 전달] 결제 동의 — 최종 결정 + dev 테스트 계정

작성 2026-08-10 · 백엔드 → 프론트(TurnflowLink)
상세 계약: [`PAYMENT_CONSENT_FRONTEND.md`](PAYMENT_CONSENT_FRONTEND.md)

---

## 1. 결정 — **동의는 결제 화면 1회. 2차 동의 모달은 만들지 마세요**

제품 회의 결정입니다. 첫 결제 45일 전에 다시 동의를 받게 하면 **리텐션이 떨어지고**, 현재
44일 쿠폰 대상이 지인 범위라 2차 동의를 요구하지 않기로 했습니다.

**이미 prod 에 배포·반영 완료**입니다(2026-08-10). 프론트에서 취소할 작업:

| 항목 | 상태 |
|---|---|
| **2차 동의 모달** | ❌ **작업 취소** |
| `conversion_consent_required` 분기 | ❌ 넣지 마세요 — 필드는 남아 있지만 **항상 `false`** |
| `kind:"conversion"` 호출 | ❌ 불필요 |
| 동의 화면 딥링크 경로 회신 | ❌ 불필요 |
| D-14 / D-3 안내 메일 | ❌ 서버에서 발송 중지됨 |

**남는 작업은 2개뿐입니다:**

1. **결제 전 고지 시트** — `GET /billing/subscription/preview/` 값을 그대로 표기.
   `buildPaymentNotice.ts` 의 `TRIAL_DAYS` 상수와 `addDays` 계산을 지우세요.
2. **동의 3개 체크 → `POST /billing/consents/` (`kind:"initial"`)** → 토스 SDK → `toss/confirm`.

서버 동작: 44일 쿠폰 체험자도 **추가 절차 없이 첫 결제일에 정상 유료전환**됩니다.

> 참고: 44일 쿠폰을 **일반 마케팅에 여는 시점**에는 대상이 지인이 아니게 되어 시행령
> §20-2(전환 전 30일 이내 동의)가 다시 이슈가 됩니다. 그때는 백엔드 플래그 하나로 2차 동의
> 파이프라인이 되살아나므로, 프론트도 그 시점에 모달을 붙이면 됩니다. **지금은 불필요.**

---

## 2. 붙일 API 2개 (요약)

### ① 결제 전 고지 — `GET /api/v1/billing/subscription/preview/`

```
GET /api/v1/billing/subscription/preview/?plan_name=pro&extra_ig_accounts=0&referral_code=OPTIONAL
Authorization: Bearer <access_token>
```

부작용 없음(구독 행 생성·쿠폰 소진·토스 호출 전부 없음). 실패하면 시트를 띄우지 말고 재시도만
허용해 주세요 — 금액을 모르는 상태의 동의는 동의가 아닙니다.

**⚠️ 날짜 필드 3개 구분** (여기서 하루가 어긋납니다):

| 필드 | 의미 | 화면 문구 |
|---|---|---|
| `trial_last_day` | 체험 **마지막 이용일** | "9월 22일까지 무료" |
| `first_charge_at` | 첫 결제 시각 (= 유료 전환) | "9월 23일 첫 결제 14,900원" |
| `trial_ends_at` | 체험 종료 시각 (= `first_charge_at`) | **표기에 쓰지 마세요** |

**`scenario` 3분기**:

| 값 | 상황 | 오늘 청구 | 고지 문구 |
|---|---|---|---|
| `trial` | 프로 최초 구독 | ❌ | "N일 무료 후 첫 결제" |
| `attach_only` | 이미 체험 중 + 카드 등록 | ❌ | 기간 불변 |
| `charge_now` | 베이직 / 체험 소진 후 재구독 | ✅ | **"오늘 즉시 결제"** |

`charge_now` 면 `trial_ends_at`·`trial_last_day` 가 `null`, `trial_days=0` 입니다.

### ② 동의 기록 — `POST /api/v1/billing/consents/`

**토스 SDK 호출 직전**에 부르세요(동의는 계약 체결 전에 성립해야 하고, confirm 이 실패해도
기록이 남아야 합니다).

```jsonc
POST /api/v1/billing/consents/
{
  "kind": "initial",                            // conversion 은 쓰지 않습니다
  "plan_name": "pro",
  "disclosed_first_charge_at": "2026-09-23",     // preview 의 first_charge_at (ISO datetime 도 허용)
  "disclosed_amount": 14900,                     // preview 의 first_charge_amount
  "disclosed_recurring_cycle": "monthly",
  "payment_method_type": "card",
  "copy_version": "billingConsent@2026-08-10",   // 문구 바꾸면 이 값도 바꿔주세요
  "agreed_terms": true,
  "agreed_privacy": true,
  "agreed_recurring": true
}
→ 201
```

**세 동의가 전부 `true` 여야 201**, 하나라도 `false` 면 400(`details` 에 필드별 사유). 3개 다
체크되기 전에는 버튼을 비활성으로 두면 이 400 은 보이지 않습니다.

IP·User-Agent·요청 ID·시각은 서버가 자동 기록합니다.

---

## 3. dev 테스트 계정 (5개)

| | 주소 |
|---|---|
| **테스트 화면** | `https://turnflow.link.ngrok.dev/home` (dev 백엔드를 바라봅니다) |
| dev API | `https://dev-api.turnflow.link` — **끝슬래시 필수** |
| 비밀번호 | 5개 계정 전부 **`TestPass1234!`** |

이 오리진은 dev 백엔드의 CORS·CSRF·ALLOWED_HOSTS 에 이미 등록돼 있어 추가 설정이 필요
없습니다(확인 완료). 화면에서 그냥 로그인해서 쓰시면 됩니다.

```
POST /api/v1/auth/login/   { "email": "...", "password": "TestPass1234!" }
```

> dev 백엔드는 **사무실 PC 로컬 도커 + Cloudflare 터널**입니다. `dev-api` 가 502 면 서버가
> 죽은 게 아니라 그 PC 의 컨테이너가 내려간 것이니 백엔드에 알려주세요.

| 이메일 | 초기 상태 | `preview` 결과 | 검증 포인트 |
|---|---|---|---|
| `billing-new@test.com` | free · 체험 미사용 | `trial` / 30일 / 14,900 | 기본 체험 고지 |
| `billing-coupon@test.com` | free · 체험 미사용 | `trial` / **44일** / 14,900 | `?referral_code=HLEVEL26` — 쿠폰 연장 표기 |
| `billing-used@test.com` | free · **체험 소진** | `charge_now` / 0일 / 14,900 | **"오늘 즉시 결제"** 문구 분기 |
| `billing-trialing@test.com` | **pro 체험 중 + 카드** | `attach_only` / 30일 | 체험 중 화면·기간 불변 |
| `billing-basic@test.com` | free | `charge_now` / 0일 / **4,900** | 베이직 즉시결제 |

전부 실제 HTTP 로 검증한 값입니다. 상태를 되돌리려면 백엔드에 말씀해 주세요
(`python manage.py seed_billing_test_accounts --reset`).

### 카드 등록까지 테스트하려면

dev 는 토스 **테스트 키**(`test_sk_...`)라서 실제 청구가 발생하지 않습니다. 두 가지 방법:

**(A) 프론트 SDK 정상 경로** — `toss/prepare/` → SDK `requestBillingAuth` → `toss/confirm/`.
토스 테스트 카드로 진행하면 됩니다.

**(B) SDK 없이 서버만** (`TOSS_DEV_CARD_AUTH_ENABLED=True`, dev 전용):

```jsonc
POST /api/v1/billing/toss/dev/issue-billing-key/
{
  "card_number": "4330123412341234",
  "card_expiration_year": "30",
  "card_expiration_month": "12",
  "customer_identity_number": "990101",
  "plan_name": "pro",
  "referral_code": ""            // 쿠폰 테스트 시 "HLEVEL26"
}
```
테스트 키에서는 앞 6자리(BIN)만 유효하면 등록됩니다. **운영에는 이 엔드포인트가 닫혀 있습니다.**

### 쿠폰 코드

dev 활성 코드: **`HLEVEL26`** (+14일) · `7QK9X2` (+14일). 총 체험 = 30 + 14 = **44일**.
표기는 `preview` 의 `trial_days`(=44)를 쓰세요 — 보너스분(14)만 노출하면 혜택이 1/3로
축소돼 보입니다.

---

## 4. 참고 — 앞선 회신에서 이미 답한 것

| 질문 | 답 |
|---|---|
| 체험이 `+30일`인가 달력 1개월인가 | **`+30일` 고정**. 주기 전진도 동일 → 프론트 계산과 어긋나지 않음 |
| 동의 저장을 `toss/confirm` 에 동봉? | **별도 엔드포인트**. 체결 전 성립 + confirm 실패 시 증거 보존 |
| 환불 조건 실제 기준 | 결제 후 **7일 이내 + 유료기능 미사용**(7개 조건). 문안 초안은 상세 문서 §8 |
| 토스 생년월일 6자리가 우리 서버로 오나 | **오지 않습니다** (요청 body `{authKey, customerKey}` 뿐, 응답·로그에도 없음) |
| §3 의 나머지 경로(업그레이드·추가계정 등) | 기존 preview API 재사용, 백엔드 추가 작업 없음 |

궁금한 점 있으면 알려주세요.
