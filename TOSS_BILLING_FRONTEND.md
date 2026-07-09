# 토스페이먼츠 빌링 — 프론트엔드 연동 가이드

> PayApp은 완전히 제거되었습니다. 결제는 **토스페이먼츠 빌링키 정기결제**로 동작합니다.
> 현재 **테스트 키** 환경입니다 (실과금 없음). 라이브 전환은 백엔드 키 교체만으로 완료되며
> 프론트 코드 변경은 없습니다.

## 0. 큰 그림

```
[요금제 페이지]                [카드 등록]                       [백엔드]
GET /billing/plans/  ──▶  requestBillingAuth(SDK)  ──▶  successUrl?authKey=...
                                                          │
                                                          ▼
                                            POST /billing/toss/confirm/
                                            { auth_key, plan_name, referral_code? }
                                                          │
                     ┌────────────────────────────────────┤
                     ▼                                    ▼
             pro 첫 구독: 무료 체험 30일          basic: 즉시 첫 결제
             (제휴코드 시 60일, 과금 없음)        (200 + payment 객체)
```

- **매월 갱신은 백엔드가 알아서** 합니다 (Celery가 빌링키로 자동 승인). 프론트는 결제일에 아무것도 할 필요 없음.
- 구독 상태의 단일 소스는 `GET /billing/my-subscription/` — 사용량/다음 결제일/카드 정보까지 전부 들어 있습니다.

## 1. 요금제 페이지

`GET /api/v1/billing/plans/` (비인증)

- `monthly_price` = 현재 판매가, `list_price` = 정가. `list_price > monthly_price`면 **할인 중** (정가 취소선).
- 프로는 론칭 프로모 9,900원 (정가 15,900원). **가입 시점 가격이 영구 고정**(그랜드파더링)이므로 "지금 가입하면 계속 이 가격" 소구 가능.
- `features` 로 플랜별 표를 렌더:

| features 키 | 의미 |
|---|---|
| `max_pages` | 링크페이지 수 (-1=무제한) |
| `ai_unlimited` | AI 생성 무제한 여부 (false면 가입 시 2회 제공) |
| `remove_logo` | 턴플로우 배지 제거 |
| `custom_css` | 커스텀 CSS |
| `dm_monthly_limit` | DM 자동화 월 한도 (-1=무제한) |
| `analytics_export` | 기간별 분석·엑셀 다운로드 (프론트에서 이 플래그로 UI 게이트) |
| `spam_filter` | 스팸 댓글 필터링 |
| `max_ig_accounts` | 기본 IG 연동 수 (프로는 추가 구매로 확장) |

## 2. 카드 등록 → 구독 시작

### 2-1. SDK 로드 & 등록창 열기

```html
<script src="https://js.tosspayments.com/v2/standard"></script>
```

```typescript
// 1) 준비 — 클라이언트 키 + 사용자 고유 customerKey 수령
const prep = await api.get('/billing/toss/prepare/');  // 인증 필요

// 2) 카드 등록창
const tossPayments = TossPayments(prep.client_key);
const payment = tossPayments.payment({ customerKey: prep.customer_key });

await payment.requestBillingAuth({
  method: 'CARD',
  successUrl: `${location.origin}/payment/billing-success?plan=pro&code=${referralCode}`,
  failUrl: `${location.origin}/payment/billing-fail`,
  customerEmail: prep.customer_email,
});
```

### 2-2. successUrl 페이지에서 확정

successUrl로 리다이렉트되면 쿼리에 `authKey`, `customerKey`가 붙습니다.

```typescript
const params = new URLSearchParams(location.search);
const res = await api.post('/billing/toss/confirm/', {
  auth_key: params.get('authKey'),
  plan_name: 'pro',              // 'basic' | 'pro' | 생략(카드 변경)
  referral_code: code || undefined,   // 제휴코드 — pro 첫 구독에만
  extra_ig_accounts: 0,          // pro 전용 추가 계정 (선택)
});
```

**응답 분기** (`res.scenario`):

| scenario | 의미 | UI |
|---|---|---|
| `trial` | 무료 체험 시작 (과금 0원) | "무료 체험이 시작되었습니다 — 첫 결제일 {first_charge_at}" |
| `charge_now` | 즉시 결제 완료 | 영수증 링크(`payment.receipt_url`) + 구독 시작 안내 |
| `card_change` | 카드 교체 완료 | "결제 카드가 변경되었습니다" |
| `attach_only` | (무카드 체험 중) 카드 부착 | "체험 종료 시 자동 결제됩니다" |

**에러 분기** (HTTP status):

| status | 의미 | UI |
|---|---|---|
| 400 | authKey 만료/카드 등록 실패/제휴코드 무효 (`detail` 표시) | 안내 후 재시도 |
| 402 | 카드 승인 거절 (즉시 결제 시나리오) — 카드는 등록됨 | "다른 카드로 시도" 또는 재시도 |
| 202 | 결제 결과 확인 중 (통신 지연) | "확인 중" 표시 후 결제 내역 폴링 (30분 내 자동 확정) |

