# 쿠폰(제휴/레퍼럴 코드) 프론트엔드 변경 요청 — 2026-08-04

> **대상:** 프론트엔드 개발자
> **성격:** ⚠️ **Breaking change** — 기존에 쓰던 엔드포인트 1개가 폐지됩니다
> **배경:** 14일 쿠폰 사용자가 `30일 + 14일 = 44일` 이 아니라 **14일만** 받는 결함이 실서비스에서 발생

---

## 0. 3줄 요약

1. **`POST /billing/referral/redeem/` 는 폐지됐습니다 — 항상 400.** 호출부를 제거해주세요.
2. 쿠폰은 **카드 등록에 동봉**합니다 → `POST /billing/toss/confirm/` 의 `referral_code` 필드.
3. `GET /billing/referral/validate/` 가 **결제 전 미리보기 정보**(총 무료일수·첫 결제일·첫 결제금액)를
   내려주도록 확장됐습니다. 쿠폰 입력 즉시 "이 쿠폰으로 결제하면 어떻게 되는지" 를 보여주세요.

---

## 1. 무슨 일이 있었나 (원인)

쿠폰으로 무료 체험을 시작하는 경로가 **두 개** 있었고, 서로 **다른 일수**를 부여했습니다.

| 경로 | 서버 계산 | 14일 쿠폰 결과 |
|---|---|---|
| `POST /billing/toss/confirm/` + `referral_code` (카드 등록 동봉) | `기본 30일 + 코드 보너스` | **44일** ✅ |
| `POST /billing/referral/redeem/` (카드 없이 쿠폰만) | `코드 보너스`만 | **14일** ❌ |

무카드 경로가 **기본 체험 30일을 가산하지 않았습니다.** 프론트는 자기 기준으로는 일관됐습니다 —
`validate` 응답의 `trial_days`(=14, 보너스분)를 표시하고 실제로 14일을 받았으니까요.
어긋난 건 **백엔드의 두 엔드포인트 계약**이었습니다. 그래서 경로를 하나로 없앴습니다.

실제 피해: `HLEVEL26`(14일 쿠폰) 사용자 17명 중 **카드 등록한 14명은 44일 정확**,
**카드 없이 쿠폰만 쓴 3명은 14일**.

### 부수적으로 닫힌 구멍

무카드 경로는 `trial_used_at` 을 채우지 않았습니다. 그래서 체험이 만료돼 free 로 내려간 뒤
카드를 등록하면 서버가 "체험 미사용자"로 재판정해 **30일 무료 체험을 한 번 더** 줬습니다
(1인 1회 원칙 우회). 경로 폐지로 이 구멍도 함께 닫혔습니다.

---

## 2. 변경된 API 계약

### 2-1. `GET /api/v1/billing/referral/validate/?code=XXX` — 확장 (하위호환 O)

인증 불필요. 기존 필드는 **그대로 유지**되고 5개가 추가됐습니다.

```jsonc
// GET /api/v1/billing/referral/validate/?code=HLEVEL26   (14일 쿠폰)
{
  "valid": true,

  // ── 기존 필드 (변경 없음) ──
  "trial_days": 14,             // ⚠️ 보너스분만. 이 값을 사용자에게 노출하지 마세요
  "base_trial_days": 30,        // 코드 없이도 받는 기본 무료일
  "total_trial_days": 44,       // ✅ 화면에 쓸 "총 무료 일수"
  "plan": { "name": "pro", "display_name": "프로", "monthly_price": 14900, /* ... */ },

  // ── 신규 필드 (결제 전 미리보기) ──
  "requires_card": true,                      // 항상 true. 무카드 경로는 없음
  "trial_ends_at": "2026-09-17T07:25:13Z",    // 무료 체험 종료 시각
  "first_charge_at": "2026-09-17T07:25:13Z",  // 첫 결제 시각 (= 체험 종료 시각)
  "first_charge_amount": 14900,               // 첫 결제 금액(원)
  "extra_ig_account_price": 9900              // 추가 IG 계정 1개당 월 단가(원)
}
```

**무효한 코드일 때** (HTTP 200 유지, `valid: false`):

```jsonc
{ "valid": false, "reason": "유효 기간이 만료된 코드입니다." }
// → valid=false 면 위 미리보기 필드는 아예 내려오지 않습니다. 반드시 valid 로 분기하세요.
```

`reason` 은 사용자에게 그대로 보여줄 수 있는 한국어 문장입니다
(`존재하지 않는 코드입니다.` / `비활성화된 코드입니다.` / `아직 사용할 수 없는 코드입니다.` /
`유효 기간이 만료된 코드입니다.` / `사용 횟수가 모두 소진된 코드입니다.`).

