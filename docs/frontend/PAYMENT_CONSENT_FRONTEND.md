# 결제 전 고지·동의 + 유료전환 2차 동의 — 백엔드 구현 회신

작성 2026-08-10 · 백엔드 → 프론트(TurnflowLink)
요청서: `backend-payment-consent.md`

**요청 전부 구현했습니다.** 1번은 확인만 필요한 항목이었고(답: **A**), preview 엔드포인트는
요청 안 하셔도 만들어 드렸습니다 — 프론트에서 날짜를 계산하는 코드가 남아 있는 것 자체가
재발 경로라서요. 2번은 5개 항목(플래그·저장 API·과금 분기·메일·소급 규모) 모두 배포 준비
완료입니다.

---

## ⚠️ 먼저 — 2차 동의 화면(모달)이 **언제 나오는지** 알려주세요

배포 시점(2026-08-10) prod 실측입니다:

| 구분 | 인원 | 비고 |
|---|---|---|
| **30일 초과 체험(쿠폰 44일) + 카드 등록** | **27명** | 월 합계 **402,300원** |
| 30일 체험 + 카드 | 7명 | 게이트 무관 |
| 카드 없는 체험 | 4명 | 대상 아님 |

이 27명은 **모달에서 동의하지 않으면 첫 결제가 되지 않고 무료로 전환**됩니다.

### 일정 — 모달이 **9월 초**까지는 나와야 합니다

| 날짜 | 무슨 일이 |
|---|---|
| **오늘** | 막히는 사람 0명 · 나간 메일 0건. 단 **1명은 이미 30일 창 안**이라 `conversion_consent_required=true` 로 내려가고 있습니다 |
| **8/23** | 첫 **D-14 안내 메일** 발송 (딥링크가 살아 있어야 함) |
| **9/06** | **첫 결제가 실제로 막히는 날** — 이때까지 모달이 없으면 이 사용자는 무료로 전환됩니다 |
| 9/06~9/30 | 나머지 26명이 순차로 도래 |

가장 빠른 5명의 첫 결제일: `09-06` · `09-09` · `09-11` · `09-12` · `09-13`.

모달이 없어도 **화면이 깨지지는 않습니다**(플래그를 무시하면 기존 동작 그대로). 손실은
9/06 부터 발생하기 시작합니다.

일정이 9월 초를 넘길 것 같으면 **미리 알려주세요** — 그때는 백엔드에서 차단을 끄고
(`CONVERSION_CONSENT_ENFORCE=False`) 동의 수집만 계속하다가, 모달이 나온 뒤 다시 켜는
순서로 가면 매출 손실 없이 넘길 수 있습니다.

일정이 밀릴 것 같으면 알려주세요 — 백엔드에 **긴급 킬스위치**(`CONVERSION_CONSENT_ENFORCE=False`)가
있어 차단만 끄고 동의 수집은 유지할 수 있습니다. 또 D-3 이내 미동의자가 생기면 운영
텔레그램으로 사전 경보가 오고, 실제로 무료 전환이 일어나면 건별 알림이 옵니다 —
조용히 매출이 사라지는 경로는 막아 뒀습니다.

---

## 요약 — 체크리스트 회신