### 2-3. 무료 체험 규칙 (UI 문구용)

- 프로 **최초** 구독 = 카드 등록만으로 30일 무료. 체험 종료일에 첫 자동결제.
- 제휴 코드 입력 시 +30일 (총 60일 후 첫 결제). 코드는 **1인 1회**.
- 체험은 1인 1회 — 해지 후 재구독하면 즉시 결제됩니다.
- 체험 중 해지하면 과금 없이 체험 종료일까지 이용.
- **베이직은 체험 없음** — 등록 즉시 첫 달 결제.

## 3. 구독 관리

| 액션 | API | 비고 |
|---|---|---|
| 내 구독 + 사용량 | `GET /billing/my-subscription/` | `usage.dm/pages/ig_accounts/ai_tokens`, `trial_ends_at`, `next_billing{date,amount}` |
| 해지 (기간말까지 이용) | `POST /billing/cancel/` | 즉시 환불 아님 — 기간말 자동 무료 전환 |
| 해지 취소(재개) | `POST /billing/resume/` | 기간 내 + 카드 등록 상태만 |
| 플랜 변경 | `POST /billing/change-plan/` `{plan_name}` | 업그레이드=**남은 기간분 차액만 즉시 비례 결제 + 갱신일 유지** / 다운그레이드=`effective_at`에 적용 |
| 플랜 변경 **견적** | `POST /billing/change-plan/preview/` `{plan_name}` | **결제 전 미리보기(부작용 0)**. `immediate_charge.amount`=지금 청구액. → §3-1 |
| 카드 변경 | prepare → requestBillingAuth → confirm(`plan_name` 생략) | past_due면 자동 재시도됨 |
| 추가 IG 계정 (pro) | `POST /billing/extra-accounts/` `{count}` | 증가분 × 9,900원을 **잔여일만큼 비례 즉시 결제**, 다음 갱신부터 전체 합산 |
| 추가 IG 계정 **견적** | `POST /billing/extra-accounts/preview/` `{count}` | **결제 전 미리보기(부작용 0)**. `unit_price`(9,900) 포함. → §3-1 |
| 결제 내역 | `GET /billing/payments/history/` | `receipt_url` = 토스 영수증 |
| 환불 가능 여부 | `GET /billing/refund-eligibility/` | 결제 후 7일 + 유료 기능 미사용 |
| 환불 | `POST /billing/payments/{id}/refund/` | 성공 시 즉시 무료 전환 |

### 3-1. 비례배분(proration) & 결제 전 견적 — ⭐신규

주기 중간에 **업그레이드**하거나 **추가 IG 계정을 늘리면**, 한 달치 전액이 아니라
**남은 기간에 해당하는 금액만** 그 자리에서 결제됩니다. 갱신일(다음 결제일)은 **그대로 유지**되고,
다음 갱신부터 새 요금 전액이 청구됩니다.

- **업그레이드**(basic→pro): `지금 청구 = (프로 잔여분) − (기존 베이직 잔여 크레딧)`. 예) 잔여 12일이면 약 2,400원만 즉시 결제, 갱신일 그대로, 다음 달부터 프로 전액.
- **추가 계정 증가**: `지금 청구 = 9,900 × 증가분 × (잔여일/30)`. 예) 7일차에 1개 추가(잔여 23일) → 약 7,590원.
- **다운그레이드 / 계정 축소**: 즉시 청구·환불 **없음**. 다음 갱신부터 낮은 금액(다운그레이드는 `effective_at`에 적용).
- 주기 말에 걸쳐 잔여 금액이 0이면 **무과금으로 즉시 적용**되고 `payment`/`immediate_charge.amount`는 0.

**반드시 "견적 → 확정" 2단계로 붙이세요.** 실행 API를 바로 부르지 말고, 먼저 `preview`로
"지금 N원이 결제됩니다"를 사용자에게 보여준 뒤 확정 시 실행 API를 호출합니다.

```typescript
// 1) 견적 (부작용 없음 — 몇 번을 불러도 안전, 결제 안 됨)
const quote = await (await fetch('/api/v1/billing/change-plan/preview/', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
  body: JSON.stringify({ plan_name: 'pro' }),
})).json();
// quote.immediate_charge.amount → "지금 2,400원이 결제됩니다" 확인 모달
// quote.next_renewal_amount    → "다음 달부터 매월 9,900원"

// 2) 사용자가 확정하면 같은 body 로 실행 API 호출
if (userConfirmed) {
  const res = await fetch('/api/v1/billing/change-plan/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ plan_name: 'pro' }),
  });
}
```