> ⚠️ `trial_ends_at` / `first_charge_at` 은 **호출 시점(now) 기준 추정치**입니다.
> 확정값은 confirm 응답의 `subscription.current_period_end` / `first_charge_at` 입니다.
> 미리보기 안내에는 추정치를 쓰고, 완료 화면에는 confirm 응답값을 쓰세요.

> ⚠️ `first_charge_amount` 는 **플랜 단가만** 입니다. 추가 IG 계정을 함께 구매하면
> `first_charge_amount + extra_ig_account_price × 개수` 가 실제 첫 결제 금액입니다.

### 2-2. `POST /api/v1/billing/referral/redeem/` — ⛔ 폐지

**항상 HTTP 400.** 구독 상태를 전혀 건드리지 않고, 코드 사용 횟수도 태우지 않습니다.

```jsonc
{
  "detail": "쿠폰은 카드 등록과 함께 사용해야 합니다. 결제 수단을 등록하면 무료 체험이 시작됩니다.",
  "code": "REFERRAL_REQUIRES_CARD"
}
```

`detail` 은 그대로 토스트에 띄워도 되는 문장입니다(구버전 앱이 남아 있어도 사용자에게
말이 되는 안내가 나가도록 의도한 것). `code` 로 분기하고 싶으면 `REFERRAL_REQUIRES_CARD` 를 쓰세요.

### 2-3. `POST /api/v1/billing/toss/confirm/` — 변경 없음 (여기로 쿠폰 동봉)

```jsonc
{
  "auth_key": "<successUrl 쿼리로 받은 authKey>",
  "plan_name": "pro",            // 쿠폰은 pro 최초 구독에만 유효
  "referral_code": "HLEVEL26",   // ← 쿠폰을 여기 동봉
  "extra_ig_accounts": 0
}
```

성공 응답(요약):

```jsonc
{
  "detail": "무료 체험이 시작되었습니다. 체험 종료 후 첫 결제가 진행됩니다.",
  "scenario": "trial",
  "first_charge_at": "2026-09-17T07:25:13Z",
  "subscription": { "status": "trialing", "current_period_end": "2026-09-17T07:25:13Z", /* ... */ }
}
```

쿠폰 관련 400 사유(`detail` 그대로 노출 가능):

| detail | 언제 |
|---|---|
| `이미 제휴/레퍼럴 코드를 사용하셨습니다.` | 1인 1회 위반 |
| `존재하지 않는 제휴 코드입니다.` | 오타 |
| `제휴 코드는 프로 플랜 최초 구독(무료 체험 시작) 시에만 사용할 수 있습니다.` | basic 선택 / 이미 체험 소진 / 카드변경 경로 |
| `비활성화된 코드입니다.` 등 | `validate` 와 동일한 검증 사유 |

> 💡 그래서 **결제창 띄우기 전에 `validate` 를 반드시 통과시키세요.** 카드 등록까지 다 하고
> 마지막에 쿠폰 때문에 400 이 나면 사용자 경험이 최악입니다.

---

## 3. 프론트 수정 대상 (파일 단위)

### 3-1. `src/lib/billing-api.ts`

```diff
-/** POST /api/v1/billing/referral/redeem/ — 레퍼럴 코드로 트라이얼 시작 */
-export function redeemReferralCode(code: string) {
-  return api.post<ReferralRedeemResponse>('/api/v1/billing/referral/redeem/', { code });
-}
```

→ **함수 삭제.** 쿠폰은 `confirm` 요청 바디에 실어 보내면 됩니다.

### 3-2. `src/app/types/billing.ts`

```diff
 export interface ReferralValidateResponse {
   valid: boolean;
   reason?: string;
-  trial_days?: number;
+  /** 보너스분만 — 사용자 노출 금지. 표기는 total_trial_days 사용 */
+  trial_days?: number;
+  base_trial_days?: number;
+  /** 총 무료 일수 = base + 보너스. 화면 표기는 이 값 */
+  total_trial_days?: number;
+  requires_card?: boolean;
+  /** 무료 체험 종료 시각(ISO) — 호출 시점 기준 추정치 */
+  trial_ends_at?: string;
+  /** 첫 결제 시각(ISO) = trial_ends_at */
+  first_charge_at?: string;
+  /** 첫 결제 금액(원) — 플랜 단가만. 추가 계정은 별도 가산 */
+  first_charge_amount?: number;
+  /** 추가 IG 계정 월 단가(원) */
+  extra_ig_account_price?: number;
   plan?: SubscriptionPlan;
 }
```