| 요청 | 상태 | 결론 |
|---|---|---|
| **1.** 체험 계산 `+30일` 확인 | ✅ 확인 | **A 입니다.** `+30일` 고정 (달력 1개월 아님). 프론트 계산과 일치 |
| **1.** (요청 아님) preview 엔드포인트 | ✅ 추가 | `GET /billing/subscription/preview/` — 자체 계산 버리세요 |
| **2-1.** 구독 응답 필드 2개 | ✅ 완료 | `conversion_consent_required` · `conversion_consent_at` (+`trial_total_days`) |
| **2-2.** 동의 기록 저장 API | ✅ 완료 | `POST /billing/consents/` — **별도 엔드포인트 채택** (confirm 동봉 아님, 이유 §3) |
| **2-3.** 첫 결제 시 분기 | ✅ 완료 | 미동의 → **승인 호출 자체를 하지 않고** 무료 전환 + 데이터 보존 + 사후 메일 |
| **2-4.** D-14 / D-3 메일 | ✅ 완료 | 배치 `billing.notify_conversion_consent` (매일 10:30 KST) |
| **2-5.** 소급 대상 수 | ⚙️ 도구 제공 | `manage.py report_consent_backlog` — **prod 숫자는 배포 후 뽑아 회신** (§7) |
| **4-1.** 환불 조건 실제 기준 | ✅ 회신 | §8 에 판정 로직 전문 + 문안 초안 |
| **4-2.** 생년월일 6자리 전달 여부 | ✅ 회신 | **우리 서버에 오지 않습니다.** 근거 §9 |

---

## 1. 체험 계산 — **A. `+30일` 입니다** (1번 종결)

`apps/billing/toss_flows.py`:

```python
PERIOD_DAYS = 30       # 월간 구독 주기
TRIAL_BASE_DAYS = 30   # 프로 최초 카드 등록 무료 기간
...
trial_ends = now + timedelta(days=TRIAL_BASE_DAYS + bonus_days)
```

주기 전진도 `relativedelta` 가 아니라 순수 `+timedelta(days=30)` 입니다. 그래서
**8월 5일 가입 → 9월 4일 첫 결제**가 서버 실제값이고, 프론트 표기와 어긋나지 않습니다.
비례배분(proration) 분모도 같은 30일 고정이라 28~31일 달의 오차가 구조적으로 없습니다.

즉 **화면은 지금도 정확합니다.** 다만 아래 preview 로 옮겨 주세요 — 이유는 정확성이 아니라
**단일 소스**입니다: 지금은 프론트가 모르는 축이 두 개 있습니다.

1. **체험 자격**: `trial_used_at` 이 이미 있는 사용자는 재구독 시 체험 없이 **즉시 결제**입니다.
   프론트가 `오늘+30일`을 찍으면 그 사용자에게 거짓 고지가 됩니다.
2. **금액**: 추가 IG 계정·그랜드파더링 스냅샷가가 반영된 실제 청구액.

### `GET /api/v1/billing/subscription/preview/`

**부작용 없음** — 구독 행조차 만들지 않고(무료 구독 자동 생성도 안 함), 쿠폰을 소진하지 않고,
토스를 호출하지 않습니다.

```
GET /api/v1/billing/subscription/preview/?plan_name=pro&extra_ig_accounts=0&referral_code=OPTIONAL
Authorization: Bearer <access_token>
```

```jsonc
200 {
  "scenario": "trial",                 // trial | attach_only | charge_now
  "is_trial": true,
  "trial_days": 44,                    // 총 무료 일수 (기본 30 + 쿠폰 보너스)
  "trial_ends_at": "2026-09-23T14:02:11+09:00",  // 체험 종료 = 첫 결제 '시각'
  "trial_last_day": "2026-09-22",      // 체험 마지막 이용일 (표기용)
  "first_charge_at": "2026-09-23T14:02:11+09:00",
  "first_charge_amount": 14900,        // 부가세 포함
  "recurring_amount": 14900,           // 이후 매월
  "next_renewal_at": "2026-10-23T14:02:11+09:00",
  "plan": { "name": "pro", "display_name": "프로", ... },
  "extra_ig_accounts": 0,
  "extra_ig_account_price": 9900,
  "referral_code": "HLEVEL26",
  "referral_bonus_days": 14
}
```

#### ⚠️ 날짜 필드 3개 — 요청서 스펙과 의미가 하나 다릅니다

요청서에는 `"trial_ends_at": "2026-09-03"  // 체험 마지막 이용일` 로 적혀 있었는데,
서버의 `trial_ends_at` 은 **결제가 일어나는 시각 그 자체**입니다(기존
`referral/validate` 와 같은 의미 — `trial_ends_at == first_charge_at`). 이 값을 날짜로
찍어 "체험 마지막 날"로 쓰면 **하루 늘어나 보입니다.**