**견적 응답 필드**
| 필드 | 의미 |
|---|---|
| `direction` | `upgrade`/`downgrade`/`noop` (플랜) · `increase`/`decrease`/`noop` (추가계정) |
| `immediate_charge.amount` | **지금 즉시 결제될 금액(원, 세포함)**. 0이면 무과금 즉시 적용 |
| `immediate_charge.proration` | 업그레이드: `remaining_days`,`new_plan_prorated`,`current_plan_credit`,`net` / 추가계정: `unit_price`,`units`,`full_amount`,`net` |
| `effective_at` | 다운그레이드 예약 적용 시각(현재 주기 종료일). 업그레이드/증가는 `null` |
| `next_renewal_amount` | **다음 정기 갱신 전액**(즉시 청구액과 다름!) |
| `unit_price` | (추가계정 견적) 계정 단가 9,900 — **하드코딩 말고 이 값을 쓰세요** |

> ⚠️ **`immediate_charge.amount`(지금 청구) ≠ `next_renewal_amount`(다음 갱신 전액)** 를 반드시 구분해 표기하세요.
> "지금 2,400원 결제 / 다음 달부터 매월 9,900원"처럼요. `my-subscription`의 `next_billing.amount`는
> **다음 정기 갱신액**일 뿐, 지금 변경 시 즉시 청구액이 아닙니다(견적 API로 확인).

> 💡 실행 API 응답의 `payment.amount`가 실제 청구된 금액입니다. 견적과 실청구는 **동일 계산**을 쓰지만,
> 견적~확정 사이 시간이 흐르면 잔여일 계산으로 **최대 몇 원** 차이날 수 있습니다(정상). 최종 금액은 `payment.amount` 기준.

> ⛔ **이중 청구 주의**: 실행 API 호출 중 버튼을 비활성화하세요. 서버가 결정적 주문번호로 중복 클릭을 막지만,
> 이전 결제가 확인 중(202)일 때 다른 변경을 시도하면 `202`("결제 확인 중")로 거절될 수 있습니다 — 잠시 후 재시도 안내.

### past_due (결제 실패) 안내

갱신 결제가 실패하면 `status: "past_due"`가 됩니다. D+1/D+3/D+5 자동 재시도, 7일 내 미해결 시 무료 전환.
`my-subscription`의 `next_billing.date`(다음 재시도)와 함께 **"카드 변경" CTA**를 노출하세요 —
카드를 변경하면 즉시 재시도됩니다.

### DM 월 한도 (free/basic 200건)

- `usage.dm.used / limit`으로 게이지 렌더. `limit: -1` = 무제한(pro).
- 한도 도달 시 발송은 **SKIPPED**로 보류됩니다 — 유실이 아니며, **프로 업그레이드 후
  캠페인의 "실패 재발송(retry-failed)"으로 되살릴 수 있습니다** (메시징 윈도우 내 건만).
  업셀 문구에 활용하세요.

### IG 계정 연동 한도

연동 시도(`connect/start`)가 한도 초과면 **HTTP 429 + `error.code: "PLAN_LIMIT_EXCEEDED"`**.
OAuth 팝업 콜백에서 초과가 감지되면 `postMessage`로 `errorCode: 'PLAN_LIMIT_EXCEEDED'`가 옵니다.
→ 프로 추가 계정 구매 모달로 연결.

## 4. 무카드 제휴 체험 (기존 레퍼럴)

`POST /billing/referral/redeem/`(카드 없이 체험 시작)은 그대로 동작합니다.
무카드 체험 중 사용자가 카드를 등록(confirm)하면 **잔여 체험 기간은 유지**되고 종료 시 첫 결제가 진행됩니다.
제휴/레퍼럴 코드는 경로와 무관하게 1인 1회입니다.

## 5. 테스트 방법 (테스트 키)

- 결제창 대신 백엔드 dev 헬퍼로도 전 과정 테스트 가능: `POST /billing/toss/dev/issue-billing-key/`
  (카드번호 `4579731111111111`(국민카드 BIN), 유효기간 미래값, 생년월일 `900101`).
  dev 환경(`TOSS_DEV_CARD_AUTH_ENABLED=True`)에서만 열립니다.
  ⚠️ 비자/마스터 등 해외 브랜드 BIN은 테스트 환경에서 빌링 승인이 거절됩니다
  (`NOT_SUPPORTED_CARD_TYPE`) — 국내 카드사 BIN을 쓰세요 (국민 457973 / 신한 515594 · 438676 / 롯데 940926 검증됨).
- 테스트 거래는 토스 개발자센터 > 테스트 거래내역에서 확인.
- 실 SDK 흐름 테스트: 위 2-1/2-2 그대로 — 테스트 키에서는 실제 과금이 없습니다.

## 6. 하지 말 것

- ❌ `customer_key`를 로컬스토리지 등에 장기 보관하거나 URL에 노출 (항상 prepare로 수령)
- ❌ 결제 금액/플랜 가격을 프론트에 하드코딩 (plans API가 소스)
- ❌ successUrl에서 authKey를 서버 전달 없이 폐기 (authKey는 일회성 — 즉시 confirm 호출)
- ❌ `/billing/toss/webhook/` 호출 (토스 서버 전용)