`ReferralRedeemResponse` 타입은 이제 쓰이지 않습니다(삭제 가능).

### 3-3. `src/app/components/PricingDialog.tsx`

- `handleRedeemReferral()` 제거 → 쿠폰 "적용" 버튼은 **결제 플로우를 시작**하도록 변경
  (`referral_code` 를 successUrl 쿼리에 실어 결제창 → 콜백에서 confirm 에 동봉).
  기존에 이미 `successUrl: .../billing-success?plan=pro&code=${referralCode}` 패턴이 있으니
  그 경로를 재사용하면 됩니다 (`TOSS_BILLING_FRONTEND.md` §2 참고).
- 검증 결과 표기를 `trial_days` → `total_trial_days` 로 교체:

```diff
- {t('pricing.referral.valid', { plan: ..., days: refValidation.trial_days })}
+ {t('pricing.referral.valid', { plan: ..., days: refValidation.total_trial_days })}
```

> 이게 **가장 중요한 한 줄**입니다. 지금은 "14일 무료" 로 보이지만 실제로는 44일을 받습니다.
> 혜택을 3분의 1로 축소해 보여주고 있는 셈입니다.

### 3-4. `src/app/pages/settings/SettingsPage.tsx`

동일 (`validateReferralCode` 는 유지, `redeemReferralCode` 호출 제거, 표기 필드 교체).

---

## 4. 요청하신 UX — "쿠폰 입력하고 결제하면 무슨 일이 일어나는지" 사전 안내

쿠폰 입력 → `validate` 통과 시, **카드 입력 전에** 아래를 보여주세요. 필요한 값은 전부
`validate` 응답에 있습니다(프론트에서 날짜·금액 계산할 필요 없음).

```
┌─────────────────────────────────────────────┐
│ ✅ 쿠폰 HLEVEL26 적용됨                      │
│                                             │
│ 프로 플랜 44일 무료                          │
│ (기본 30일 + 쿠폰 14일)                      │
│                                             │
│ • 지금 결제되는 금액        0원              │
│ • 무료 체험 종료           2026년 9월 17일   │
│ • 첫 결제 예정             2026년 9월 17일   │
│                            14,900원         │
│                                             │
│ 체험 기간 중에는 한 번도 청구되지 않습니다.   │
│ 종료일 전에 해지하면 요금이 청구되지 않습니다.│
│                                             │
│           [ 카드 등록하고 시작하기 ]         │
└─────────────────────────────────────────────┘
```

필드 매핑:

| 화면 | 필드 |
|---|---|
| `44일 무료` | `total_trial_days` |
| `(기본 30일 + 쿠폰 14일)` | `base_trial_days` + `trial_days` |
| `지금 결제되는 금액 0원` | 고정 문구 (체험은 즉시 과금 없음) |
| `무료 체험 종료` | `trial_ends_at` |
| `첫 결제 예정` 날짜 | `first_charge_at` |
| `첫 결제 예정` 금액 | `first_charge_amount` (+ 추가계정 시 `extra_ig_account_price × N`) |

참고 구현:

```typescript
const res = await validateReferralCode(code);
if (!res.ok || !res.data?.valid) {
  showError(res.data?.reason ?? '쿠폰을 확인할 수 없습니다.');
  return;
}
const v = res.data;
const totalAmount = (v.first_charge_amount ?? 0)
  + (v.extra_ig_account_price ?? 0) * extraAccounts;

setCouponPreview({
  totalDays: v.total_trial_days!,          // 44
  baseDays: v.base_trial_days!,            // 30
  bonusDays: v.trial_days!,                // 14
  trialEndsAt: new Date(v.trial_ends_at!),
  firstChargeAt: new Date(v.first_charge_at!),
  firstChargeAmount: totalAmount,          // 14900 (+ 추가계정)
  planLabel: v.plan!.display_name,
});
```

### 문구 주의사항

- **"14일 무료" 라고 쓰지 마세요.** 사용자가 받는 건 44일입니다.
- "N개월" 로 환산할 땐 `Math.round(total_trial_days / 30)` — 44일이면 "약 1.5개월" 또는
  "44일" 이 정확합니다. 44를 "1개월" 로 반올림하면 14일을 또 숨기는 셈입니다.
- 첫 결제일·금액을 **카드 등록 전에** 명시해야 합니다(정기결제 고지 의무 + 분쟁 예방).
- 체험 중 해지 가능함을 함께 안내하면 전환율에 유리합니다.

---

## 5. 배포 순서 / 호환성