그래서 표기 전용으로 `trial_last_day`(= 첫 결제일 − 1일, KST 날짜)를 따로 넣었습니다.

| 필드 | 의미 | 화면 문구 |
|---|---|---|
| `trial_last_day` | 체험 **마지막 이용일** | "9월 22일까지 무료" |
| `first_charge_at` | 첫 결제 시각 (= 유료 전환) | "9월 23일 첫 결제 14,900원" |
| `trial_ends_at` | 체험 종료 시각 (= `first_charge_at`) | 표기에 쓰지 마세요 |

#### `scenario` 분기

| 값 | 상황 | 오늘 청구 |
|---|---|---|
| `trial` | 프로 최초 구독 → 무료 체험 시작 | ❌ |
| `attach_only` | 이미 체험 중 + 카드 등록 (기간 불변) | ❌ |
| `charge_now` | 베이직 구독 / **체험 소진 후 재구독** / 프로 직구매 | ✅ `first_charge_amount` |

`charge_now` 면 `trial_ends_at` · `trial_last_day` 가 `null`, `trial_days=0` 이고
`first_charge_at` 이 지금입니다. 고지 문구를 "오늘 즉시 결제"로 바꿔 주세요.

#### 에러

`400`(플랜 누락/무료 플랜/추가계정 오용/이미 유료 구독 중/쿠폰 무효) · `401` · `404`(플랜 없음).
전부 프로젝트 표준 포맷(`{success:false, error:{code,message,details}}`)입니다.

**견적이 실패하면 시트를 띄우지 않는다**는 §3 원칙에 동의합니다 — 금액을 모르는 상태의 동의는
동의가 아닙니다. 재시도만 허용하세요.

---

## 2. `conversion_consent_required` — 서버가 판정합니다 (2-1)

`GET /api/v1/billing/my-subscription/` 에 3개 필드 추가:

```jsonc
{
  // ...기존 필드
  "conversion_consent_required": true,   // 이 플래그만 보고 모달 렌더
  "conversion_consent_at": null,         // 동의받은 시각 (ISO) 또는 null
  "trial_total_days": 44                 // 이번 체험 총 일수 (쿠폰 보너스 포함)
}
```

판정 규칙 (요청서와 동일 + 한 조건 추가):

```
conversion_consent_required =
      status == 'trialing'
  AND 체험 길이 > 30일                       # (current_period_end - current_period_start)
  AND conversion_consent_at IS NULL
  AND 첫 결제까지 30일 이내                   # 시행령 30일 창
  AND 카드가 등록되어 있다                    # ← 추가
```

**추가한 조건 — 카드 보유**: 카드 없이 체험 중인 사용자(쿠폰 무카드 체험, prod 실측 9명)는
자동 유료전환 자체가 일어나지 않습니다(기간 만료 시 그냥 무료로 정리). 이들에게 모달을 띄우면
"결제되지 않게 하려면 동의하라"는 앞뒤가 안 맞는 요구가 되므로 대상에서 뺐습니다.

**30일 체험자는 항상 `false`** — 요청하신 대로입니다. 결제 화면의 동의로 요건이 충족됩니다.

**체험 길이의 기준점**은 `current_period_start`(체험 시작 트랜잭션이 세팅)입니다.
`trial_used_at` 은 "1인 1회" 어뷰징 방어용 내구 필드라 재체험 이력이 섞여 길이 계산에 못 씁니다.

**창(30일) 밖에서는 `false`** 입니다. 44일 체험의 D-0~D-13 에 미리 동의를 받으면 **그 동의가
다시 30일 창을 벗어나** 무효가 되니, 창에 들어온 뒤에만 모달이 뜹니다.

**체험을 새로 시작하면 초기화**됩니다(`conversion_consent_at = NULL`). 지난 체험의 동의가
다음 체험의 게이트를 통과시키면 안 되기 때문입니다.

> 판정 단일 소스: `apps/billing/consent.py`. 프론트 플래그 · 안내 메일 대상 · **과금 차단
> 게이트** 세 곳이 같은 함수를 씁니다 — 그래서 "모달은 떴는데 그냥 결제된다"가 구조적으로
> 발생하지 않습니다.

---

## 3. `POST /api/v1/billing/consents/` — **별도 엔드포인트로 갑니다** (2-2)

> "`toss/confirm` 에 동의 정보를 실어 보내는 방식이 더 편하면 그렇게 맞추겠습니다.
> **어느 쪽이 좋은지 알려주세요.**"

**별도 엔드포인트가 맞습니다.** 이유 두 가지 — 둘 다 증거로서의 성질에 관한 것입니다.

1. **순서가 뒤집힙니다.** 동의는 계약 체결(카드 등록) **전에** 성립해야 하는데, confirm 에
   실으면 기록 시각이 카드 등록 **이후**가 됩니다. 분쟁에서 타임스탬프만 남습니다.
2. **실패 시 증거가 사라집니다.** confirm 이 카드 거절·통신 오류로 실패하면 동의 기록도 함께
   날아갑니다. "무엇을 보고 동의했는가"는 결제 성공 여부와 독립적으로 남아야 합니다.

### 호출 순서

```
[고지 시트] 견적 표시 → 사용자가 3개 체크 + '동의하고 계속'
     ↓
POST /billing/consents/  { kind: "initial", ... }     ← 여기
     ↓
토스 SDK requestBillingAuth (카드 등록창)
     ↓
POST /billing/toss/confirm/  { authKey, plan_name, referral_code? }
```

`kind: "conversion"` 은 모달에서 동의 버튼을 누른 시점에 한 번 호출하면 됩니다.

### 요청 / 응답

```jsonc
POST /api/v1/billing/consents/
{
  "kind": "conversion",                          // "initial" | "conversion"
  "plan_name": "pro",
  "disclosed_first_charge_at": "2026-09-23",      // 화면에 표시한 첫 결제일
  "disclosed_amount": 14900,                      // 화면에 표시한 금액(부가세 포함)
  "disclosed_recurring_cycle": "monthly",         // 선택 (기본 monthly)
  "payment_method_type": "card",                  // 선택 (기본 card)
  "copy_version": "billingConsent@2026-08-10",
  "agreed_terms": true,
  "agreed_privacy": true,
  "agreed_recurring": true
}

201 {
  "id": "9f1c0b7a-...",
  "kind": "conversion",
  "plan_name": "pro",
  "disclosed_first_charge_at": "2026-09-23",
  "disclosed_amount": 14900,
  "disclosed_recurring_cycle": "monthly",
  "payment_method_type": "card",
  "copy_version": "billingConsent@2026-08-10",
  "agreed_terms": true, "agreed_privacy": true, "agreed_recurring": true,
  "consented_at": "2026-09-09T14:02:11+09:00",
  "applied_to_subscription": true          // ← conversion 이 실제로 게이트를 해제했는지
}
```

구현 편의 3가지:

- `disclosed_first_charge_at` 은 **ISO datetime 도 받습니다**(날짜부만 저장). preview 응답의
  `first_charge_at` 을 그대로 넘겨도 됩니다. 이 필드에서 400 을 내면 동의 기록이 남지 않으므로
  포맷 관용적으로 처리했습니다.
- `kind: "initial"` 은 **구독 행이 없는 신규 사용자도** 남길 수 있습니다.
- `applied_to_subscription` — `conversion` 인데 `trialing` 이 아니면 `false`(기록은 남지만
  게이트 해제는 없음). 정상 흐름에서는 `true` 입니다.

### 세 동의가 모두 `true` 여야 합니다 (하나라도 `false` → 400)

```jsonc
400 {
  "success": false,
  "error": {
    "code": 400,
    "message": "입력값을 확인해주세요.",
    "details": { "agreed_recurring": ["이 항목에 동의하지 않으면 동의 기록을 저장할 수 없습니다."] }
  }
}
```