백엔드를 먼저 배포해도 프론트가 깨지지는 않습니다 — 다만 **쿠폰 "적용" 버튼이 400 토스트**를
띄웁니다(`detail` 이 사용자에게 말이 되는 문장이라 치명적이진 않지만, 쿠폰을 못 씁니다).

권장 순서:

1. 프론트에서 **쿠폰 입력을 결제 플로우에 통합** (`redeemReferralCode` 제거 + 표기 필드 교체)
2. 프론트 배포
3. 백엔드 배포

프론트 배포가 늦어질 경우, **백엔드 배포를 프론트 뒤로 미루는 것**이 안전합니다.
(백엔드만 먼저 나가면 그 사이 쿠폰 신규 사용이 전면 불가 → 문의 유발)

### 검증 체크리스트

- [ ] 유효한 14일 쿠폰 입력 → 화면에 **44일**, 첫 결제일 = 오늘 + 44일, 금액 14,900원
- [ ] 카드 등록 완료 → `subscription.status === "trialing"`,
      `current_period_end` 가 미리보기와 ±수분 내로 일치
- [ ] 무효/만료/소진 코드 → `valid: false` + `reason` 노출, 결제 버튼 비활성
- [ ] 이미 쿠폰 쓴 계정 → confirm 400 `이미 제휴/레퍼럴 코드를 사용하셨습니다.`
- [ ] 추가 IG 계정 N개 선택 시 첫 결제 금액에 `9,900 × N` 가산 표시
- [ ] `redeemReferralCode` 호출부가 코드베이스에 남아 있지 않은지 grep

---

## 6. 기존 피해자 처리 (백엔드 — ✅ 2026-08-04 완료)

카드 없이 쿠폰만 써서 14일만 받은 사용자 **3명을 44일로 보정 완료**했습니다.
`HLEVEL26` 사용자 17명 전원이 이제 44일로 균일합니다.

| uid | 이메일 | 보정 전 만료 | 보정 후 만료 |
|---|---|---|---|
| 109 | hjban1351@gmail.com | 08-16 16:40 | **09-15 16:40** |
| 119 | jhji17@naver.com | 08-17 11:14 | **09-16 11:14** |
| 130 | rainytea58@gmail.com | 08-18 11:55 | **09-17 11:55** |

- `user_subscriptions.current_period_end` 와 `referral_redemptions.trial_ends_at` 을 함께 +30일 (동기 확인)
- `trial_used_at` 은 **의도적으로 비워둠** — 이미 손해 본 고객에게서 재체험 자격까지 빼앗지 않기 위함
- 구독 응답에 캐시가 없어 **다음 조회부터 즉시 반영**됩니다. 프론트 조치 불필요.
- 이 3명은 **아직 카드가 없습니다** → 체험 종료 시 과금이 아니라 free 강등입니다.
  별도로 카드 등록을 안내할 예정이며, 안내 후 이들이 카드를 등록하면 아래 경로를 탑니다.

폐지 전 무카드 체험 중인 기존 사용자가 카드를 등록하면 `scenario: "attach_only"` 로
**잔여 기간 유지 + 종료 시 첫 결제** 가 됩니다 — 프론트에서 특별히 분기할 것은 없습니다.

> ⚠️ 이들이 카드를 등록할 때는 **쿠폰 코드를 다시 넣지 않도록** 해주세요. 이미 사용한
> 코드라 `이미 제휴/레퍼럴 코드를 사용하셨습니다.` 400 이 납니다. 체험 중인 사용자에게는
> 쿠폰 입력란을 숨기고(`my-status` 의 `redeemed: true` 로 판별) 카드 등록만 유도하면 됩니다.

---

## 7. 서버 측 변경 파일

| 파일 | 변경 |
|---|---|
| `apps/billing/referral_views.py` | 무카드 redeem 폐지(항상 400) + validate 미리보기 필드 추가 |
| `apps/billing/serializers.py` | `ReferralCodeValidateResponseSerializer` 필드 5개 추가, `referral_code` help_text 정정 |
| `apps/billing/toss_flows.py` | 시나리오 docstring 정정 (쿠폰 체험의 유일 경로임을 명시) |
| `apps/billing/test_referral_validate.py` | 미리보기 계약 + 폐지 회귀 테스트 |
| `apps/billing/test_retention.py` | T-3 재체험 테스트를 confirm 경로로 이관 + 44일 단언 |
| `TOSS_BILLING_FRONTEND.md` | §4 쿠폰 섹션 갱신, "+30일/60일" 하드코딩 문구 정정 |