공정위 지침상 명시적으로 동의하지 않은 항목은 '동의 없음'으로 처리해야 합니다. 일부만 체크된
상태를 저장하면 **그 기록 자체가 무효 증거**가 되므로 아예 받지 않습니다. 프론트에서는 3개 전부
체크되기 전까지 버튼을 비활성으로 두시면 이 400 은 보이지 않습니다.

### 서버가 함께 저장하는 것

동의 시각 · **고지한 금액과 첫 결제일**(사후 정책 변경과 무관하게 그 시점 값) · 결제수단 종류 ·
**동의 문구 버전** · 요청 IP · User-Agent · 세션/요청 식별자(`X-Request-ID`) · 동의 시점의 구독 행.

원장은 append-only 입니다 (`payment_consents` 테이블). 수정·삭제하지 않고, 동의 철회는 새 행이
아니라 구독 해지로 표현합니다.

---

## 4. 첫 결제 분기 — 미동의면 **승인 호출 자체를 하지 않습니다** (2-3)

가장 중요하다고 하신 항목입니다. `billing.charge_subscription_renewal` 진입부(`TXN 0`)에서
막습니다:

```
미동의 판정 (consent.blocks_first_charge)
  → 토스 승인 호출 없음
  → PaymentHistory 행도 만들지 않음        ← 아래 ⚠️
  → 빌링키 삭제 (동의 없는 결제수단을 보관할 근거가 없음)
  → 무료 플랜으로 전환
  → 페이지·DM 캠페인·설정·분석 기록·IG 연동 전부 보존 (재구독 시 복원)
  → 사후 안내 메일 발송
```

> ⚠️ **PENDING 주문 행을 만들지 않는 것이 중요합니다.** 만들어두면 토스에 존재하지 않는
> `orderId` 를 `reconcile_pending_payments` 가 조회해 **FAILED 로 확정**하고, 사용자에게는
> "결제 실패"로 보입니다. 실제로는 우리가 승인을 시도하지 않은 것이므로 결제 내역에 아무 흔적도
> 남지 않아야 맞습니다.

락 안에서 **재검증**합니다 — 배치가 도는 그 순간 사용자가 동의 버튼을 눌렀다면 무료 전환을
취소하고 정상 결제로 갑니다.

**정기 갱신은 절대 막지 않습니다.** 게이트는 `status == trialing` 인 첫 과금에만 적용되며,
유료 전환 후의 매월 갱신은 최초 동의가 유효합니다. (회귀 테스트로 고정해 뒀습니다.)

### 사후 안내 메일

새 템플릿 `consent_missing_downgrade`:

> **결제하지 않고 무료 플랜으로 전환했어요**
> 유료 전환에 대한 동의가 확인되지 않아 결제를 진행하지 않았습니다. …
> 청구된 금액 **0원 (결제 없음)** / 현재 플랜 **무료**
> **데이터는 그대로 보관돼 있습니다.** 페이지·DM 캠페인·설정·분석 기록 모두 남아 있어,
> 다시 구독하시면 이전 상태로 이어서 쓰실 수 있어요.
> [다시 시작하기]
> 등록하셨던 결제 카드 정보는 삭제했습니다.

---

## 5. D-14 / D-3 메일 (2-4)

배치: `billing.notify_conversion_consent` — **매일 10:30 KST** (`ScheduledJob` 시드 완료).

| 시점 | 대상 | 멱등 마커 |
|---|---|---|
| D-14 | `conversion_consent_required` && 미동의 | `conversion_consent_notice_sent_at` |
| D-3 | 위와 같고 아직 미동의 | `conversion_consent_reminder_sent_at` |

- 발송 시점은 `CONVERSION_CONSENT_NOTICE_DAYS` / `..._REMINDER_DAYS` **환경변수**입니다.
  회의에서 확정되면 코드 수정 없이 바꿉니다 (30일 창 안이면 자유).
- 배치가 하루 1회라 정확히 D-14 를 놓칠 수 있어 **"N일 이하 남았고 아직 안 보냄"** 으로
  판정합니다 — 늦게라도 한 번은 나갑니다. 두 마커가 독립이라 D-14 를 놓쳐도 D-3 은 나갑니다.
- **마케팅 수신동의(`marketing_opt_in`)와 무관하게 발송**합니다. 결제 예정 고지는 거래
  정보성 메일입니다.

### 이메일 클릭을 동의로 처리하지 않습니다 — 동의의 정의에 전적으로 동의합니다

메일은 **알림·유입 경로**이고 동의는 앱 화면의 버튼(`POST /billing/consents/`)으로만 성립합니다.
그래서 CTA 라벨도 "동의하기"가 아니라 **"동의 화면 열기"** 로 썼고, 본문에 이 문장을 넣었습니다:

> 이 메일은 안내용이며, 동의는 위 화면에서 직접 확인·선택하셔야 완료됩니다.

열람 추적·클릭 추적을 동의 판정에 쓰는 코드는 없습니다.

### 딥링크 — 경로 알려주세요

현재 기본값: `{FRONTEND_URL}/billing/consent` (환경변수 `CONVERSION_CONSENT_PATH`).
**프론트에서 경로가 확정되면 알려주세요** — 환경변수 한 줄 교체로 반영합니다(재배포 불필요).
비로그인 진입 시 로그인 모달 자동 오픈은 기존 `/support/tickets/:id` 와 같은 방식으로
프론트에서 처리하시면 됩니다.

---

## 6. 새 템플릿 2개 — 어드민에서 문구 수정 가능

| 키 | 용도 |
|---|---|
| `conversion_consent` | D-14 / D-3 동의 요청 (변수 `days_left` 로 두 시점 구분) |
| `consent_missing_downgrade` | 미동의 무료 전환 사후 안내 |

이메일 템플릿은 **DB 에 저장**되어 어드민 콘솔에서 편집할 수 있습니다. 사용 가능한 변수 목록도
함께 등록돼 있습니다(`conversion_consent`: `full_name` · `plan_name` · `amount_str` ·
`first_charge_date` · `days_left` · `consent_url` …).

---

## 7. 소급 대상 (2-5) — 규모 산출 도구 + 정책 스위치

**prod 숫자는 배포 후 뽑아 별도로 회신합니다.** dev DB 에는 표본이 1건뿐이라 의미가 없습니다.

```bash
python manage.py report_consent_backlog --since 2026-08-10 --list
```

읽기 전용이며 6개로 분해합니다:

| 버킷 | 의미 | 조치 |
|---|---|---|
| ① 30일 초과 체험 + 미동의 | 쿠폰 연장 체험 | **배포만으로 게이트 대상** — D-14/D-3 메일 자동 발송 |
| ② 30일 체험 + 동의 기록 없음 | 새 화면 이전 가입자 | **이 숫자를 보고 정책 결정** (아래) |
| ③ 배포 이후 가입인데 `initial` 기록 없음 | 프론트 연결 누락 의심 | 0이어야 정상 — 모니터링용 |
| ④ 30일 체험 + 결제화면 동의 있음 | 조치 불필요 | — |
| ⑤ 이미 2차 동의 완료 | — | — |
| ⑥ 카드 미등록 체험 | 자동 전환 없음 | 대상 아님 |

### ② 를 게이트에 넣는 것은 **스위치로 분리**했습니다 (기본 꺼짐)

`CONVERSION_CONSENT_REQUIRE_ALL_TRIALS=False` (기본).

켜면 동의 기록이 없는 30일 체험자도 첫 결제가 차단되고 무료로 전환됩니다. 켜는 즉시 이탈로
직결되는 결정이라, 요청서 §2-5 의 "규모에 따라 처리 방식이 달라진다"는 판단을 위해 **메커니즘은
완성해 두고 스위치는 꺼둔** 상태입니다. ② 숫자를 회신드린 뒤 결정하시면 환경변수 한 줄로
켭니다. 켜면 그 인원에게도 D-14/D-3 메일이 자동으로 나갑니다.

> ③ 이 0 이 아니면 프론트의 `kind:"initial"` 호출이 어딘가에서 빠진 것입니다 — 배포 후 이 숫자를
> 같이 봐 주세요.

---

## 8. 환불 조건·절차 실제 판정 기준 (4-1)

`GET /billing/refund-eligibility/` 의 판정은 `apps/billing/payment_views.check_refund_eligibility`
이고, **아래 중 하나라도 걸리면 `eligible: false`** 입니다. `reasons[]` 에 사람이 읽을 문장으로
내려갑니다.

| # | 조건 | 판정 |
|---|---|---|
| 1 | **결제 후 7일 경과** (`REFUND_WINDOW_DAYS = 7`) | 불가 |
| 2 | 결제 이후 **페이지를 1개 이상 생성** | 불가 |
| 3 | 결제 이후 **AI 생성을 1회 이상 사용** | 불가 |
| 4 | **배지(로고) 제거**를 1개 이상 페이지에 적용 (유료 전용 기능) | 불가 |
| 5 | 결제 이후 **DM 200건 초과 발송** (무료/베이직 한도 초과 = 프로 전용 기능 사용) | 불가 |
| 6 | (프로) **IG 계정 2개 이상 연동** | 불가 |
| 7 | (프로) **스팸 댓글 필터 사용 중** | 불가 |

기준 시점은 **'해당 결제 시점(`paid_at`)'** 입니다 — 체험 기간의 사용은 무료 제공분이라 심사
대상이 아닙니다. 커스텀 CSS 는 무료 플랜도 쓸 수 있어 심사하지 않습니다.

### 고지 화면 문안 초안 (그대로 쓰셔도 됩니다)

> **환불 조건**
> 결제일로부터 **7일 이내**에, 결제 후 유료 기능을 사용하지 않은 경우 전액 환불됩니다.
> 유료 기능 사용에는 페이지 추가 생성, AI 콘텐츠 생성, 배지(로고) 제거, 월 200건을 초과한
> DM 발송, 인스타그램 계정 2개 이상 연동, 스팸 댓글 필터 사용이 포함됩니다.
> 환불 가능 여부는 결제 내역 화면에서 즉시 확인할 수 있습니다.
>
> **해지**
> 언제든 해지할 수 있고, 해지해도 **이미 결제한 기간이 끝날 때까지** 유료 기능을 그대로
> 사용합니다. 그 이후 자동으로 무료 플랜으로 전환되며 추가 청구는 없습니다.
> 페이지·캠페인·설정 데이터는 **기간 제한 없이 보관**되어, 다시 구독하면 이어서 사용할 수 있습니다.

두 번째 블록의 근거: 해지는 즉시 환불이 아니라 `cancelled` 예약(기간 만료 시 다운그레이드)이고,
데이터 무기한 보존은 확정된 정책입니다(`CANCEL_RETENTION_FRONTEND.md`).

---

## 9. 토스 생년월일 6자리 / 사업자번호 10자리 (4-2)

**우리 서버·로그·분석 도구로 오지 않습니다.** 근거 3단계:

1. **요청 본문에 없습니다.** 운영 경로는 토스 SDK 가 준 `authKey` 로 빌링키를 발급받는
   `POST /v1/billing/authorizations/issue` 이고, 우리가 보내는 body 는 `{authKey, customerKey}`
   **둘뿐**입니다. 생년월일은 토스 결제창(iframe) 안에서 입력되어 토스↔카드사 사이에서만 쓰입니다.
2. **응답에 없습니다.** 발급 응답은 `{billingKey, customerKey, cardCompany, cardNumber(마스킹),
   card{...}}` 이고, 우리가 저장하는 것은 **카드사명 + 마스킹된 번호** 둘뿐입니다
   (`card_company` · `card_number_masked`). `customerIdentityNumber` 계열 필드는 응답에 오지
   않고, 오더라도 저장 코드가 없습니다.
3. **로그에 없습니다.** 토스 클라이언트는 요청 본문을 로깅하지 않습니다(메서드·마스킹된 경로·
   에러 코드만). 빌링키가 승인 URL path 에 들어가므로 httpx 로거는 WARNING 으로 고정해 뒀습니다.

> 하나의 예외: 카드번호를 직접 입력해 빌링키를 발급하는 **dev 전용 헬퍼**
> (`POST /billing/toss/dev/issue-billing-key/`)에는 `customer_identity_number` 파라미터가 있습니다.
> `TOSS_DEV_CARD_AUTH_ENABLED` 게이트 뒤에 있고 **운영은 `False`** 입니다(라이브에서는 별도
> 계약 + PCI-DSS 가 필요해 애초에 호출 불가). 저장하지 않고 토스로 전달만 하며, 로깅도 없습니다.

결론: 주민등록번호 처리에 해당하는 데이터가 우리 시스템에 들어오지 않으므로, 그에 따른 별도
법적 근거·보안조치 점검은 필요하지 않습니다.

---

## 10. §3 의 아직 안 붙인 경로 — 백엔드 작업 없음, 확인했습니다

| 경로 | 사용 API | 상태 |
|---|---|---|
| 업그레이드 (베이직→프로) | `POST /billing/change-plan/preview/` → `change-plan/` | 그대로 사용 가능 |
| 다운그레이드 | `change-plan/` (예약) | 동일 |
| 추가 IG 계정 증감 | `extra-accounts/preview/` → `extra-accounts/` | 동일 |
| 미납 해소 카드 변경 | `toss/prepare/` → `toss/confirm/` | 동일 |
| 재개 / 리텐션 할인 | `billing/resume/` · `retention-offer/apply/` | 동일 |

`ProrationBox` 의 "그대로 진행하면 실제 비례배분 금액으로 결제됩니다" 문구를 고지 화면에서 쓸 수
없다는 지적에 동의합니다 — `change-plan/preview/` 가 확정 금액을 주므로 그 값을 표기하시고,
견적 실패 시 시트를 띄우지 마세요.

---

## 11. 마이그레이션 / 배포 메모 (백엔드 내부)

| 항목 | 값 |
|---|---|
| 마이그레이션 | `billing 0025`(구독 필드 3개 + `payment_consents` 테이블) · `emails 0008`(템플릿 키 2개) · `core 0014`(`ScheduledJob` 시드) |
| 이메일 템플릿 | `python manage.py seed_email_templates` 필요 (신규 2건 생성) |
| 신규 주기잡 | `billing-notify-conversion-consent` (매일 10:30 KST, `billing` 큐) |
| 새 환경변수 | `CONVERSION_CONSENT_NOTICE_DAYS=14` · `CONVERSION_CONSENT_REMINDER_DAYS=3` · `CONVERSION_CONSENT_PATH=/billing/consent` · `CONVERSION_CONSENT_REQUIRE_ALL_TRIALS=False` · **`CONVERSION_CONSENT_ENFORCE=True`**(긴급 킬스위치 — 차단만 끔) (전부 기본값 있음 — 설정 없이도 동작) |
| 운영 알림 | ①D-3 이내 미동의자 발생 시 사전 경보 ②실제 무과금 무료 전환 시 건별 알림 (둘 다 텔레그램) |
| 테스트 | `apps/billing/test_payment_consent.py` 36건 (게이트 제거 시 실패하는지 역검증 완료) |
| 판정 단일 소스 | `apps/billing/consent.py` |

---

궁금한 점이나 필드명·응답 형태 조정이 필요하면 알려 주세요. `trial_last_day` 처럼 의미가
갈릴 수 있는 부분은 특히 먼저 맞춰두는 게 좋겠습니다.
